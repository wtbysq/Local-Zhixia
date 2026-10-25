"""模型资产层（L0）：只登记，不下载。

职责边界（对应设计文档第 5 节）：
- 扫描 models/ 目录，解析 GGUF 头部拿真实结构参数（层数 / KV 头 / head_dim / 原生上下文）
- 按实测公式估算显存，装不下就提前说，而不是等引擎崩
- 结果缓存到 data/models.json，文件 mtime 变化才重解析

显存公式的常数来自本机实测，不是猜的：
  16k 上下文 + KV q8_0 + Q4_K_M 权重 → 实测 7656 MiB
  权重 4794 + KV 1152 + 附加 1710 = 7656  ✔ 误差 < 10 MiB
"""
import json
import os
import struct
import time

from . import config

# GGUF 量化枚举 → 名字（只列常见的）
_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
}

_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
           6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}

_WANT = ("general.architecture", "general.name", "general.file_type",
         # 纯注意力架构（Qwen3 / Llama / Gemma3）
         "qwen3.block_count", "qwen3.attention.head_count",
         "qwen3.attention.head_count_kv", "qwen3.embedding_length",
         "qwen3.context_length", "llama.block_count", "llama.attention.head_count",
         "llama.attention.head_count_kv", "llama.embedding_length",
         "llama.context_length", "gemma3.block_count", "gemma3.attention.head_count",
         "gemma3.attention.head_count_kv", "gemma3.embedding_length",
         "gemma3.context_length",
         # 混合注意力（Qwen3.5 / Ornith：Gated DeltaNet + 每 N 层一个全注意力层）
         "qwen35.block_count", "qwen35.attention.head_count",
         "qwen35.attention.head_count_kv", "qwen35.embedding_length",
         "qwen35.context_length", "qwen35.attention.key_length",
         "qwen35.attention.value_length", "qwen35.full_attention_interval")


def models_dir():
    d = config.CFG.get("models_dir") or "models"
    return d if os.path.isabs(d) else os.path.join(config.ROOT, d)


def path_of(model_id):
    entry = get(model_id)
    return entry["path"] if entry else None


# ---------------------------------------------------------------- 模型类型
#
# 分类只做「登记与检索」，不做推理：文本走 llamacpp，图片/视频等引擎接进来再说。
# 判断顺序：目录名 > 文件名关键词 > 扩展名。宁可分错也能手动改 data/models.json，
# 好过让用户面对一个永远空的分类。

KINDS = ("text", "image", "video")

_VIDEO_HINTS = ("h3", "hailuo", "wan2", "t2v", "i2v", "svd", "cogvideo",
                "animate", "ltx", "video", "hunyuanvideo", "mochi")
_IMAGE_HINTS = ("sd15", "sd21", "sdxl", "sd3", "flux", "kolors", "playground",
                "diffusion", "image")
_DIFF_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")


def infer_kind(path):
    rel = os.path.relpath(path, models_dir()).replace("\\", "/").lower()
    parts = rel.split("/")[:-1]
    name = os.path.basename(rel)

    for seg in parts:                       # 目录名优先：models/video/xxx
        if seg in ("video", "videos"):
            return "video"
        if seg in ("image", "images", "picture", "pictures"):
            return "image"
        if seg in ("text", "llm", "language"):
            return "text"

    if name.endswith(".gguf"):
        return "text"                       # GGUF 目前只用于语言模型

    if name.endswith(_DIFF_EXT):
        if any(h in name for h in _VIDEO_HINTS):
            return "video"
        if any(h in name for h in _IMAGE_HINTS):
            return "image"
        return "image"                      # 扩散权重默认按图片归类

    return "text"


# ---------------------------------------------------------------- GGUF 解析

def _read_meta(path, limit_keys=None):
    """流式解析 GGUF 头部。数组按元素类型批量跳过，避免逐项读 15 万词表。"""
    out = {}
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        version = struct.unpack("<I", f.read(4))[0]
        f.read(8)                                   # tensor count
        n_kv = struct.unpack("<Q", f.read(8))[0]
        if version < 2 or n_kv > 100000:
            raise ValueError("GGUF 版本或元数据异常")

        def rd(n):
            return f.read(n)

        def u32():
            return struct.unpack("<I", rd(4))[0]

        def u64():
            return struct.unpack("<Q", rd(8))[0]

        def s_():
            n = u64()
            return rd(n).decode("utf-8", "replace")

        def skip(t):
            """读完并丢弃一个值，返回它（标量才有意义）。"""
            if t == 8:
                return s_()
            if t == 9:
                et = u32()
                n = u64()
                if et in _SCALAR:
                    f.seek(struct.calcsize(_SCALAR[et]) * n, os.SEEK_CUR)
                elif et == 8:
                    for _ in range(n):
                        s_()
                elif et == 9:
                    for _ in range(n):
                        skip(9)
                else:
                    return None
                return "<array:%d>" % n
            fmt = _SCALAR.get(t)
            if not fmt:
                raise ValueError("未知值类型 %s" % t)
            return struct.unpack(fmt, rd(struct.calcsize(fmt)))[0]

        for _ in range(n_kv):
            key = s_()
            vtype = u32()
            if limit_keys and key not in limit_keys:
                skip(vtype)
                continue
            out[key] = skip(vtype)
    return out


def _read_safetensors_meta(path):
    """safetensors 的头部是一段 JSON：前 8 字节是长度，后面是张量表。

    扩散模型没有 block_count / KV 头这些字段，所以只取能以证实的部分：
    架构名与张量数量。显存估算交给 decorate 按权重比例粗算。
    """
    import json as _json
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) < 8:
            raise ValueError("文件过短")
        n = struct.unpack("<Q", head)[0]
        if n <= 0 or n > 200 * 1024 * 1024:
            raise ValueError("头部长度异常：%s" % n)
        raw = f.read(n)
    j = _json.loads(raw.decode("utf-8"))
    meta = j.get("__metadata__") or {}
    keys = [k for k in j.keys() if k != "__metadata__"]
    return {
        "arch": meta.get("modelspec.architecture"),
        "name": meta.get("modelspec.title"),
        "tensor_count": len(keys),
        "quant": None,
    }


def _shape_from_meta(meta):
    arch = (meta.get("general.architecture") or "").lower()

    def pick(suffix):
        return meta.get("%s.%s" % (arch, suffix))

    n_layer = pick("block_count")
    n_head = pick("attention.head_count")
    n_head_kv = pick("attention.head_count_kv")
    n_embd = pick("embedding_length")
    n_ctx = pick("context_length")
    if isinstance(n_head_kv, list):
        n_head_kv = max(n_head_kv) if n_head_kv else None
    head_dim = int(n_embd / n_head) if (n_embd and n_head) else None
    # 混合架构里只有"全注意力层"留 KV，其余层是常数状态的 SSM/线性注意力。
    # Qwen3.5 / Ornith 用 full_attention_interval 标出每隔几层一个全注意力层。
    interval = pick("full_attention_interval")
    kv_layers = n_layer
    if n_layer and interval and int(interval) > 1:
        kv_layers = int((int(n_layer) + int(interval) - 1) // int(interval))
    key_len = pick("attention.key_length") or pick("attention.value_length")
    if key_len and n_head_kv:
        head_dim = int(key_len)          # Qwen3.5 的 head_dim 记在 key_length 里
    return {
        "arch": meta.get("general.architecture"),
        "name": meta.get("general.name"),
        "n_layer": n_layer, "n_head": n_head, "n_head_kv": n_head_kv,
        "kv_layers": kv_layers, "n_embd": n_embd, "head_dim": head_dim,
        "n_ctx_train": n_ctx,
        "quant": _FILE_TYPES.get(meta.get("general.file_type"), None),
        "schema": 2,
    }


# ---------------------------------------------------------------- 缓存与扫描

_CACHE_PATH = os.path.join(config.DATA_DIR, "models.json")


def _load_cache():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_cache(cache):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


def _entry(path, cache):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = os.path.abspath(path)
    hit = cache.get(key)
    # 缓存里存的是绝对路径。整个目录被搬走（换盘 / 换用户名）后旧路径会失效，
    # 所以命中前先确认文件真在，不在就当场重新解析。
    if hit and hit.get("schema") == 2 and hit.get("mtime") == int(st.st_mtime) \
            and hit.get("size") == st.st_size and os.path.isfile(hit.get("path") or ""):
        e = dict(hit)
    else:
        e = {"path": key, "mtime": int(st.st_mtime), "size": st.st_size}
        ext = os.path.splitext(path)[1].lower()
        if ext == ".gguf":
            try:
                meta = _read_meta(path, limit_keys=set(_WANT))
                e.update(_shape_from_meta(meta))
                e["meta_ok"] = True
            except Exception as exc:
                e["meta_ok"] = False
                e["meta_error"] = str(exc)[:120]
        elif ext == ".safetensors":
            try:
                e.update(_read_safetensors_meta(path))
                e["meta_ok"] = True
            except Exception as exc:
                e["meta_ok"] = False
                e["meta_error"] = str(exc)[:120]
        else:
            # .ckpt/.pth 需要 pickle 才能读，不在这里冒险解析
            e["meta_ok"] = False
        cache[key] = e
    e["id"] = os.path.splitext(os.path.basename(path))[0]
    e["file"] = os.path.basename(path)
    e["size_gb"] = round(st.st_size / (1024 ** 3), 2)
    e["weights_mb"] = int(st.st_size / (1024 ** 2))
    e["kind"] = e.get("kind_override") or infer_kind(path)
    return e


# 语言模型是 GGUF；扩散模型常用 safetensors / ckpt
_MODEL_EXTS = (".gguf", ".safetensors", ".ckpt", ".pth", ".bin")

BUNDLE_FILE = "bundle.json"
COMPONENTS_DIR = "components"


def _walk(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        # components/ 是共享组件池：里面的 VAE、文本编码器属于某个模型，
        # 不是独立模型，不能摆到界面上。
        if dirpath == root and COMPONENTS_DIR in dirs:
            dirs.remove(COMPONENTS_DIR)
        for fn in files:
            if fn.lower().endswith(_MODEL_EXTS):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


# bundle.json 里 components 的键 → components/ 下的池子目录。
# 未列出的键只在模型目录内找，不猜位置。
_ROLE_POOL = {
    "diffusion": "diffusion",
    "model": "diffusion",
    "vae": "vae",
    "audio_vae": "audio_vae",
    "llm": "text_encoders",
    "llm_vision": "text_encoders",
    "t5xxl": "text_encoders",
    "clip_l": "text_encoders",
    "clip_g": "text_encoders",
}


def _resolve_component(folder, role, name):
    """组件查找顺序：显式路径 → 模型目录内 → components/<池子>/。

    池子让多个模型复用同一份 VAE / 文本编码器（anima 与 krea2 共用
    qwen_image_vae 就是例子）。模型目录内优先，则保证「把整个模型目录
    单独拷给别人」的老布局照样能跑。
    """
    if not name:
        return None
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    if os.sep in name or "/" in name:          # 显式相对路径，按路径解析
        p = os.path.normpath(os.path.join(folder, name.replace("/", os.sep)))
        return p if os.path.isfile(p) else None
    p = os.path.join(folder, name)             # 1) 模型目录内
    if os.path.isfile(p):
        return p
    pool = _ROLE_POOL.get(role)                # 2) 共享池
    if pool:
        p = os.path.join(models_dir(), COMPONENTS_DIR, pool, name)
        if os.path.isfile(p):
            return p
    return None


def _bundle_entry(folder):
    """一个扩散模型往往由多个文件组成（transformer + VAE + 文本编码器）。

    这种目录里放一个 bundle.json，整个目录登记为一个模型，
    避免把组件当成三个独立模型摆到界面上。
    """
    try:
        with open(os.path.join(folder, BUNDLE_FILE), "r", encoding="utf-8") as f:
            meta = json.load(f) or {}
    except Exception:
        return None
    comps = {}
    total = 0
    folder_abs = os.path.abspath(folder)
    for key, name in (meta.get("components") or {}).items():
        p = _resolve_component(folder, key, name)
        if p:
            shared = os.path.dirname(os.path.abspath(p)) != folder_abs
            comps[key] = {"file": name, "path": p,
                          "size_gb": round(os.path.getsize(p) / 1024 ** 3, 2),
                          "shared": shared}
            total += os.path.getsize(p)
        else:
            comps[key] = {"file": name, "missing": True}
    name = os.path.basename(folder.rstrip("\\/"))
    return {
        "id": name,
        "file": name,
        "path": os.path.abspath(folder),
        "bundle": True,
        "kind": meta.get("kind") or infer_kind(folder + os.sep),
        "pipeline": meta.get("pipeline"),
        "params": meta.get("params") or {},
        "components": comps,
        "missing": [k for k, v in comps.items() if v.get("missing")],
        "size_gb": round(total / 1024 ** 3, 2),
        "weights_mb": int(total / 1024 ** 2),
        "quant": meta.get("quant"),
        "extra_args": meta.get("extra_args") or [],
        "meta_ok": True,
    }


def scan(force=False):
    """扫描 models 目录，返回登记表。

    两类条目：组件包（含 bundle.json 的目录）与零散模型文件。
    """
    root = models_dir()
    cache = {} if force else _load_cache()
    entries, seen, bundle_dirs = [], set(), set()

    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            if BUNDLE_FILE in files:
                b = _bundle_entry(dirpath)
                if b:
                    bundle_dirs.add(os.path.abspath(dirpath))
                    if b["id"] not in seen:
                        seen.add(b["id"])
                        entries.append(b)

        for p in _walk(root):
            ap = os.path.abspath(p)
            if any(ap.startswith(d + os.sep) for d in bundle_dirs):
                continue                      # 组件包内部的文件不再单独登记
            e = _entry(p, cache)
            if not e or e["id"] in seen:
                continue
            seen.add(e["id"])
            entries.append(e)

    # 缓存键是绝对路径，文件被删、目录被改名后，旧条目会永远留在 models.json 里。
    # 不清理的话缓存越积越脏，下次挪动项目目录还会读到一批指向"另一个自己"的失效路径。
    stale = [k for k in cache if not os.path.isfile(k)]
    for k in stale:
        cache.pop(k, None)

    _save_cache(cache)
    return entries


def get(model_id):
    if not model_id:
        return None
    for e in scan():
        if e["id"] == model_id:
            return e
    return None


# ---------------------------------------------------------------- 显存估算

_KV_BYTES = {"f16": 2.0, "fp16": 2.0, "q8_0": 1.0, "q5_1": 0.75, "q4_0": 0.5, "q4_1": 0.5}


def estimate(path, ctx, kv_bits="q8_0"):
    """返回显存估算（MiB）。结构未知时只报权重，不编造 KV。"""
    e = None
    for cand in scan():
        if os.path.abspath(cand["path"]) == os.path.abspath(path):
            e = cand
            break
    weights = int(os.path.getsize(path) / (1024 ** 2)) if os.path.isfile(path) else 0
    overhead = int(config.CFG.get("vram_overhead_mb") or 1700)
    out = {"weights_mb": weights, "kv_mb": 0, "overhead_mb": overhead,
           "total_mb": weights + overhead, "kv_per_1k_mb": 0,
           "exact": False, "ctx": int(ctx), "kv_bits": kv_bits}
    if not e or not e.get("n_layer") or not e.get("n_head_kv") or not e.get("head_dim"):
        return out
    per_token = 2 * int(e.get("kv_layers") or e["n_layer"]) * int(e["n_head_kv"]) \
        * int(e["head_dim"]) * _KV_BYTES.get(str(kv_bits).lower(), 1.0)
    kv_mb = int(per_token * int(ctx) / (1024 ** 2))
    out.update({
        "kv_mb": kv_mb,
        "kv_per_1k_mb": round(per_token * 1024 / (1024 ** 2), 3),
        "total_mb": weights + kv_mb + overhead,
        "exact": True,
    })
    return out


def decorate(entry, ctx, kv_bits):
    """给模型列表补上显存估算，供界面与 CLI 直接展示。"""
    d = dict(entry)
    kind = entry.get("kind") or "text"
    if entry.get("bundle") or kind in ("image", "video"):
        # 扩散模型没有 KV cache。显存 = 权重 + 计算缓冲（激活值、VAE 中间张量）。
        # 缓冲按类型给经验值，标 exact=False，别让它看起来像精确计算。
        w = entry.get("weights_mb", 0)
        overhead = 2200 if kind == "video" else 1500
        est = {"weights_mb": w, "kv_mb": 0, "overhead_mb": overhead,
               "total_mb": w + overhead, "kv_per_1k_mb": 0,
               "exact": False, "ctx": 0, "kv_bits": None}
    else:
        est = estimate(entry["path"], ctx, kv_bits)
    d["est"] = est
    gpu = None
    try:
        from .adapters.base import gpu_info
        gpu = gpu_info()
    except Exception:
        pass
    if gpu:
        d["fits"] = est["total_mb"] <= gpu["total_mb"] - 200
        d["gpu_total_mb"] = gpu["total_mb"]
    else:
        d["fits"] = None
    return d
