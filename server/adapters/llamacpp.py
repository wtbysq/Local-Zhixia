"""llama.cpp 的 llama-server 适配器（本程序负责拉起与回收进程）。

这是 `managed=True` 的唯一实现：它知道怎么启动一个引擎进程，其余适配器只管连接。

三条设计要点，全部来自实测踩坑：
1. **进程输出必须落文件，不能用 PIPE。** PIPE 不读会写满缓冲区把引擎卡死。
2. **DLL 目录要进 PATH。** 从 LM Studio 运行时包里取出的 llama-server.exe
   依赖同目录 dll 与 vendor 下的 CUDA 运行库；不加 PATH 会以 0xC0000135 静默退出。
3. **起之前先算显存。** 装不下就直接报数字，不要让用户面对一个 OOM 崩溃。
"""
import atexit
import os
import subprocess
import threading
import time

from .base import Adapter, BackendError, _progress, gpu_info


class LlamaCppAdapter(Adapter):
    name = "llamacpp"
    label = "llama.cpp llama-server（本程序管理进程）"
    managed = True

    def __init__(self, cfg, models_mod=None):
        super().__init__(cfg)
        self._proc = None
        self._model_id = None
        self._ctx = None
        self._lock = threading.RLock()
        self._log_path = self.cfg.get("log_file") or os.path.join("logs", "engine.log")
        self._models = models_mod
        atexit.register(self.stop)

    # ---------------------------------------------------------------- 路径

    @property
    def binary(self):
        b = self.cfg.get("binary") or ""
        if not b:
            return ""
        if os.path.isabs(b):
            return b
        # 相对路径：先按 exe 旁找（用户可替换引擎），再按打包内部找
        from .. import config
        local = os.path.join(config.ROOT, b)
        if os.path.exists(local):
            return local
        meipass = getattr(__import__("sys"), "_MEIPASS", None)
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
        host = self.cfg.get("host") or "127.0.0.1"
        port = int(self.cfg.get("port") or 8090)
        return "http://%s:%d/v1" % (host, port)

    def _search_paths(self):
        """二进制所在目录 + vendor 运行库目录，一起放进子进程 PATH。"""
        dirs = [self.workdir]
        explicit = self.cfg.get("vendor_dir")
        if explicit:
            dirs.append(explicit)
        else:
            backends = os.path.dirname(self.workdir)
            vendor_root = os.path.join(backends, "vendor")
            if os.path.isdir(vendor_root):
                for name in sorted(os.listdir(vendor_root)):
                    d = os.path.join(vendor_root, name)
                    if os.path.isdir(d) and any(f.lower().startswith(("cudart", "vulkan"))
                                                for f in os.listdir(d)):
                        dirs.append(d)
        return dirs

    # ---------------------------------------------------------------- 状态

    def status(self):
        alive = bool(self._proc and self._proc.poll() is None)
        info = {
            "adapter": self.name, "label": self.label, "managed": True,
            "base": self.base, "binary": self.binary,
            "binary_exists": bool(self.binary and os.path.isfile(self.binary)),
            "engine_running": alive,
            "model": self._model_id, "ctx": self._ctx,
        }
        if self._proc is not None:
            info["pid"] = self._proc.pid
        if self._proc is not None and not alive:
            info["exit_code"] = self._proc.returncode
        return info

    def ensure(self, model_id, ctx):
        """确保引擎在跑、且加载的正是这个模型。必要时重启一次。"""
        if not self.binary or not os.path.isfile(self.binary):
            raise BackendError(
                "ENGINE_BINARY_MISSING",
                "找不到 llama-server 可执行文件：%s\n请在 config.json 的 adapters.llamacpp.binary 里填写正确路径"
                % (self.binary or "(未配置)"))

        path = self._model_path(model_id)
        ctx = int(ctx)
        with self._lock:
            if self._active > 0 and (self._model_id != model_id or self._ctx != ctx):
                raise BackendError("MODEL_BUSY", "正在生成回答，暂不能切换模型或上下文；请等这次回答结束")
            if (self._proc and self._proc.poll() is None
                    and self._model_id == model_id and self._ctx == ctx):
                return self.status()
            self._guard_vram(path, ctx)
            self.stop()
            self._start(path, model_id, ctx)
            return self.status()

    def _model_path(self, model_id):
        if self._models is not None:
            entry = self._models.get(model_id)
            if entry:
                return entry["path"]
        raise BackendError("MODEL_NOT_FOUND", "模型 %s 不在 models 目录里" % model_id)

    def _guard_vram(self, path, ctx):
        """装不下就报数字，别让它崩。实测本机 CUDA 附加开销约 1.7GB。"""
        if self._models is None:
            return
        est = self._models.estimate(path, ctx, kv_bits=self._kv_bits())
        gpu = gpu_info()
        if not gpu:
            return
        total, used = gpu["total_mb"], gpu["used_mb"]
        # 引擎是自己独占显存的，占用以「当前总占用」为基线更接近真实
        budget = total - 200
        if est["total_mb"] > budget:
            raise BackendError(
                "VRAM_INSUFFICIENT",
                "装不下：%s 在 %d 上下文下预计占用 %d MiB（权重 %d + KV %d + 附加 %d），"
                "本机共 %d MiB、留 200 MiB 余量后只有 %d MiB 可用。\n"
                "把上下文降到 %s，或在 config.json 里把 kv_cache 设为 q4_0。"
                % (os.path.basename(path), ctx, est["total_mb"], est["weights_mb"],
                   est["kv_mb"], est["overhead_mb"], total, budget,
                   _suggest_ctx(est, budget)))

    def _kv_bits(self):
        return str(self.cfg.get("kv_cache") or "q8_0")

    # ---------------------------------------------------------------- 进程

    def _build_args(self, path, ctx):
        c = self.cfg
        args = [
            self.binary,
            "-m", path,
            "-c", str(ctx),
            "-ngl", str(int(c.get("gpu_layers", 99))),
            "-fa", str(c.get("flash_attn", "on")),
            "--host", str(c.get("host") or "127.0.0.1"),
            "--port", str(int(c.get("port") or 8090)),
            "-np", str(int(c.get("parallel", 1))),
        ]
        kv = self._kv_bits()
        if kv and kv.lower() not in ("f16", "fp16", "off", "none"):
            args += ["-ctk", kv, "-ctv", kv]
        if c.get("jinja", True):
            args.append("--jinja")
        args += list(c.get("extra_args") or [])
        return args

    def _start(self, path, model_id, ctx):
        os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(self._search_paths() + [env.get("PATH", "")])
        # 本地进程不能被系统代理劫持（实测：代理会把本地请求转成 502）
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)

        args = self._build_args(path, ctx)
        _progress("启动引擎：%s" % os.path.basename(path))
        _progress("上下文 %d / KV %s / 层卸载 %s" % (ctx, self._kv_bits(),
                                                  self.cfg.get("gpu_layers", 99)))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        logf = open(self._log_path, "ab", buffering=0)
        logf.write(("\n===== %s 启动 %s ctx=%d =====\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(path), ctx)).encode())
        self._proc = subprocess.Popen(
            args, cwd=self.workdir, env=env, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=flags, stdin=subprocess.DEVNULL,
        )
        self._model_id = model_id
        self._ctx = ctx
        self._wait_health(ctx)

    def _wait_health(self, ctx):
        timeout = float(self.cfg.get("start_timeout") or 180)
        started = time.time()
        while time.time() - started < timeout:
            code = self._proc.poll()
            if code is not None:
                tail = _tail_log(self._log_path, 12)
                raise BackendError(
                    "ENGINE_START_FAILED",
                    "引擎启动即退出（退出码 %s）。日志尾部：\n%s" % (code, tail),
                    detail={"exit_code": code})
            try:
                self._active = self._active  # 保持锁外可见
                conn, resp = self._open("GET", "/models", timeout=2)
                ok = resp.status == 200
                conn.close()
                if ok:
                    _progress("引擎就绪，用时 %.1fs" % (time.time() - started))
                    gpu = gpu_info()
                    if gpu:
                        _progress("显存 %d / %d MiB" % (gpu["used_mb"], gpu["total_mb"]))
                    return
            except BackendError:
                pass
            except Exception:
                pass
            time.sleep(0.5)
        raise BackendError("ENGINE_TIMEOUT",
                           "引擎在 %d 秒内没有就绪。日志：%s\n%s"
                           % (int(timeout), self._log_path, _tail_log(self._log_path, 10)))

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    # ---------------------------------------------------------------- 请求

    def resolve_name(self, model):
        return self._model_id or model or ""

    def chat_stream(self, messages, params=None, model=None):
        with self._lock:
            self._active += 1
        try:
            for chunk in super().chat_stream(messages, params, model):
                yield chunk
        finally:
            with self._lock:
                self._active -= 1

    def chat_once(self, *a, **kw):
        with self._lock:
            self._active += 1
        try:
            return super().chat_once(*a, **kw)
        finally:
            with self._lock:
                self._active -= 1


def _tail_log(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 6000))
            text = f.read().decode("utf-8", "replace")
        return "\n".join([l for l in text.strip().splitlines() if l.strip()][-lines:])
    except Exception:
        return "(无法读取日志)"


def _suggest_ctx(est, budget):
    """按实测比例倒推一个装得下的上下文，别让用户自己试。"""
    per_1k = est.get("kv_per_1k_mb") or 0
    if not per_1k:
        return "更小的值"
    fixed = est["weights_mb"] + est["overhead_mb"]
    room = max(0, budget - fixed)
    tokens = int(room / per_1k * 1024)
    tokens = (tokens // 2048) * 2048
    if tokens < 2048:
        return "更小的值（连 2048 都装不下）"
    return "%d 或更低" % tokens
