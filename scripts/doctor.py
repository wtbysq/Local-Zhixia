"""启动自检：把每一种「起不来」变成一条带结论的检查。

用法：
    python scripts/doctor.py            # 完整报告
    python scripts/doctor.py --brief    # 只报失败的项（给批处理调用）

检查顺序就是从启动顺序来的：配置 → 引擎二进制 → 模型 → 端口 → 后端 → 代理。
任何一项失败，后面的都会连带失败，所以报告按依赖顺序排，第一条红的就是根因。
"""
import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import config, models  # noqa: E402

OK, WARN, BAD = "OK", "WARN", "FAIL"
_rows = []
BRIEF = False


def row(level, name, detail, fix=""):
    _rows.append((level, name, detail, fix))


def _get(url, timeout=3):
    """本地请求必须绕开系统代理，否则会被代理拦截成 502（真踩过）。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return None, str(exc.reason)
    except socket.timeout:
        return None, "超时"
    except Exception as exc:
        return None, str(exc)


def _port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def check_python():
    v = sys.version_info
    if v >= (3, 9):
        row(OK, "Python 版本", "%d.%d.%d" % (v[0], v[1], v[2]))
    else:
        row(BAD, "Python 版本", "%d.%d.%d 过低" % (v[0], v[1], v[2]),
            "升级到 3.9 以上")


def check_config():
    from server import adapters as _ad
    mapping = config.CFG.get("adapters_by_kind") or {}
    parts = []
    for kind, label in (("text", "语言"), ("image", "图片"), ("video", "视频")):
        nm = _ad.adapter_name_for(kind, config.CFG)
        cfg = (config.CFG.get("adapters") or {}).get(nm) or {}
        if nm == "openai" and cfg.get("base"):
            parts.append("%s→openai(%s)" % (label, cfg["base"].replace("http://", "")))
        else:
            parts.append("%s→%s" % (label, nm))
    row(OK, "执行适配器", " / ".join(parts))
    for kind in ("text", "image", "video"):
        nm = _ad.adapter_name_for(kind, config.CFG)
        if nm not in ("llamacpp", "sdcpp", "openai"):
            row(BAD, "适配器配置", "%s 指向未知适配器 %s" % (kind, nm),
                "改成 llamacpp / sdcpp / openai")


def check_diffusion():
    """扩散引擎（图片/视频）单独查：它和语言引擎是两套二进制。"""
    from server import adapters as _ad
    name = _ad.adapter_name_for("image", config.CFG)
    if name != "sdcpp":
        return
    cfg = config.adapter_cfg("sdcpp")
    binary = cfg.get("binary") or ""
    if not binary:
        row(BAD, "扩散引擎", "adapters.sdcpp.binary 为空",
            "填 runtime\\sdcpp\\sd-cli.exe，或留空使用默认路径")
        return
    if not os.path.isabs(binary):
        binary = os.path.join(config.ROOT, binary)
    if not os.path.isfile(binary):
        row(BAD, "扩散引擎", "不存在：%s" % binary,
            "从 stable-diffusion.cpp 的 Release 解压到 runtime/sdcpp/")
        return
    workdir = os.path.dirname(binary)
    dlls = [f for f in os.listdir(workdir) if f.lower().endswith(".dll")]
    ggml = [f for f in dlls if f.lower().startswith(("ggml", "cudart", "cublas", "vulkan"))]
    if not ggml:
        row(BAD, "扩散引擎依赖", "%s 里没有 ggml*.dll" % os.path.basename(workdir),
            "二进制必须和它的 dll 同目录")
    else:
        row(OK, "扩散引擎", "%s（%d 个运行库）" % (os.path.basename(binary), len(ggml)))


def check_engine_binary():
    if config.CFG.get("adapter") != "llamacpp":
        return
    a = config.adapter_cfg("llamacpp")
    binary = a.get("binary") or ""
    if not binary:
        row(BAD, "引擎二进制", "config.json 里 adapters.llamacpp.binary 为空",
            "填入 llama-server.exe 的完整路径")
        return
    if not os.path.isfile(binary):
        row(BAD, "引擎二进制", "不存在：%s" % binary,
            "检查路径；或把 llama.cpp 的发布包解压后指过去")
        return
    row(OK, "引擎二进制", os.path.basename(binary))

    # DLL 缺失是 0xC0000135 静默退出的根因，必须单独查
    workdir = os.path.dirname(binary)
    files = [f.lower() for f in os.listdir(workdir)]
    has_ggml = any(f.startswith("ggml") for f in files)
    cuda_here = [f for f in files if f.startswith(("cudart", "cublas"))]
    vendor = a.get("vendor_dir")
    if not vendor:
        backends = os.path.dirname(workdir)
        vroot = os.path.join(backends, "vendor")
        if os.path.isdir(vroot):
            for n in sorted(os.listdir(vroot)):
                d = os.path.join(vroot, n)
                if os.path.isdir(d) and any(f.lower().startswith("cudart")
                                            for f in os.listdir(d)):
                    vendor = d
                    break
    if not has_ggml:
        row(BAD, "引擎依赖库", "同目录下没有 ggml*.dll",
            "二进制必须和它的 dll 放在同一目录")
    elif cuda_here:
        row(OK, "CUDA 运行库", "与二进制同目录（%d 个）" % len(cuda_here))
    elif vendor and os.path.isdir(vendor):
        row(OK, "CUDA 运行库", os.path.basename(vendor))
    else:
        row(WARN, "CUDA 运行库", "同目录与 vendor 里都没找到 cudart/cublas",
            "若引擎启动即退出（0xC0000135），把 CUDA 运行库 dll 放到二进制同目录")


def check_models():
    from server.adapters.base import gpu_info
    entries = models.scan()
    if not entries:
        row(BAD, "模型目录", "%s 里没有模型文件" % models.models_dir(),
            "把 .gguf / .safetensors 放进该目录；本程序不负责下载")
        return

    counts = {"text": 0, "image": 0, "video": 0}
    for e in entries:
        counts[e.get("kind") or "text"] = counts.get(e.get("kind") or "text", 0) + 1
    row(OK, "模型目录", "语言 %d · 图片 %d · 视频 %d"
        % (counts["text"], counts["image"], counts["video"]))

    gpu = gpu_info()
    ctx = int(config.CFG.get("ctx_max") or 8192)
    kv = (config.adapter_cfg("llamacpp") or {}).get("kv_cache") or "q8_0"
    for e in entries:
        kind = e.get("kind") or "text"
        if e.get("missing"):
            row(BAD, e["id"], "缺少组件：%s" % "、".join(e["missing"]),
                "把缺少的文件放进 %s" % e["path"])
            continue
        if e.get("bundle") or kind in ("image", "video"):
            d = models.decorate(e, ctx, kv)
            est = d["est"]
            if gpu and est["total_mb"] > gpu["total_mb"] - 200:
                row(WARN, e["id"], "预估 %d MiB（权重 %d + 缓冲 %d），8GB 上偏紧"
                    % (est["total_mb"], est["weights_mb"], est["overhead_mb"]),
                    "把分辨率或帧数降一档；生成前会先卸载语言模型")
            else:
                row(OK, e["id"], "%s · %.1f GB，预估 %d MiB"
                    % ("视频" if kind == "video" else "图片", e.get("size_gb") or 0,
                       est["total_mb"]))
            continue
        est = models.estimate(e["path"], ctx, kv)
        if not est["exact"]:
            row(WARN, e["id"], "元数据解析失败，无法估算显存", e.get("meta_error") or "")
            continue
        if gpu and est["total_mb"] > gpu["total_mb"] - 200:
            row(BAD, e["id"], "ctx%d 需 %d MiB，超出可用显存" % (ctx, est["total_mb"]),
                "把 ctx_max 降到 4096，或改 kv_cache 为 q4_0")
        else:
            row(OK, e["id"], "%d 层 / %d KV 头 / %s，ctx%d ≈ %d MiB"
                % (e["n_layer"], e["n_head_kv"], e.get("quant") or "?",
                   ctx, est["total_mb"]))


def check_ports():
    bport = int(config.CFG.get("port") or 8787)
    if _port_open(bport):
        row(OK, "后端端口 %d" % bport, "已监听")
    else:
        row(WARN, "后端端口 %d" % bport, "没有监听",
            "双击「启动.bat」，或执行 python server/main.py")

    if config.CFG.get("adapter") == "llamacpp":
        eport = int((config.adapter_cfg("llamacpp") or {}).get("port") or 8090)
        if _port_open(eport):
            row(OK, "引擎端口 %d" % eport, "已监听（引擎在跑）")
        else:
            row(OK, "引擎端口 %d" % eport, "空闲（首次对话时自动加载，正常）")


def check_backend():
    port = int(config.CFG.get("port") or 8787)
    status, body = _get("http://127.0.0.1:%d/api/health" % port)
    if status != 200:
        row(WARN, "后端健康检查", "无法访问：%s" % str(body)[:60], "先启动后端")
        return
    try:
        h = json.loads(body)
    except Exception:
        row(BAD, "后端健康检查", "返回内容无法解析", "")
        return
    eng = h.get("engine") or {}
    if h.get("backend_ok"):
        row(OK, "引擎连通", "模型 %s" % (eng.get("model") or "-"))
    elif eng.get("managed") and eng.get("engine_running") is False:
        row(OK, "引擎连通", "未加载（懒加载，首次对话时启动）")
    else:
        err = h.get("error") or {}
        row(BAD, "引擎连通", "%s：%s" % (err.get("code"), str(err.get("message"))[:70]),
            "看 logs/engine.log 与 logs/app.log")


def check_proxy():
    bad = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
           if os.environ.get(k)]
    if bad:
        row(WARN, "代理环境变量", "检测到 %s" % "、".join(bad),
            "本程序内部已绕开代理；但你在命令行用 curl 测试时要加 --noproxy \"*\"")
    else:
        row(OK, "代理环境变量", "无")


def check_dirs():
    for d, name in ((config.DATA_DIR, "数据目录"), (config.LOG_DIR, "日志目录")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            row(OK, name, d)
        except Exception as exc:
            row(BAD, name, "不可写：%s" % exc, "检查权限")


def render():
    if BRIEF:
        bad = [r for r in _rows if r[0] != OK]
        for level, name, detail, fix in bad:
            print("[%s] %s：%s" % (level, name, detail))
        return 0 if not bad else 1

    print("=" * 66)
    print(" 启动自检  ·  %s" % time_str())
    print("=" * 66)
    width = max(len(r[1]) for r in _rows) if _rows else 10
    for level, name, detail, fix in _rows:
        mark = {OK: "[OK]  ", WARN: "[!]   ", BAD: "[X]   "}[level]
        print("%s%-*s  %s" % (mark, width + 2, name, detail))
        if fix and level != OK:
            print("        %s→ %s" % (" " * (width + 2), fix))
    print("-" * 66)
    bad = [r for r in _rows if r[0] == BAD]
    warn = [r for r in _rows if r[0] == WARN]
    if bad:
        print(" 结论：%d 项必须修复（上面标 [X] 的）。修完重跑本脚本。" % len(bad))
    elif warn:
        print(" 结论：可以启动，但有 %d 项提示（[!]）。" % len(warn))
    else:
        print(" 结论：全链路就绪。打开 http://%s:%d 即可对话。"
              % (config.CFG.get("host"), int(config.CFG.get("port"))))
    print("=" * 66)
    return 0 if not bad else 1


def time_str():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main():
    global BRIEF
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    args = ap.parse_args()
    BRIEF = args.brief

    check_python()
    check_config()
    check_engine_binary()
    check_diffusion()
    check_models()
    check_ports()
    check_backend()
    check_proxy()
    check_dirs()
    return render()


if __name__ == "__main__":
    sys.exit(main())
