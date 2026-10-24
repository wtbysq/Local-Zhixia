"""执行层门面。

上层（main.py / context.py / 脚本）只认这个文件，不认任何具体引擎。
真正的实现藏在 adapters/ 里，换引擎不需要动这里，只需改 config.json 的 adapter。
"""
import time

from . import adapters, config, models
from .adapters.base import BackendError, gpu_info


def current():
    return adapters.current(config.CFG, models_mod=models)


def for_kind(kind):
    return adapters.for_kind(kind, config.CFG, models_mod=models)


def reset():
    """配置改了适配器参数后调用，重建实例并回收旧进程。"""
    adapters.reset()


# ---------------------------------------------------------------- 引擎

def status():
    try:
        return current().status()
    except BackendError as exc:
        return {"adapter": config.CFG.get("adapter"), "error": {"code": exc.code,
                                                                "message": exc.message}}


def ensure_ready(model_id, ctx):
    """对话前确保引擎就绪。managed 适配器在这里拉起/切换模型。"""
    return current().ensure(model_id, ctx)


def stop_engine():
    try:
        current().stop()
    except Exception:
        pass


def engine_models():
    """引擎自报的模型列表。连不上就返回空，不抛错。"""
    try:
        return current().list_models()
    except BackendError:
        return []


# ---------------------------------------------------------------- 资产

def list_models(ctx=None, kv_bits=None):
    """L0 登记表 + 当前上下文下的显存估算。"""
    ctx = int(ctx or config.CFG.get("ctx_max") or 8192)
    kv = kv_bits or (config.adapter_cfg().get("kv_cache") or "q8_0")
    return [models.decorate(e, ctx, kv) for e in models.scan()]


def get_model(model_id):
    return models.get(model_id)


def validate_model(model_id):
    """managed 适配器的模型必须来自 models 目录；外部端点由它自己负责。"""
    if not model_id:
        return False, "未指定模型"
    if not current().managed:
        return True, ""
    if models.get(model_id):
        return True, ""
    names = [e["id"] for e in models.scan()]
    return False, "模型 %s 不在 models 目录里。可用：%s" % (
        model_id, "、".join(names) if names else "（空）")


# ---------------------------------------------------------------- 推理

def chat_once(messages, temperature=0.2, max_tokens=600, timeout=None, model=None):
    return current().chat_once(messages, temperature=temperature, max_tokens=max_tokens,
                               timeout=timeout or config.CFG["summary_timeout"], model=model)


def chat_stream(messages, params=None, model=None):
    return current().chat_stream(messages, params=params, model=model)


# ---------------------------------------------------------------- 生成（图片 / 视频）

def supports_generate(kind):
    return kind in ("image", "video")


def generate(model_id, req, out_path, on_event=None):
    """跑一次图片/视频生成。

    8GB 显存装不下两套权重，所以这里做显存互斥：先把语言引擎停掉，
    等显存真的释放出来，再起扩散引擎。语言模型下次对话会自动重新加载。
    """
    entry = models.get(model_id)
    if not entry:
        raise BackendError("MODEL_NOT_FOUND", "模型 %s 不在 models 目录里" % model_id)
    kind = entry.get("kind")
    if not supports_generate(kind):
        raise BackendError("WRONG_KIND",
                           "%s 是%s模型，不能用来生成图片或视频" % (model_id, kind or "未知类型"))

    adapter = for_kind(kind)
    if not hasattr(adapter, "generate"):
        raise BackendError("NOT_SUPPORTED",
                           "当前执行器 %s 不支持生成。请在 config.json 的 "
                           "adapters_by_kind 里把 %s 指向 sdcpp" % (adapter.name, kind))

    released = release_vram_except(adapter.name)
    return adapter.generate(model_id, req, out_path, on_event=on_event)


def release_vram_except(keep_name, need_mb=512, timeout=40):
    """停掉 keep_name 之外的所有托管引擎，并等显存回落。返回释放说明列表。"""
    freed = []
    for name, inst in adapters.all_instances().items():
        if name == keep_name or not getattr(inst, "managed", False):
            continue
        st = inst.status() if hasattr(inst, "status") else {}
        if st.get("engine_running"):
            inst.stop()
            freed.append(name)
    if not freed:
        return freed
    deadline = time.time() + timeout
    while time.time() < deadline:
        gpu = gpu_info(force=True)
        if not gpu or gpu["free_mb"] >= need_mb:
            break
        time.sleep(0.6)
    return freed


def cancel_generate(kind):
    try:
        inst = for_kind(kind)
        return bool(getattr(inst, "cancel", lambda: False)())
    except Exception:
        return False



# ---------------------------------------------------------------- 健康

def health():
    started = time.time()
    st = status()
    engine_ok = None
    err = st.get("error")
    if current().managed:
        engine_ok = bool(st.get("engine_running"))
        if not engine_ok and not err:
            err = {"code": "ENGINE_NOT_LOADED",
                   "message": "引擎尚未加载模型，首次对话时会自动启动"}
    else:
        try:
            current().list_models()
            engine_ok = True
        except BackendError as exc:
            engine_ok = False
            err = {"code": exc.code, "message": exc.message}
    return {
        "backend_ok": bool(engine_ok),
        "adapter": st.get("adapter"),
        "engine": st,
        "base": st.get("base"),
        "model": st.get("model"),
        "models": list_models(),
        "error": err,
        "latency_ms": int((time.time() - started) * 1000),
        "gpu": gpu_info(),
        "ctx_max": config.CFG["ctx_max"],
        "ctx_ceiling": config.CFG["ctx_ceiling"],
        "snapshot_level": (config.CFG.get("snapshot") or {}).get("level"),
    }
