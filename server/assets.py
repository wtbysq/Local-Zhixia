"""参考素材层：图生图 / 局部重绘 / 图生视频的输入。

与产物相反——产物是「生成了什么」，素材是「拿什么当条件」。
两者都落在 data/ 下但分目录，备份与清理互不影响。

存 data/inputs/<随机id>.<ext>，原始文件名只记在 sidecar json 里，原因有三：
  1. 中文名、emoji 名在 Windows 上容易踩编码坑
  2. 重名会互相覆盖
  3. 直接用文件名当路径就有路径穿越风险（../../app.db）
"""
import base64
import json
import os
import re
import time
import uuid

from . import config

ASSET_DIR = os.path.join(config.DATA_DIR, "inputs")

# 上限按「解码后的字节」算。图片 24MB 足够 4K PNG；
# 视频单独放宽，但通过 base64 传 200MB 已经是这套零依赖方案的极限了。
MAX_BYTES = {"image": 24 * 1024 * 1024, "video": 200 * 1024 * 1024}

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _sniff(head):
    """按魔数判断真实类型。扩展名只用于落盘，不用来判断。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[4:8] == b"ftyp":
        return "video", ".mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video", ".webm"
    return None, None


def _strip_data_url(data):
    """前端用 FileReader 读出来常常带 data URL 前缀，去掉后再解码。"""
    if isinstance(data, str) and data.startswith("data:") and "," in data:
        return data.split(",", 1)[1]
    return data


def save(kind, data, name=""):
    """保存一份素材，返回登记信息。data 是 base64 字符串。"""
    kind = "video" if kind == "video" else "image"
    payload = _strip_data_url(data)
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise ValueError("素材不是合法的 base64")

    if not raw:
        raise ValueError("素材是空的")
    limit = MAX_BYTES[kind]
    if len(raw) > limit:
        raise ValueError("素材太大：%.1f MB，上限 %d MB"
                         % (len(raw) / 1024 ** 2, limit // 1024 ** 2))

    real_kind, ext = _sniff(raw[:16])
    if not real_kind:
        raise ValueError("认不出文件类型，只支持 PNG / JPEG / WebP / MP4 / WebM")
    if real_kind != kind:
        raise ValueError("这是%s，不能当%s素材用" % (
            "图片" if real_kind == "image" else "视频",
            "图片" if kind == "image" else "视频"))

    os.makedirs(ASSET_DIR, exist_ok=True)
    aid = uuid.uuid4().hex[:16]
    filename = aid + ext
    path = os.path.join(ASSET_DIR, filename)
    with open(path, "wb") as f:
        f.write(raw)

    meta = {"id": aid, "kind": real_kind, "file": filename,
            "name": (name or "粘贴的素材")[:80],
            "bytes": len(raw), "created_at": int(time.time() * 1000)}
    try:
        with open(os.path.join(ASSET_DIR, aid + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    meta["path"] = path
    meta["url"] = "/api/assets/" + aid
    return meta


def path_of(asset_id):
    """素材 id → 绝对路径；id 不合法或文件不存在都返回 None。

    id 只允许 16 位十六进制，先把路径穿越的可能性堵死。
    """
    if not asset_id or not _ID_RE.match(str(asset_id)):
        return None
    for ext in (".png", ".jpg", ".webp", ".mp4", ".webm"):
        p = os.path.join(ASSET_DIR, asset_id + ext)
        if os.path.isfile(p):
            return p
    return None


def get(asset_id):
    p = path_of(asset_id)
    if not p:
        return None
    try:
        with open(os.path.join(ASSET_DIR, asset_id + ".json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {"id": asset_id}
    meta["path"] = p
    meta["url"] = "/api/assets/" + asset_id
    return meta
