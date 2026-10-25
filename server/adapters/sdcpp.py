"""stable-diffusion.cpp 适配器 —— 图片与视频生成（本程序管理进程）。

为什么用 CLI 而不用 sd-server：
1. **显存要能彻底释放。** 8GB 卡上文本模型占约 7GB，生成图片/视频前必须先卸载；
   一次性进程退出即归还全部显存，比常驻服务更可靠。
2. **参数完全显式。** 图片是一个文件、视频是三个文件（transformer+VAE+编码器），
   CLI 每次把参数拼全，不依赖引擎内部状态。
3. **失败隔离。** 一次生成崩了不影响下一次，也不会把引擎卡在坏状态。

实测踩过的坑，代码里都对应处理了：
- 官方 Wan `Wan2.1_VAE.pth` 不被接受（键名/形状不匹配），必须用 Comfy 重打包的 safetensors。
- 30 万字符的进度条用 `\\r` 原地刷新，不能用 readline，要按 `\\r`/`\\n` 双分隔读。
- VAE 解码会 OOM 后自动降级分块，耗时翻倍，但不会失败——所以要如实把这段耗时报出来。
"""
import os
import re
import subprocess
import sys
import threading
import time

from .base import Adapter, BackendError, _progress

# 进度条形如：  |========>          | 3/6 -  19.49s/it - ETA 01:01
_STEP_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)")
_TAKING_RE = re.compile(r"taking\s+([\d.]+)s")

# 引擎日志里的阶段标志 → 给前端看的阶段名
_STAGE_MARKS = (
    ("loading diffusion model", "loading", "加载扩散模型"),
    ("loading t5xxl", "loading", "加载文本编码器"),
    ("loading vae", "loading", "加载 VAE"),
    ("loading tensors completed", "loading", "权重载入完成"),
    ("get_learned_condition completed", "encode", "文本编码完成"),
    ("generating latent video completed", "sample", "采样完成"),
    ("sampling completed", "sample", "采样完成"),
    ("decoding", "decode", "VAE 解码"),
    ("save result image", "save", "写入文件"),
)

_OOM_MARKS = ("out of memory", "failed during weight preparation",
              "cannot make enough memory available")
_TILE_MARKS = ("retrying with temporal tiling", "Reducing VAE decode tiles")


class SDCppAdapter(Adapter):
    name = "sdcpp"
    label = "stable-diffusion.cpp（图片 / 视频）"
    managed = True

    def __init__(self, cfg, models_mod=None):
        super().__init__(cfg)
        self._models = models_mod
        self._lock = threading.RLock()
        self._proc = None
        self._cancelled = False
        self._devices = None
        self._log_path = self.cfg.get("log_file") or os.path.join("logs", "sdcpp.log")

    # ---------------------------------------------------------------- 路径

    @property
    def binary(self):
        b = self.cfg.get("binary") or ""
        if not b:
            return ""
        if os.path.isabs(b):
            return b
        from .. import config
        local = os.path.join(config.ROOT, b)
        if os.path.exists(local):
            return local
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            packed = os.path.join(meipass, b)
            if os.path.exists(packed):
                return packed
        return local

    @property
    def workdir(self):
        return os.path.dirname(os.path.abspath(self.binary)) if self.binary else "."

    @property
    def base(self):
        return ""          # 不是 HTTP 服务，走命令行

    def status(self):
        exists = bool(self.binary and os.path.isfile(self.binary))
        info = {
            "adapter": self.name, "label": self.label, "managed": True,
            "base": "", "binary": self.binary, "binary_exists": exists,
            "engine_running": bool(self._proc and self._proc.poll() is None),
            "mode": "cli",
        }
        if self._devices is not None:
            info["devices"] = self._devices
        return info

    # ---------------------------------------------------------------- 设备

    def devices(self, refresh=False):
        """列出可用后端设备。结果缓存，供界面显示"用哪块卡"。"""
        if self._devices is not None and not refresh:
            return self._devices
        if not self.binary or not os.path.isfile(self.binary):
            return []
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run([self.binary, "--list-devices"], cwd=self.workdir,
                                 capture_output=True, text=True, timeout=60,
                                 creationflags=flags, encoding="utf-8", errors="replace")
            devs = []
            for line in (out.stdout or "").splitlines():
                s = line.strip()
                if s and ("=" in s or "GPU" in s or "Vulkan" in s or "CUDA" in s):
                    devs.append(s)
            self._devices = devs
            return devs
        except Exception:
            return []

    # ---------------------------------------------------------------- 就绪校验

    def ensure(self, model_id, ctx=None):
        """扩散模型没有上下文概念，这里只校验二进制与组件是否齐全。"""
        if not self.binary or not os.path.isfile(self.binary):
            raise BackendError(
                "ENGINE_BINARY_MISSING",
                "找不到 sd-cli 可执行文件：%s\n"
                "请把 stable-diffusion.cpp 解压到 runtime/sdcpp/，"
                "或在 config.json 的 adapters.sdcpp.binary 里填写正确路径"
                % (self.binary or "(未配置)"))
        entry = self._entry(model_id)
        if entry.get("missing"):
            raise BackendError(
                "MODEL_INCOMPLETE",
                "模型 %s 缺少组件：%s\n把缺少的文件放进 %s"
                % (model_id, "、".join(entry["missing"]), entry["path"]))
        return self.status()

    def _entry(self, model_id):
        if self._models is None:
            raise BackendError("MODEL_NOT_FOUND", "模型登记表不可用")
        entry = self._models.get(model_id)
        if not entry:
            raise BackendError("MODEL_NOT_FOUND", "模型 %s 不在 models 目录里" % model_id)
        return entry

    # ---------------------------------------------------------------- 参数

    def _build_args(self, entry, req, out_path):
        c = self.cfg
        video = (entry.get("kind") == "video") or int(req.get("frames") or 0) > 1
        args = [self.binary]

        comps = entry.get("components") or {}
        if entry.get("bundle"):
            # 组件包：扩散主模型 + VAE + 文本编码器分别指定
            if comps.get("diffusion"):
                args += ["--diffusion-model", comps["diffusion"]["path"]]
            elif comps.get("model"):
                args += ["-m", comps["model"]["path"]]
            if comps.get("vae"):
                args += ["--vae", comps["vae"]["path"]]
            if comps.get("t5xxl"):
                args += ["--t5xxl", comps["t5xxl"]["path"]]
            if comps.get("clip_l"):
                args += ["--clip_l", comps["clip_l"]["path"]]
            if comps.get("clip_g"):
                args += ["--clip_g", comps["clip_g"]["path"]]
            if comps.get("llm"):
                args += ["--llm", comps["llm"]["path"]]
            # 视觉塔单独存放时（部分 Qwen3-VL 包）才需要显式给
            if comps.get("llm_vision"):
                args += ["--llm_vision", comps["llm_vision"]["path"]]
            # 音视频联合模型（MiniMax-H3）多了音频 VAE
            if comps.get("audio_vae"):
                args += ["--audio-vae", comps["audio_vae"]["path"]]
        else:
            args += ["-m", entry["path"]]

        if video:
            args += ["-M", "vid_gen"]

        args += ["-p", str(req.get("prompt") or "")]
        # 参考素材：文字仍是主控，素材只做条件输入
        init = req.get("init_image")
        if init:
            args += ["-i", str(init)]
            if req.get("strength") is not None:
                # --strength 是加噪强度：越小越贴近原图，越大越放开发挥
                args += ["--strength", "%.2f" % float(req["strength"])]
        if req.get("end_image"):
            args += ["--end-img", str(req["end_image"])]
        if req.get("mask"):
            args += ["--mask", str(req["mask"])]
        neg = str(req.get("negative") or "").strip()
        if neg:
            args += ["-n", neg]

        w = int(req.get("width") or 512)
        h = int(req.get("height") or 512)
        args += ["-W", str(w), "-H", str(h)]
        args += ["--steps", str(int(req.get("steps") or 20))]
        args += ["--cfg-scale", str(float(req.get("cfg_scale") or 7.0))]
        if req.get("seed") not in (None, ""):
            args += ["-s", str(int(req.get("seed")))]
        if req.get("sampler"):
            args += ["--sampling-method", str(req["sampler"])]

        if video:
            args += ["--video-frames", str(int(req.get("frames") or 9))]
            args += ["--fps", str(int(req.get("fps") or 16))]

        # 后端分配：默认交给引擎 auto-fit，显式配置则照办
        backend = str(c.get("backend") or "").strip()
        if backend:
            args += ["--backend", backend]
        params_backend = str(c.get("params_backend") or "").strip()
        if params_backend:
            args += ["--params-backend", params_backend]
        if c.get("auto_fit") is False:
            args += ["--auto-fit", "off"]

        args += ["-o", out_path]
        args += list(c.get("extra_args") or [])
        # 单个模型自己的引擎参数（例如大模型才需要的 --offload-to-cpu）
        args += list(entry.get("extra_args") or [])
        return args

    # ---------------------------------------------------------------- 生成

    def generate(self, model_id, req, out_path, on_event=None):
        """跑一次生成。

        on_event(dict) 会收到两类事件：
          {"type": "stage", "stage": "...", "label": "..."}
          {"type": "progress", "step": 3, "total": 6, "it": "19.5s/it", "eta": "01:01"}
        阻塞到进程结束；返回结果 dict。取消请调用 cancel()。
        """
        entry = self._entry(model_id)
        self.ensure(model_id)
        if int(req.get("frames") or 1) > 1 and entry.get("kind") != "video":
            raise BackendError("WRONG_KIND", "模型 %s 不是视频模型，不能设 frames > 1" % model_id)

        # 引擎在 runtime/sdcpp 下执行，相对路径会落到引擎目录里。统一转绝对路径。
        out_path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        args = self._build_args(entry, req, out_path)

        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([self.workdir, env.get("PATH", "")])
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)

        os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
        started = time.time()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise BackendError("ENGINE_BUSY", "上一次生成还没结束，请先等待或停止")
            self._cancelled = False
            _progress("生成：%s → %s" % (os.path.basename(entry["path"]), os.path.basename(out_path)))
            logf = open(self._log_path, "ab", buffering=0)
            logf.write(("\n===== %s 生成 %s =====\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(entry["path"]))).encode())
            self._proc = subprocess.Popen(
                args, cwd=self.workdir, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=flags, stdin=subprocess.DEVNULL, bufsize=0,
            )
            proc = self._proc

        stages, warnings = [], []
        last_step = {"cur": 0, "total": 0}

        def emit(ev):
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

        try:
            for line in _iter_output(proc):
                try:
                    logf.write((line + "\n").encode("utf-8", "replace"))
                except Exception:
                    pass

                s = line.strip()
                if not s:
                    continue

                # 进度条
                m = _STEP_RE.search(s)
                if m:
                    cur, total = int(m.group(1)), int(m.group(2))
                    if (cur, total) != (last_step["cur"], last_step["total"]):
                        last_step.update({"cur": cur, "total": total})
                        eta = ""
                        em = re.search(r"ETA\s+([\d:]+)", s)
                        if em:
                            eta = em.group(1)
                        emit({"type": "progress", "step": cur, "total": total,
                              "it": "%s %s" % (m.group(3), m.group(4)), "eta": eta})
                    continue

                low = s.lower()
                # 显存不足 → 记录，最终如实告诉用户质量/耗时受影响
                if any(k.lower() in low for k in _OOM_MARKS):
                    if s.startswith("[E]") or "failed" in low:
                        warnings.append("VAE 显存不足，已降级分块解码")
                    continue
                if any(k.lower() in low for k in _TILE_MARKS):
                    warnings.append("VAE 解码分块降级，耗时增加")
                    emit({"type": "stage", "stage": "decode", "label": "显存不足，降级分块解码"})
                    continue

                for mark, stage, label in _STAGE_MARKS:
                    if mark.lower() in low:
                        info = {"stage": stage, "label": label}
                        tm = _TAKING_RE.search(s)
                        if tm:
                            info["took_s"] = float(tm.group(1))
                        stages.append(info)
                        emit(dict(info, type="stage"))
                        break
        finally:
            try:
                proc.wait(timeout=30)
            except Exception:
                pass
            with self._lock:
                self._proc = None
            try:
                logf.close()
            except Exception:
                pass

        elapsed = time.time() - started
        code = proc.returncode
        if self._cancelled:
            raise BackendError("CANCELLED", "生成已取消")

        # 输出文件可能被引擎自动改后缀（.mp4 → .mp4.avi），两种都认
        real = _resolve_output(out_path)
        if code != 0:
            tail = _tail(self._log_path, 15)
            if real:
                warnings.append("引擎以退出码 %s 结束，但已产出文件" % code)
            else:
                raise BackendError(
                    "GENERATE_FAILED",
                    "生成失败（退出码 %s）。日志尾部：\n%s" % (code, tail),
                    detail={"exit_code": code})
        if not real:
            raise BackendError("NO_OUTPUT",
                               "生成结束但没有产出文件。日志尾部：\n%s" % _tail(self._log_path, 15))

        size = os.path.getsize(real)
        result = {
            "file": os.path.basename(real),
            "path": real,
            "size": size,
            "elapsed_ms": int(elapsed * 1000),
            "width": int(req.get("width") or 512),
            "height": int(req.get("height") or 512),
            "frames": int(req.get("frames") or 1) if req.get("frames") else 1,
            "stages": stages,
            "warnings": sorted(set(warnings)),
            "engine": "sdcpp",
        }
        # 同名 sidecar：让"同一条结果"能被复现，而不是只剩一张图。
        # 放在适配器层而不是 HTTP 层，这样命令行/脚本调用同样有记录。
        try:
            import json as _json
            rec = {"prompt": req.get("prompt"), "negative": req.get("negative"),
                   "req": req, "model": model_id, "kind": entry.get("kind"),
                   "width": result["width"], "height": result["height"],
                   "frames": result["frames"],
                   "elapsed_ms": result["elapsed_ms"], "warnings": result["warnings"],
                   "created_at": int(time.time() * 1000)}
            with open(real + ".json", "w", encoding="utf-8") as f:
                _json.dump(rec, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return result

    def cancel(self):
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        _kill_tree(proc)
        return True

    def stop(self):
        self.cancel()


def _iter_output(proc):
    """按 \\r 与 \\n 双分隔读取。

    引擎的进度条用 \\r 原地刷新，整段可能有几十万字符且没有换行；
    用 readline 会一直等到结束才吐出来，进度就失去意义了。
    """
    buf = b""
    while True:
        chunk = proc.stdout.read(4096) if proc.stdout else b""
        if not chunk:
            break
        buf += chunk
        while True:
            i_n = buf.find(b"\n")
            i_r = buf.find(b"\r")
            idx = min(x for x in (i_n, i_r) if x >= 0) if (i_n >= 0 or i_r >= 0) else -1
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1:]
            text = line.decode("utf-8", "replace")
            if text.strip():
                yield text
    if buf.strip():
        yield buf.decode("utf-8", "replace")


def _resolve_output(path):
    """引擎可能把 out.mp4 写成 out.mp4.avi，逐个后缀试探。"""
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    base, ext = os.path.splitext(path)
    for cand in (path + ".avi", base + ".avi", base + ".webm", base + ".webp",
                 path + ".png", base + ".png"):
        if os.path.isfile(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def _tail(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            text = f.read().decode("utf-8", "replace")
        return "\n".join([l for l in text.strip().splitlines() if l.strip()][-lines:])
    except Exception:
        return "(无法读取日志)"


def _kill_tree(proc):
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15, creationflags=flags)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
