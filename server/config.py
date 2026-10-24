"""配置加载：默认值 < config.json < 环境变量。

默认值全部来自本机实测，不是拍的：
- ctx_max 8192：16k + KV q8_0 实测吃 7656/8188 MiB，只剩 532 MiB，太险
- ctx_ceiling 16384：实测能装下的上限，再高必然溢出
- vram_overhead_mb 1700：权重与 KV 之外的实测开销（CUDA 上下文、计算缓冲、桌面合成）
"""
import json
import os
import sys


def _base_dir():
    """项目基准目录。

    打包后 __file__ 指向 PyInstaller 的临时解压目录（sys._MEIPASS），
    而 runtime/、models/、web/ 都在 exe 旁边。必须用 exe 所在目录当基准，
    否则打包后会出现「模型找不到、页面 404」这类看似莫名其妙的错误。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = _base_dir()


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _fallback_base():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "LocalModelWorkbench")


def _res(name):
    """定位随程序分发的资源。

    开发时资源就在项目根；打包后 PyInstaller 会把它们塞进 _internal，
    而 sys._MEIPASS 正指向那里。先找 exe 旁（允许用户覆盖），再找打包内部。
    """
    local = os.path.join(ROOT, name)
    if os.path.exists(local):
        return local
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        packed = os.path.join(meipass, name)
        if os.path.exists(packed):
            return packed
    return local


# 便携优先：数据放 exe 旁边，拷走目录即完成迁移。
# 若不可写（典型是装在 Program Files），自动退到 LOCALAPPDATA，避免一启动就崩。
DATA_DIR = os.path.join(ROOT, "data") if _writable(os.path.join(ROOT, "data")) \
    else os.path.join(_fallback_base(), "data")
LOG_DIR = os.path.join(ROOT, "logs") if _writable(os.path.join(ROOT, "logs")) \
    else os.path.join(_fallback_base(), "logs")
WEB_DIR = _res("web")
CONFIG_PATH = _res("config.json")
DB_PATH = os.path.join(DATA_DIR, "app.db")
# 生成产物分两个目录存放，用户打开 data 就能一眼找到自己的图/片子。
# 目录名用中文：实测引擎能正确写入中文路径（Windows 下走 UTF-16 参数），
# 但 URL 里用英文 image/video，避免百分号编码在各级代理里出岔子。
GEN_IMAGE_DIR = os.path.join(DATA_DIR, "图片")
GEN_VIDEO_DIR = os.path.join(DATA_DIR, "视频")


def gen_dir(kind):
    """按类型给出产物目录。未知类型一律当图片。"""
    return GEN_VIDEO_DIR if kind == "video" else GEN_IMAGE_DIR

DEFAULTS = {
    # ---- 执行层：选哪个适配器，以及各适配器自己的参数 ----
    "adapter": "llamacpp",
    # 按模态分派：语言走 llama.cpp，图片/视频走 stable-diffusion.cpp
    "adapters_by_kind": {"text": "llamacpp", "image": "sdcpp", "video": "sdcpp"},
    "adapters": {
        "llamacpp": {
            "binary": "",
            "vendor_dir": "",
            "host": "127.0.0.1",
            "port": 8090,
            "gpu_layers": 99,
            "flash_attn": "on",
            "kv_cache": "q8_0",
            "parallel": 1,
            "jinja": True,
            "start_timeout": 180,
            "chat_template_kwargs": {"enable_thinking": False},
            "extra_args": [],
        },
        "sdcpp": {
            "binary": "",
            "backend": "",
            "params_backend": "",
            "auto_fit": True,
            "extra_args": [],
        },
        "openai": {
            "base": "http://127.0.0.1:8080/v1",
        },
    },
    # ---- 资产层 ----
    "models_dir": "models",
    "vram_overhead_mb": 1700,
    # 视频产物容器。引擎支持 webm / webp（动图）/ avi 三种单文件输出：
    #   webm —— 真正的视频文件，播放器、剪辑软件、浏览器都认
    #   webp —— 动画 WebP，本质是"会动的图片"，体积小但很多软件当静态图处理
    #   avi  —— 兼容性最广，文件最大
    "video_format": "webm",
    # ---- 后端监听：只绑回环 ----
    "host": "127.0.0.1",
    "port": 8787,
    # ---- 上下文 ----
    "ctx_max": 8192,
    "ctx_ceiling": 16384,
    "max_tokens": 1024,
    "safety_margin": 256,
    # ---- 自动压缩 ----
    "compact_enabled": True,
    "trigger_ratio": 0.75,
    "target_ratio": 0.45,
    "keep_recent_turns": 4,
    "min_turns": 3,
    "max_batch_turns": 12,
    "summary_cap": 600,
    "summary_timeout": 20,
    # ---- 采样默认值 ----
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "repeat_penalty": 1.05,
    "enable_thinking": False,
    # 新会话默认用哪个模型；留空 = models 里第一个装得下的
    "default_model": "",
    # ---- 快照密度：off / basic / full ----
    "snapshot": {"level": "basic"},
    # ---- 日志 ----
    "log_requests": True,
    "engine_log": "logs/engine.log",
}

_ENV_MAP = {
    "adapter": "ADAPTER",
    "models_dir": "MODELS_DIR",
    "host": "HOST",
    "port": "PORT",
    "ctx_max": "CTX_MAX",
    "ctx_ceiling": "CTX_CEILING",
    "compact_enabled": "COMPACT_ENABLED",
    "vram_overhead_mb": "VRAM_OVERHEAD_MB",
}


def _merge_deep(base, over):
    """适配器参数是嵌套 dict，浅合并会把 adapters.llamacpp 整个覆盖掉。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_deep(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))   # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = _merge_deep(cfg, json.load(f) or {})
        except Exception as exc:
            print("[config] 读取 config.json 失败，使用默认值：%s" % exc)
    for key, env in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        if isinstance(DEFAULTS[key], bool):
            cfg[key] = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(DEFAULTS[key], int):
            try:
                cfg[key] = int(raw)
            except ValueError:
                pass
        else:
            cfg[key] = raw
    cfg["port"] = int(cfg["port"])
    # 兼容旧变量名：LLM_BASE 现在等价于「用 openai 适配器 + 这个地址」
    lb = os.environ.get("LLM_BASE")
    if lb:
        cfg.setdefault("adapters", {}).setdefault("openai", {})["base"] = lb
    cfg["ctx_ceiling"] = int(cfg.get("ctx_ceiling") or DEFAULTS["ctx_ceiling"])
    if int(cfg["ctx_max"]) > cfg["ctx_ceiling"]:
        cfg["ctx_max"] = cfg["ctx_ceiling"]
    # ADAPTER 环境变量表示「强制所有模态都用这个执行器」。
    # 测试与调试要的正是这个：不管 adapters_by_kind 怎么配，一律走同一个。
    if os.environ.get("ADAPTER"):
        cfg["adapter_force"] = True
    return cfg


CFG = load_config()


def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR, GEN_IMAGE_DIR, GEN_VIDEO_DIR):
        os.makedirs(d, exist_ok=True)


def adapter_cfg(name=None, kind=None):
    if kind:
        from . import adapters as _a
        name = _a.adapter_name_for(kind, CFG)
    name = name or CFG.get("adapter") or "llamacpp"
    return (CFG.get("adapters") or {}).get(name) or {}


# 允许被界面修改、并且会持久化到数据库的键。
# 不在这个集合里的键，前端传上来也不会生效——这是有意的白名单。
PERSIST_KEYS = ("ctx_max", "max_tokens", "temperature", "top_p", "top_k",
                "compact_enabled", "trigger_ratio", "target_ratio",
                "keep_recent_turns", "enable_thinking", "snapshot_level",
                "default_model")


def set_runtime(key, value):
    """应用一条设置，顺便把类型收口。返回是否被接受。

    类型转换集中在这里，而不是散落在路由里：写错的配置不应该变成运行时的 TypeError。
    """
    if key == "snapshot_level":
        v = str(value).lower()
        if v not in ("off", "basic", "full"):
            return False
        CFG.setdefault("snapshot", {})["level"] = v
        return True
    if key not in PERSIST_KEYS:
        return False
    try:
        if key in ("compact_enabled", "enable_thinking"):
            CFG[key] = bool(value)
        elif key in ("ctx_max", "max_tokens", "keep_recent_turns", "top_k"):
            CFG[key] = int(value)
            if key == "ctx_max":
                CFG[key] = min(CFG[key], int(CFG.get("ctx_ceiling") or 16384))
        elif key in ("temperature", "top_p", "trigger_ratio", "target_ratio"):
            CFG[key] = float(value)
        else:
            CFG[key] = str(value)
    except (TypeError, ValueError):
        return False
    return True


def get_runtime(key):
    """取当前值，用于持久化。"""
    if key == "snapshot_level":
        return (CFG.get("snapshot") or {}).get("level", "basic")
    return CFG.get(key)
