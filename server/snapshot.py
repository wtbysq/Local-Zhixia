"""请求快照（Q3：密度由使用者配置决定）。

三档，在 config.json 的 `snapshot.level` 里设：
  off   —— 完全不写。只想要聊天时用，零额外磁盘。
  basic —— 每次请求一行：模型、参数、tokens、首字延迟、速度、压缩决策。不含正文。
  full  —— 额外保存实际发送的消息序列与摘要全文。用于重放与复盘。

为什么默认 basic：它足以回答「这次为什么慢、为什么忘了前文」，
而 full 会让磁盘随对话线性增长，且正文本来就已在 messages 表里。
"""
import json
import os
import time

from . import config

LEVELS = ("off", "basic", "full")


def level():
    lv = str((config.CFG.get("snapshot") or {}).get("level") or "basic").lower()
    return lv if lv in LEVELS else "basic"


def _dir():
    d = os.path.join(config.DATA_DIR, "snapshots")
    os.makedirs(d, exist_ok=True)
    return d


def write(record, lv=None):
    """追加一条快照。失败不影响对话——快照是旁路，不是主链路。"""
    lv = lv or level()
    if lv == "off":
        return None
    rec = dict(record)
    rec.setdefault("id", "snap_%d_%s" % (int(time.time() * 1000), os.urandom(3).hex()))
    rec["ts"] = int(time.time() * 1000)
    rec["level"] = lv
    if lv != "full":
        rec.pop("messages", None)
        rec.pop("summary", None)
        rec.pop("thinking", None)
    path = os.path.join(_dir(), time.strftime("%Y-%m-%d") + ".jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec["id"]
    except Exception:
        return None


def list_recent(limit=50):
    """最近 N 条，新的在前。只读行，不解析成大数据结构。"""
    d = _dir()
    files = sorted([os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")],
                   reverse=True)
    out = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec.pop("messages", None)
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


def get(snap_id):
    """取单条完整快照（含 full 档的消息序列），用于重放。"""
    d = _dir()
    for fn in sorted(os.listdir(d), reverse=True):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or snap_id not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == snap_id:
                    return rec
    return None
