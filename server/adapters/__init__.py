"""适配器注册表。

`config.json` 里 `adapter` 指定默认执行器，`adapters_by_kind` 可按模态分别指定：
    {"text": "llamacpp", "image": "sdcpp", "video": "sdcpp"}

这样语言模型和扩散模型可以挂在不同的引擎上，而核心代码始终只认 Adapter 契约。
新增引擎 = 写一个 Adapter 子类 + 在 FACTORIES 里登记一行，别处不用改。
"""
import threading

from .base import Adapter, BackendError, gpu_info
from .llamacpp import LlamaCppAdapter
from .sdcpp import SDCppAdapter
from .openai_http import OpenAIAdapter

FACTORIES = {
    "llamacpp": LlamaCppAdapter,
    "sdcpp": SDCppAdapter,
    "openai": OpenAIAdapter,
}

KINDS = ("text", "image", "video")

_instances = {}          # name -> adapter
_lock = threading.RLock()


def adapter_name_for(kind, cfg):
    """按模态决定用哪个适配器。没配就回落到默认适配器。"""
    if cfg.get("adapter_force"):
        return (cfg.get("adapter") or "llamacpp").strip()
    mapping = cfg.get("adapters_by_kind") or {}
    name = mapping.get(kind or "text")
    if not name and kind in ("image", "video"):
        # 图片/视频没单独配时，不要退回 llamacpp——它跑不了扩散模型
        name = mapping.get("image") or mapping.get("video")
    return (name or cfg.get("adapter") or "llamacpp").strip()


def build(name, cfg, models_mod=None):
    """按名字构造适配器实例。"""
    factory = FACTORIES.get(name)
    if factory is None:
        raise BackendError("BAD_ADAPTER", "未知的执行适配器：%s（可选：%s）"
                           % (name, "、".join(sorted(FACTORIES))))
    sub = (cfg.get("adapters") or {}).get(name) or {}
    merged = dict(sub)
    if name == "llamacpp":
        merged.setdefault("log_file", cfg.get("engine_log") or "logs/engine.log")
    if name == "sdcpp":
        merged.setdefault("log_file", cfg.get("sdcpp_log") or "logs/sdcpp.log")
    if name in ("llamacpp", "sdcpp"):
        return factory(merged, models_mod=models_mod)
    return factory(merged)


def get(name, cfg, models_mod=None):
    """按名字取实例（惰性构造 + 缓存）。"""
    with _lock:
        inst = _instances.get(name)
        if inst is None:
            inst = build(name, cfg, models_mod)
            _instances[name] = inst
        return inst


def for_kind(kind, cfg, models_mod=None):
    """按模态取适配器。这是上层应该用的入口。"""
    return get(adapter_name_for(kind, cfg), cfg, models_mod)


def current(cfg, models_mod=None):
    """默认适配器。兼容旧调用点。"""
    return get((cfg.get("adapter") or "llamacpp").strip(), cfg, models_mod)


def all_instances():
    with _lock:
        return dict(_instances)


def reset():
    """回收所有适配器（停进程），下次访问时重建。"""
    with _lock:
        for inst in list(_instances.values()):
            try:
                inst.stop()
            except Exception:
                pass
        _instances.clear()


def describe_all(cfg):
    """给 doctor 用：列出所有可选适配器及是否可用。"""
    out = []
    active = {adapter_name_for(k, cfg) for k in KINDS}
    for name in sorted(FACTORIES):
        sub = (cfg.get("adapters") or {}).get(name) or {}
        out.append({"name": name, "active": name in active, "config": sub})
    return out
