"""SQLite 存储层。

设计要点：
- 每条请求独立连接：ThreadingHTTPServer 是多线程的，共享连接需要额外加锁，
  而 SQLite 本地打开的开销可以忽略（<1ms）。
- 启用 WAL，读写不互斥，长回答写入时不影响列表查询。
- 压缩结果存在 sessions.summary_json，原始消息永不因压缩被删除。
"""
import json
import os
import sqlite3
import time
import uuid

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id              TEXT PRIMARY KEY,
  title           TEXT NOT NULL DEFAULT '新对话',
  model           TEXT,
  params_json     TEXT NOT NULL DEFAULT '{}',
  summary_json    TEXT,
  summary_dirty   INTEGER NOT NULL DEFAULT 0,
  compact_count   INTEGER NOT NULL DEFAULT 0,
  last_compact_at INTEGER,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  archived        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
  id         TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK(role IN ('system','user','assistant')),
  content    TEXT NOT NULL,
  tokens     INTEGER,
  meta_json  TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_ms():
    return int(time.time() * 1000)


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- sessions

def _session_row(row):
    if row is None:
        return None
    d = dict(row)
    try:
        d["params"] = json.loads(d.pop("params_json") or "{}")
    except Exception:
        d["params"] = {}
    raw = d.pop("summary_json", None)
    try:
        d["summary"] = json.loads(raw) if raw else None
    except Exception:
        d["summary"] = None
    d["summary_dirty"] = bool(d.get("summary_dirty"))
    d["archived"] = bool(d.get("archived"))
    return d


def create_session(title=None, model=None, params=None):
    sid = new_id("s")
    ts = now_ms()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO sessions(id,title,model,params_json,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (sid, title or "新对话", model, json.dumps(params or {}, ensure_ascii=False), ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(sid)


def get_session(sid):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return _session_row(row)
    finally:
        conn.close()


def list_sessions(include_archived=False):
    conn = connect()
    try:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msg_count,"
               " (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.role='user') AS turn_count"
               " FROM sessions s")
        if not include_archived:
            sql += " WHERE s.archived=0"
        sql += " ORDER BY s.updated_at DESC"
        out = []
        for row in conn.execute(sql):
            d = _session_row(row)
            d["msg_count"] = row["msg_count"]
            d["turn_count"] = row["turn_count"]
            out.append(d)
        return out
    finally:
        conn.close()


def update_session(sid, **fields):
    if not fields:
        return get_session(sid)
    allowed = {"title", "model", "params_json", "summary_json", "summary_dirty",
               "compact_count", "last_compact_at", "archived", "updated_at"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append("%s=?" % k)
        vals.append(v)
    if not sets:
        return get_session(sid)
    sets.append("updated_at=?")
    vals.append(now_ms())
    vals.append(sid)
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET %s WHERE id=?" % ",".join(sets), vals)
        conn.commit()
    finally:
        conn.close()
    return get_session(sid)


def delete_session(sid):
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_sessions(ids):
    """批量删除，一个事务里做完。

    要么全成功要么全回滚——批量操作最怕删一半失败，
    用户既不知道删了哪些，也没法重试。返回 (删除数, 实际删掉的 id)。
    """
    ids = [i for i in (ids or []) if i]
    if not ids:
        return 0, []
    deleted = []
    conn = connect()
    try:
        conn.execute("BEGIN")
        for i in range(0, len(ids), 400):        # 避开 SQLite 变量上限
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT id FROM sessions WHERE id IN (%s)" % marks, chunk).fetchall()
            found = [r["id"] for r in rows]
            if found:
                conn.execute("DELETE FROM sessions WHERE id IN (%s)"
                             % ",".join("?" * len(found)), found)
                deleted.extend(found)
        conn.commit()
        return len(deleted), deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def touch_session(sid):
    conn = connect()
    try:
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), sid))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- messages

def add_message(sid, role, content, tokens=None, meta=None):
    mid = new_id("m")
    ts = now_ms()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO messages(id,session_id,role,content,tokens,meta_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (mid, sid, role, content, tokens,
             json.dumps(meta, ensure_ascii=False) if meta else None, ts),
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, sid))
        conn.commit()
    finally:
        conn.close()
    return {"id": mid, "session_id": sid, "role": role, "content": content,
            "tokens": tokens, "meta": meta, "created_at": ts}


def delete_message(mid):
    """删除单条消息。摘要覆盖范围会因此错位，必须置 dirty 让下次请求重算。"""
    conn = connect()
    try:
        row = conn.execute("SELECT session_id FROM messages WHERE id=?", (mid,)).fetchone()
        if not row:
            return False
        sid = row["session_id"]
        conn.execute("DELETE FROM messages WHERE id=?", (mid,))
        conn.execute("UPDATE sessions SET summary_dirty=1, updated_at=? WHERE id=?", (now_ms(), sid))
        conn.commit()
        return True
    finally:
        conn.close()


def list_messages(sid):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid", (sid,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.pop("meta_json")) if d.get("meta_json") else None
            except Exception:
                d["meta"] = None
            out.append(d)
        return out
    finally:
        conn.close()


def count_messages(sid):
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- settings

def get_settings():
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}
    finally:
        conn.close()


def set_setting(key, value):
    conn = connect()
    try:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()
    finally:
        conn.close()
