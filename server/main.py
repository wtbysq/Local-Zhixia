"""本地 AI 界面后端 —— 标准库实现，零 pip 依赖。

    python server/main.py          或     python -m server.main

接口契约与方案 1.4 完全一致（若将来换 FastAPI，只需重写本文件）。
"""
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server import assets, config, context, llm, models, snapshot, store
    from server import png as pngmod
    from server.tokens import estimate
else:
    from . import assets, config, context, llm, models, snapshot, store
    from . import png as pngmod
    from .tokens import estimate

# ---------------------------------------------------------------- 运行时状态

_stop_flags = {}
_stop_lock = threading.Lock()
_log_lock = threading.Lock()

# 生成产物支持的格式 → Content-Type
_GEN_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
    ".webm": "video/webm", ".mp4": "video/mp4", ".avi": "video/x-msvideo",
}


def list_generated(limit=60):
    """列出生成产物。图片来自 data/图片，视频来自 data/视频。"""
    out = []
    for kind, folder in (("image", config.GEN_IMAGE_DIR), ("video", config.GEN_VIDEO_DIR)):
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if fn.lower().endswith(".json"):
                continue                      # 参数记录不是产物
            p = os.path.join(folder, fn)
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"file": fn, "url": "/generated/%s/%s" % (kind, fn),
                        "size": st.st_size, "created_at": int(st.st_mtime * 1000),
                        "kind": kind, "ext": os.path.splitext(fn)[1].lower(),
                        "meta": _read_sidecar(p)})
    out.sort(key=lambda x: -x["created_at"])
    return out[:limit]


def _read_sidecar(path):
    """生成时顺手写一个同名 .json，记录提示词与参数，便于复现同一条结果。"""
    try:
        with open(path + ".json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _align8(v, lo=256, hi=1536):
    """扩散模型要求边长是 8 的倍数；顺便夹到安全范围，避免用户输入 8000 直接 OOM。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = lo
    v = max(lo, min(hi, v))
    return (v // 8) * 8


def ctx_ceiling():
    """上下文硬上限，来自配置。超过它的请求一律被夹回，防止 OOM。"""
    try:
        return int(config.CFG.get("ctx_ceiling") or 16384)
    except (TypeError, ValueError):
        return 16384


def default_model():
    """新会话的默认模型：只在语言模型里挑，别把图片模型当聊天模型用。"""
    entries = [m for m in llm.list_models() if m.get("kind", "text") == "text"]
    want = config.CFG.get("default_model")
    if want:
        for e in entries:
            if e["id"] == want and e.get("fits") is not False:
                return want
    for e in entries:
        if e.get("fits") is not False:
            return e["id"]
    return entries[0]["id"] if entries else None


def apply_persisted_settings():
    """把数据库里存的用户默认值贴回配置。启动时调用一次。

    这是「重启后参数不丢」的关键：值存在 settings 表，加载顺序在 config 读完之后。
    """
    try:
        raw = store.get_settings()
    except Exception:
        return 0
    n = 0
    for key, val in (raw or {}).items():
        try:
            parsed = json.loads(val)
        except Exception:
            parsed = val
        if config.set_runtime(key, parsed):
            n += 1
    return n


def log_event(**kv):
    if not config.CFG.get("log_requests"):
        return
    kv["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    line = json.dumps(kv, ensure_ascii=False)
    try:
        with _log_lock:
            with open(os.path.join(config.LOG_DIR, "app.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _stop_flag(sid):
    with _stop_lock:
        flag = _stop_flags.get(sid)
        if flag is None:
            flag = threading.Event()
            _stop_flags[sid] = flag
        return flag


def _clear_stop_flag(sid):
    with _stop_lock:
        _stop_flags.pop(sid, None)


# ---------------------------------------------------------------- 摘要回调

def make_summarizer(model=None):
    """注入给 context.build_context 的摘要函数。失败抛异常由 context 降级。"""

    def _summarize(prev_text, turns, max_tokens):
        req = context.build_summary_request(prev_text, turns, max_tokens)
        return llm.chat_once(req, temperature=0.2, max_tokens=max(80, max_tokens),
                             timeout=config.CFG["summary_timeout"], model=model)

    return _summarize


def all_white_mask(init_path):
    """造一张与底图同尺寸的全白 PNG。

    inpaint 模型只在「收到 mask」时才真正去噪，而且力度很弱（实测蒙版区域
    差异只有全图重绘的 1/3）。所以喂它全白蒙版 = 让它做满强度整图重绘，
    真实蒙版由 Handler._generate 在生成后做像素级合成。
    同尺寸复用同一个文件，避免每次都写一遍。
    """
    with open(init_path, "rb") as f:
        w, h, _ch, _px = pngmod.decode(f.read())
    path = os.path.join(assets.ASSET_DIR, "white-%dx%d.png" % (w, h))
    if not os.path.isfile(path):
        with open(path, "wb") as f:
            f.write(pngmod.encode(w, h, 3, b"\xff\xff\xff" * (w * h)))
    return path


# ---------------------------------------------------------------- 请求处理器

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LocalAI/1.0"

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        pass  # 静音默认访问日志，改用结构化日志

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _err(self, status, code, message):
        self._json({"error": {"code": code, "message": message}}, status=status)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, event, data):
        payload = ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")
        self.wfile.write(("%X\r\n" % len(payload)).encode("ascii") + payload + b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    # ---------- 路由 ----------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        parts = [unquote(p) for p in path.strip("/").split("/") if p]

        try:
            if parts and parts[0] == "generated":
                return self._generated_file(parts[1:])
            if not parts or parts[0] != "api":
                return self._static(path)

            # /api/health
            if parts == ["api", "health"] and method == "GET":
                return self._json(llm.health())

            # /api/models —— 来自 models 目录的登记表，不依赖引擎是否在跑
            if parts == ["api", "models"] and method == "GET":
                try:
                    ctx = int((query.get("ctx") or [config.CFG["ctx_max"]])[0])
                except (TypeError, ValueError):
                    ctx = config.CFG["ctx_max"]
                allm = llm.list_models(ctx=ctx)
                counts = {"text": 0, "image": 0, "video": 0}
                for m in allm:
                    k = m.get("kind", "text")
                    counts[k] = counts.get(k, 0) + 1
                want = (query.get("kind") or ["all"])[0]
                shown = allm if want in ("all", "", "None") else \
                    [m for m in allm if m.get("kind") == want]
                return self._json({"models": shown, "all_count": len(allm),
                                   "counts": counts, "kind": want,
                                   "default": default_model(),
                                   "engine": llm.status()})

            # /api/models/load —— 让引擎加载指定模型（切换模型会重启引擎进程）
            if parts == ["api", "models", "load"] and method == "POST":
                return self._load_model(self._body())

            # /api/snapshots —— 最近的请求快照（默认不含正文）
            if parts == ["api", "snapshots"] and method == "GET":
                try:
                    limit = int((query.get("limit") or ["30"])[0])
                except (TypeError, ValueError):
                    limit = 30
                sid = (query.get("session") or [None])[0]
                rows = snapshot.list_recent(limit=limit * 6 if sid else limit)
                if sid:
                    rows = [r for r in rows if r.get("session_id") == sid][:limit]
                return self._json({"level": snapshot.level(), "snapshots": rows})

            # /api/assets —— 上传参考素材（图生图 / 重绘 / 图生视频的输入）
            if parts == ["api", "assets"] and method == "POST":
                return self._upload_asset(self._body())

            if len(parts) == 3 and parts[:2] == ["api", "assets"] and method == "GET":
                return self._asset_file(parts[2])

            # /api/generate —— 图片/视频生成（SSE：stage / progress / result / error）
            if parts == ["api", "generate"] and method == "POST":
                return self._generate(self._body())

            if parts == ["api", "generate", "stop"] and method == "POST":
                body = self._body()
                entry = models.get(body.get("model") or "")
                kind = (entry or {}).get("kind") or body.get("kind") or "image"
                return self._json({"stopped": llm.cancel_generate(kind)})

            # /api/generated —— 已生成的产物列表（新→旧）
            if parts == ["api", "generated"] and method == "GET":
                try:
                    limit = int((query.get("limit") or ["60"])[0])
                except (TypeError, ValueError):
                    limit = 60
                return self._json({
                    "generated": list_generated(limit),
                    # 顺手把两个目录的真实路径给前端：用户最常问的就是「文件存哪了」
                    "dirs": {"image": config.GEN_IMAGE_DIR, "video": config.GEN_VIDEO_DIR},
                })

            # /api/chat
            if parts == ["api", "chat"] and method == "POST":
                return self._chat(self._body())

            if parts == ["api", "chat", "stop"] and method == "POST":
                body = self._body()
                sid = body.get("session_id") or ""
                _stop_flag(sid).set()
                return self._json({"stopped": True, "session_id": sid})

            # /api/settings
            if parts == ["api", "settings"]:
                if method == "GET":
                    return self._json({"config": config.CFG})
                if method == "POST":
                    return self._update_settings(self._body())

            # /api/sessions/batch-delete —— 批量删除（单事务，要么全删要么全不删）
            if parts == ["api", "sessions", "batch-delete"] and method == "POST":
                ids = (self._body() or {}).get("ids")
                if not isinstance(ids, list) or not ids:
                    return self._err(400, "BAD_IDS", "ids 必须是非空数组")
                try:
                    n, deleted = store.delete_sessions(ids)
                except Exception:
                    log_event(event="batch_delete_failed", trace=traceback.format_exc()[:1200])
                    return self._err(500, "DELETE_FAILED", "批量删除失败，已回滚，未删除任何会话")
                log_event(event="sessions_deleted", count=n, requested=len(ids))
                return self._json({"deleted": n, "ids": deleted,
                                   "missing": [i for i in ids if i not in deleted]})

            # /api/sessions...
            if parts[:2] == ["api", "sessions"]:
                if len(parts) == 2:
                    if method == "GET":
                        return self._json({"sessions": store.list_sessions()})
                    if method == "POST":
                        body = self._body()
                        model = body.get("model") or default_model()
                        ok, why = llm.validate_model(model)
                        if not ok:
                            return self._err(400, "MODEL_INVALID", why)
                        sess = store.create_session(
                            title=body.get("title"),
                            model=model,
                            params=body.get("params") or {},
                        )
                        return self._json({"session": sess}, status=201)
                if len(parts) == 3:
                    sid = parts[2]
                    if method == "GET":
                        sess = store.get_session(sid)
                        if not sess:
                            return self._err(404, "NOT_FOUND", "会话不存在")
                        return self._json({"session": sess})
                    if method == "PATCH":
                        body = self._body()
                        if body.get("model") is not None:
                            cur = store.get_session(sid)
                            if cur and body["model"] != cur.get("model") and store.count_messages(sid) > 0:
                                return self._err(
                                    409, "MODEL_LOCKED",
                                    "会话已绑定模型 %s，对话途中不能更换。"
                                    "想换模型请新建一个对话。" % cur.get("model"))
                        patch = {k: v for k, v in body.items() if k in ("title", "model", "archived")}
                        if "params" in body:
                            patch["params_json"] = json.dumps(body["params"] or {}, ensure_ascii=False)
                        sess = store.update_session(sid, **patch)
                        return self._json({"session": sess})
                    if method == "DELETE":
                        ok = store.delete_session(sid)
                        return self._json({"deleted": ok})
                if len(parts) == 4:
                    sid, action = parts[2], parts[3]
                    if action == "messages" and method == "GET":
                        sess = store.get_session(sid)
                        if not sess:
                            return self._err(404, "NOT_FOUND", "会话不存在")
                        return self._json({"session": sess, "messages": store.list_messages(sid)})
                    if action == "export" and method == "GET":
                        fmt = (query.get("format", ["md"])[0] or "md").lower()
                        return self._export(sid, fmt)
                    if action == "compact" and method == "POST":
                        return self._compact(sid)

            return self._err(404, "NOT_FOUND", "未知接口 %s %s" % (method, path))
        except llm.BackendError as exc:
            log_event(event="backend_error", code=exc.code, message=exc.message)
            return self._err(502, exc.code, exc.message)
        except ConnectionError:
            # 客户端主动断开（关标签页 / 点停止）不是错误，不该进 unhandled
            return
        except Exception:
            log_event(event="unhandled", trace=traceback.format_exc()[:2000])
            return self._err(500, "INTERNAL", "服务端异常，详见 logs/app.log")

    # ---------- 静态资源 ----------

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(config.WEB_DIR, rel))
        if not full.startswith(os.path.normpath(config.WEB_DIR)) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype, _ = mimetypes.guess_type(full)
        if full.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif full.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif full.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        elif full.endswith(".svg"):
            ctype = "image/svg+xml"
        ctype = ctype or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass

    def _generated_file(self, parts):
        """路径形如 /generated/image/xxx.png。只认 image / video 两个目录。"""
        if len(parts) != 2 or parts[0] not in ("image", "video"):
            return self._err(404, "NOT_FOUND", "找不到文件")
        kind = parts[0]
        name = os.path.basename(parts[1])
        base = config.GEN_VIDEO_DIR if kind == "video" else config.GEN_IMAGE_DIR
        full = os.path.normpath(os.path.join(base, name))
        if os.path.dirname(full) != os.path.normpath(base) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ext = os.path.splitext(name)[1].lower()
        ctype = _GEN_MEDIA.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass

    def _asset_file(self, asset_id):
        """把上传过的素材回给前端做缩略图预览。"""
        full = assets.path_of(asset_id)
        if not full:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = _GEN_MEDIA.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.end_headers()
        try:
            self.wfile.write(data)
        except ConnectionError:
            pass

    def _upload_asset(self, body):
        """收参考素材。用 base64 JSON 而不是 multipart：标准库解析 multipart 要手写一遍，
        而 base64 的 33% 膨胀对图片素材（几 MB 级）可以接受。"""
        try:
            meta = assets.save(body.get("kind") or "image",
                               body.get("data") or "",
                               body.get("name") or "")
        except ValueError as exc:
            return self._err(400, "BAD_ASSET", str(exc))
        except Exception:
            log_event(event="asset_save_failed", trace=traceback.format_exc()[:800])
            return self._err(500, "ASSET_SAVE_FAILED", "素材保存失败，请重试")
        log_event(event="asset_saved", kind=meta["kind"], bytes=meta["bytes"])
        return self._json({"asset": meta}, status=201)

    # ---------- 业务 ----------

    def _generate(self, body):
        """图片/视频生成。SSE 推送：stage → progress → result / error。

        为什么用 SSE 而不是同步返回：视频一次要几分钟，
        没有进度的话界面只能干等，用户没法判断是在算还是卡死了。
        """
        model_id = (body.get("model") or "").strip()
        entry = models.get(model_id)
        if not entry:
            return self._err(400, "MODEL_NOT_FOUND", "模型 %s 不在 models 目录里" % model_id)

        kind = entry.get("kind") or "image"
        # 所有校验必须在 _start_sse 之前完成：一旦开始推流，HTTP 状态码就改不了了，
        # 错误只能变成 200 + error 事件，前端与脚本都难以判断。
        if kind not in ("image", "video"):
            return self._err(400, "WRONG_KIND",
                             "%s 是%s模型，不能用来生成。请到「图片」或「视频」里选对应模型。"
                             % (model_id, "语言" if kind == "text" else kind))
        if entry.get("missing"):
            return self._err(400, "MODEL_INCOMPLETE",
                             "模型 %s 缺少组件：%s" % (model_id, "、".join(entry["missing"])))
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "EMPTY_PROMPT", "请先写提示词")

        # ---- 参考素材 ----
        # 文字始终是主控：素材只做条件输入，prompt 不能为空。
        src, src_ids = {}, {}
        for field in ("init_image", "end_image", "mask"):
            aid = str(body.get(field) or "").strip()
            if not aid:
                continue
            p = assets.path_of(aid)
            if not p:
                return self._err(400, "ASSET_NOT_FOUND",
                                 "参考素材不存在（%s），请重新上传" % field)
            src[field] = p
            src_ids[field] = aid

        # 需要哪种能力：优先按最"强"的输入判断，报错信息才对得上
        need = None
        if src.get("mask"):
            need = "inpaint"
        elif src.get("end_image"):
            need = "flf2v"
        elif src.get("init_image"):
            need = "i2v" if kind == "video" else "i2i"
        caps = models.capabilities(entry)
        if need and need not in caps:
            supported = "、".join(models.CAPABILITIES.get(c, c) for c in caps) or "无"
            return self._err(400, "CAPABILITY_UNSUPPORTED",
                             "%s 能做的：%s；不支持「%s」。换个模型，或去掉参考素材。"
                             % (model_id, supported, models.CAPABILITIES.get(need, need)))

        strength = None
        if src.get("init_image"):
            try:
                strength = float(body.get("strength"))
            except (TypeError, ValueError):
                strength = 0.75
            strength = min(1.0, max(0.0, strength))

        adapter = llm.for_kind(kind)
        if not hasattr(adapter, "generate"):
            return self._err(400, "NOT_SUPPORTED",
                             "当前执行器 %s 不支持生成。把 config.json 的 "
                             "adapters_by_kind.%s 指向 sdcpp 后再试。" % (adapter.name, kind))

        defaults = entry.get("params") or {}
        video = kind == "video"
        frames = 1
        if video:
            try:
                frames = int(body.get("frames") or defaults.get("frames") or 9)
            except (TypeError, ValueError):
                frames = 9
            # 上限放宽到 400：MiniMax-H3 最短就要 124 帧（17k+5）。
            # 显存是否扛得住由引擎自己报错，这里不再提前砍。
            frames = max(1, min(400, frames))

        # 有蒙版时：交给引擎的是「全白蒙版」，让它做满强度整图重绘；
        # 用户真正的蒙版留给后面的像素级合成。底图不是 PNG 时退回原蒙版。
        engine_mask = None
        if src.get("mask") and kind == "image":
            try:
                engine_mask = all_white_mask(src["init_image"])
            except pngmod.PngError as exc:
                log_event(event="white_mask_fallback", reason=str(exc)[:100])
                engine_mask = src["mask"]

        req = {
            "prompt": prompt,
            "negative": (body.get("negative") or "").strip(),
            "width": _align8(body.get("width") or defaults.get("width") or 512),
            "height": _align8(body.get("height") or defaults.get("height") or 512),
            "steps": max(1, min(60, int(body.get("steps") or defaults.get("steps") or 20))),
            "cfg_scale": float(body.get("cfg_scale") or defaults.get("cfg_scale") or 7.0),
            "seed": body.get("seed"),
            "frames": frames,
            "fps": max(1, min(60, int(body.get("fps") or defaults.get("fps") or 16))),
            "init_image": src.get("init_image"),
            "end_image": src.get("end_image"),
            "mask": engine_mask,
            "strength": strength,
        }

        # 视频容器可选：webm（真视频）/ webp（动画图）/ avi（老兼容）
        fmt = str(config.CFG.get("video_format") or "webm").lower().lstrip(".")
        if fmt not in ("webm", "webp", "avi"):
            fmt = "webm"
        ext = "." + fmt if video else ".png"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        fname = "%s-%s%s" % (kind, stamp, ext)
        out_dir = config.gen_dir(kind)          # data/图片 或 data/视频
        out = os.path.join(out_dir, fname)
        os.makedirs(out_dir, exist_ok=True)

        self._start_sse()        # 必须先发响应头，否则 chunk 长度会被当成状态行
        self._sse("stage", {"stage": "preparing", "label": "准备生成",
                            "model": model_id, "kind": kind, "req": req})

        last_stage = {"label": None}

        def on_event(ev):
            # 开了 offload 之后，引擎每步都重新搬一遍权重，会重复报同一个阶段；
            # 只把变化的阶段推给前端，否则进度条那边的文案一直在闪
            if ev.get("type") == "stage":
                lab = ev.get("label") or ev.get("stage")
                if lab == last_stage["label"]:
                    return
                last_stage["label"] = lab
            self._sse(ev.get("type", "stage"), ev)

        started = time.time()
        try:
            result = llm.generate(model_id, req, out, on_event=on_event)
        except llm.BackendError as exc:
            log_event(event="generate_error", model=model_id, code=exc.code,
                      message=exc.message[:200])
            self._sse("error", {"code": exc.code, "message": exc.message})
            return self._sse_end()

        # 局部重绘：引擎那次拿的是整图满强度重绘，这里按蒙版把结果贴回原图。
        # 放在业务层而不是适配器里——「哪些像素该换」是业务语义，不是引擎参数。
        if src.get("mask") and kind == "image":
            try:
                with open(src["init_image"], "rb") as f:
                    base_img = pngmod.decode(f.read())
                with open(src["mask"], "rb") as f:
                    mask_img = pngmod.decode(f.read())
                with open(result["path"], "rb") as f:
                    painted = pngmod.decode(f.read())
                cw, chh, cch, cpx = pngmod.compose_mask(base_img, painted, mask_img)
                with open(result["path"], "wb") as f:
                    f.write(pngmod.encode(cw, chh, cch, cpx))
                result["width"], result["height"] = cw, chh
                result["size"] = os.path.getsize(result["path"])
                log_event(event="inpaint_composed", w=cw, h=chh)
            except pngmod.PngError as exc:
                # 底图不是我们能解的 PNG（比如 JPEG 直接传上来）：
                # 退回引擎的整图重绘，但要留痕并告诉前端，别让人以为局部生效了
                log_event(event="inpaint_compose_skipped", reason=str(exc)[:120])
                result["warnings"] = (result.get("warnings") or []) + [
                    "底图不是 PNG，蒙版合成已跳过：结果是整图重绘"]
            except Exception:
                log_event(event="inpaint_compose_failed",
                          trace=traceback.format_exc()[:800])

        url = "/generated/%s/%s" % (kind, os.path.basename(result["path"]))
        payload = {
            "url": url, "file": result["file"], "kind": kind, "model": model_id,
            "prompt": prompt, "negative": req["negative"], "req": req,
            # 素材 id 单独记一份：req 里是绝对路径，换机器就失效了
            "sources": src_ids or None,
            "width": result["width"], "height": result["height"],
            "frames": result.get("frames"), "fps": req["fps"],
            "elapsed_ms": result["elapsed_ms"], "size": result["size"],
            "warnings": result.get("warnings") or [],
            "engine": result.get("engine"),
            "created_at": int(time.time() * 1000),
        }

        # 参数记录由适配器在产出时写入（同名 .json），这里不重复写
        log_event(event="generate_done", model=model_id, kind=kind,
                  ms=result["elapsed_ms"], w=req["width"], h=req["height"],
                  frames=req["frames"], steps=req["steps"])
        payload["gpu"] = llm.gpu_info()
        self._sse("result", payload)
        self._sse_end()

    def _load_model(self, body):
        """显式加载：让引擎真的把模型读进显存。切换模型会重启引擎进程。"""
        model = body.get("model") or default_model()
        ok, why = llm.validate_model(model)
        if not ok:
            return self._err(400, "MODEL_INVALID", why)
        try:
            ctx = int(body.get("ctx_max") or config.CFG["ctx_max"])
        except (TypeError, ValueError):
            ctx = config.CFG["ctx_max"]
        ceiling = ctx_ceiling()
        clamped = ctx > ceiling
        if clamped:
            ctx = ceiling
        try:
            st = llm.ensure_ready(model, ctx)
        except llm.BackendError as exc:
            log_event(event="engine_load_failed", model=model, ctx=ctx, code=exc.code)
            return self._err(507 if exc.code in ("VRAM_INSUFFICIENT", "OOM") else 502,
                             exc.code, exc.message)
        config.CFG["ctx_max"] = ctx
        log_event(event="model_load", model=model, ctx=ctx, clamped=clamped)
        return self._json({"model": model, "ctx_max": ctx, "clamped": clamped,
                           "engine": st,
                           "note": "本机上下文硬上限 %d；超过会被夹回（改 config.json 的 ctx_ceiling）" % ceiling})

    def _update_settings(self, body):
        """修改默认参数并落库 —— 下次启动仍然生效。

        这里的键是「用户默认值」，与单个会话自己的参数是两回事：
        会话参数只影响那一次对话，这里改的是新对话的起点。
        """
        applied, rejected = {}, {}
        for k, v in (body or {}).items():
            if k not in config.PERSIST_KEYS:
                rejected[k] = "不在可持久化白名单里"
                continue
            try:
                if k == "ctx_max":
                    v = min(int(v), ctx_ceiling())
                elif k == "trigger_ratio":
                    v = min(max(float(v), 0.5), 0.95)
                elif k == "target_ratio":
                    v = min(max(float(v), 0.2), 0.7)
            except (TypeError, ValueError):
                rejected[k] = "类型不对"
                continue
            if k == "default_model" and v:
                ok, why = llm.validate_model(v)
                if not ok:
                    rejected[k] = why
                    continue
            if not config.set_runtime(k, v):
                rejected[k] = "类型不符"
                continue
            val = config.get_runtime(k)
            applied[k] = val
            try:
                store.set_setting(k, json.dumps(val, ensure_ascii=False))
            except Exception:
                rejected[k] = "已生效，但落库失败（重启后会丢）"
        if applied:
            log_event(event="settings_saved", applied=applied)
        return self._json({"config": config.CFG, "applied": applied, "rejected": rejected})

    def _effective_cfg(self, sess, params):
        cfg = dict(config.CFG)
        override = {}
        override.update(sess.get("params") or {})
        override.update(params or {})
        for key in ("ctx_max", "max_tokens", "compact_enabled", "trigger_ratio", "target_ratio",
                    "keep_recent_turns", "min_turns", "max_batch_turns", "summary_cap"):
            if key in override and override[key] is not None:
                try:
                    cfg[key] = int(override[key]) if key not in ("compact_enabled", "trigger_ratio", "target_ratio") else override[key]
                except (TypeError, ValueError):
                    pass
        if int(cfg["ctx_max"]) > ctx_ceiling():
            cfg["ctx_max"] = ctx_ceiling()
        return cfg, override

    def _chat(self, body):
        content = (body.get("content") or "").strip()
        if not content:
            return self._err(400, "EMPTY_CONTENT", "消息内容不能为空")

        sid = body.get("session_id")
        sess = store.get_session(sid) if sid else None
        requested = body.get("model")

        if sess is None:
            model = requested or default_model()
            ok, why = llm.validate_model(model)
            if not ok:
                return self._err(400, "MODEL_INVALID", why)
            sess = store.create_session(
                title=content[:20].replace("\n", " ") or "新对话",
                model=model,
            )
            sid = sess["id"]
        else:
            # 模型在会话创建时绑定，途中不可更换（Q2）。换模型请新开对话。
            model = sess.get("model") or requested or default_model()
            if requested and requested != model:
                return self._err(409, "MODEL_LOCKED",
                                 "会话已绑定模型 %s，对话途中不能更换。"
                                 "想用 %s 请新建一个对话。" % (model, requested))
            if not sess.get("model"):
                store.update_session(sid, model=model)
            if sess["title"] in ("新对话", "", None) and store.count_messages(sid) == 0:
                store.update_session(sid, title=content[:20].replace("\n", " ") or "新对话")

        cfg, override = self._effective_cfg(sess, body.get("params"))

        store.add_message(sid, "user", content, tokens=estimate(content))
        messages = store.list_messages(sid)

        summary = sess.get("summary")
        if sess.get("summary_dirty"):
            summary = None

        started = time.time()
        try:
            result = context.build_context(
                messages,
                system_prompt=(override.get("system_prompt") or ""),
                summarize_fn=make_summarizer(model),
                session_summary=summary,
                cfg=cfg,
            )
        except Exception:
            log_event(event="context_failed", trace=traceback.format_exc()[:1500])
            return self._err(500, "CONTEXT_ERROR", "上下文构造失败")

        info = result["info"]
        if result["summary"]:
            store.update_session(
                sid,
                summary_json=json.dumps(result["summary"], ensure_ascii=False),
                summary_dirty=0,
                compact_count=(sess.get("compact_count") or 0) + 1,
                last_compact_at=store.now_ms(),
            )
            log_event(event="compacted", session=sid, mode=info["mode"], turns=info["turns"],
                      before=info["before"], after=info["after"])

        self._start_sse()
        flag = _stop_flag(sid)
        flag.clear()

        assistant_id = store.new_id("m")
        try:
            # 引擎按需启动。放在 SSE 之后是有意的：加载模型要几十秒，
            # 必须先让界面看到「正在加载」，而不是白屏等。
            self._sse("stage", {"stage": "engine", "model": model, "ctx_max": cfg["ctx_max"]})
            t_engine0 = time.time()
            try:
                llm.ensure_ready(model, cfg["ctx_max"])
            except llm.BackendError as exc:
                log_event(event="engine_error", session=sid, model=model, code=exc.code)
                self._sse("error", {"code": exc.code, "message": exc.message,
                                    "partial_saved": False})
                return
            engine_ms = int((time.time() - t_engine0) * 1000)
            if engine_ms > 500:
                # 首次加载很慢，把它单独记下来，别混进生成速度里
                log_event(event="engine_ready", session=sid, model=model, engine_ms=engine_ms)
            self._sse("stage", {"stage": "generating"})

            if info["compacted"] or info["trimmed"]:
                self._sse("compact", {
                    "compacted": info["compacted"], "mode": info["mode"], "turns": info["turns"],
                    "before": info["before"], "after": info["after"],
                    "summary_tokens": info["summary_tokens"], "trimmed": info["trimmed"],
                    "covered_turns": info["covered_turns"], "ms": info["ms"],
                    "reason": info["reason"],
                    "summary": (result["summary"] or {}).get("text", "")[:4000],
                })

            self._sse("meta", {
                "msg_id": assistant_id, "session_id": sid, "model": model,
                "ctx_used": result["tokens"], "ctx_max": result["ctx_max"],
                "ctx_usable": result["usable"], "ratio": result["ratio"],
                "turns_total": result.get("turns_total"), "trimmed": info["trimmed"],
                "ttft_target_ms": 1500,
            })

            parts = []
            think_parts = []
            usage = {}
            ttft = None
            first = True
            finish_reason = "stop"
            aborted = False

            try:
                for chunk in llm.chat_stream(result["messages"], params=override, model=model):
                    if flag.is_set():
                        aborted = True
                        finish_reason = "aborted"
                        break
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        # Qwen3 等模型的思考过程走 reasoning_content。
                        # 不单独处理的话，开了思考开关正文会是空的。
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            if first:
                                ttft = int((time.time() - started) * 1000)
                                first = False
                            think_parts.append(reasoning)
                            self._sse("think", {"t": reasoning})
                        piece = delta.get("content")
                        if piece:
                            if first:
                                ttft = int((time.time() - started) * 1000)
                                first = False
                            parts.append(piece)
                            self._sse("delta", {"t": piece})
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
            except llm.BackendError as exc:
                partial = "".join(parts)
                if partial:
                    store.add_message(sid, "assistant", partial,
                                      tokens=estimate(partial),
                                      meta={"partial": True, "error": exc.code})
                log_event(event="stream_error", session=sid, code=exc.code)
                self._sse("error", {"code": exc.code, "message": exc.message,
                                    "partial_saved": bool(partial)})
                return

            text = "".join(parts)
            elapsed = max(0.001, time.time() - started)
            completion_tokens = usage.get("completion_tokens") or estimate(text)
            # 生成速度只算「首字之后」：否则引擎加载的十几秒会把 tok/s 拉低一个数量级
            gen_seconds = max(0.001, elapsed - (ttft or 0) / 1000.0)
            tps = round(completion_tokens / gen_seconds, 2) if completion_tokens else 0

            meta = {
                "ttft_ms": ttft, "tps": tps, "completion_tokens": completion_tokens,
                "prompt_tokens": usage.get("prompt_tokens") or result["tokens"],
                "finish_reason": finish_reason,
                "engine_ms": engine_ms,
                "thinking_chars": len("".join(think_parts)),
                "compaction": {k: info[k] for k in ("compacted", "mode", "turns", "before", "after", "trimmed")},
            }
            if text or aborted:
                store.add_message(sid, "assistant", text, tokens=completion_tokens, meta=meta)

            self._sse("usage", {
                "prompt_tokens": meta["prompt_tokens"], "completion_tokens": completion_tokens,
                "ttft_ms": ttft, "tps": tps, "elapsed_ms": int(elapsed * 1000),
                "engine_ms": engine_ms,
                "finish_reason": finish_reason, "msg_id": assistant_id,
            })
            snap_id = snapshot.write({
                "session_id": sid, "model": model,
                "adapter": config.CFG.get("adapter"),
                "params": {k: override.get(k) for k in
                           ("temperature", "top_p", "max_tokens", "top_k",
                            "repeat_penalty", "enable_thinking")},
                "ctx": {"max": result["ctx_max"], "used": result["tokens"],
                        "usable": result["usable"], "ratio": result["ratio"]},
                "compaction": meta["compaction"],
                "usage": {"prompt_tokens": meta["prompt_tokens"],
                          "completion_tokens": completion_tokens,
                          "ttft_ms": ttft, "tps": tps,
                          "elapsed_ms": int(elapsed * 1000),
                          "finish_reason": finish_reason, "aborted": aborted},
                "messages": result["messages"],
                "summary": (result["summary"] or {}).get("text", ""),
                "thinking": "".join(think_parts)[:4000],
            })
            self._sse("done", {"finish_reason": finish_reason, "aborted": aborted,
                               "content_len": len(text), "snapshot": snap_id})
            log_event(event="chat", session=sid, model=model, ttft_ms=ttft, tps=tps,
                      tokens=completion_tokens, ctx=result["tokens"], ratio=result["ratio"],
                      compacted=info["compacted"], mode=info["mode"], trimmed=info["trimmed"])
        except ConnectionError:
            text = "".join(locals().get("parts", []))
            if text:
                store.add_message(sid, "assistant", text, tokens=estimate(text),
                                  meta={"partial": True, "finish_reason": "client_closed"})
            log_event(event="client_closed", session=sid, chars=len(text))
        finally:
            _clear_stop_flag(sid)
            self._sse_end()

    def _compact(self, sid):
        sess = store.get_session(sid)
        if not sess:
            return self._err(404, "NOT_FOUND", "会话不存在")
        messages = store.list_messages(sid)
        cfg, override = self._effective_cfg(sess, {})
        before_count = len(messages)
        # 手动压缩：强制把 keep_recent 之外的轮次都压掉一批
        result = context.build_context(
            messages,
            system_prompt=(override.get("system_prompt") or ""),
            summarize_fn=make_summarizer(sess.get("model")),
            session_summary=None if sess.get("summary_dirty") else sess.get("summary"),
            cfg=dict(cfg, trigger_ratio=0.0, target_ratio=0.3),
        )
        info = result["info"]
        if result["summary"]:
            store.update_session(
                sid,
                summary_json=json.dumps(result["summary"], ensure_ascii=False),
                summary_dirty=0,
                compact_count=(sess.get("compact_count") or 0) + 1,
                last_compact_at=store.now_ms(),
            )
        return self._json({
            "compacted": info["compacted"], "mode": info["mode"], "turns": info["turns"],
            "before": info["before"], "after": info["after"], "ms": info["ms"],
            "reason": info["reason"], "summary": (result["summary"] or {}).get("text", ""),
            "messages_total": before_count,
        })

    def _export(self, sid, fmt):
        sess = store.get_session(sid)
        if not sess:
            return self._err(404, "NOT_FOUND", "会话不存在")
        messages = store.list_messages(sid)
        if fmt == "json":
            body = json.dumps({"session": sess, "messages": messages},
                              ensure_ascii=False, indent=2).encode("utf-8")
            ctype = "application/json; charset=utf-8"
            name = "%s.json" % sid
        else:
            lines = ["# %s" % (sess.get("title") or "对话"), ""]
            lines.append("- 模型：%s" % (sess.get("model") or "-"))
            lines.append("- 消息数：%d" % len(messages))
            lines.append("")
            if sess.get("summary"):
                lines.append("> 历史摘要（覆盖前 %d 轮）：\n" % sess["summary"].get("covered_turns", 0))
                lines.append(sess["summary"].get("text", ""))
                lines.append("")
            for m in messages:
                role = {"user": "我", "assistant": "助手"}.get(m["role"], m["role"])
                lines.append("## %s" % role)
                lines.append("")
                lines.append(m["content"])
                lines.append("")
            body = "\n".join(lines).encode("utf-8")
            ctype = "text/markdown; charset=utf-8"
            name = "%s.md" % sid
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def create_server(host=None, port=None):
    """构造 HTTP 服务但不启动。桌面壳与命令行共用同一个入口。"""
    host = host or config.CFG["host"]
    port = int(port if port is not None else config.CFG["port"])
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠ 警告：host=%s 会让同网段的设备访问到本界面。默认应为 127.0.0.1" % host)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def start_in_thread(host="127.0.0.1", port=0):
    """在后台线程里启动服务，返回 (httpd, 实际端口, 线程)。

    port=0 表示让系统分配空闲端口——桌面版用这个，避免和网页版抢 8787。
    """
    httpd = create_server(host, port)
    t = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    t.start()
    return httpd, httpd.server_address[1], t


def shutdown(httpd):
    """停止接受新请求并回收监听端口（引擎由 llm.stop_engine 单独收）。"""
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass


def main():
    config.ensure_dirs()
    store.init_db()
    restored = apply_persisted_settings()
    httpd = create_server()
    host, port = httpd.server_address[0], httpd.server_address[1]
    gpu = llm.gpu_info()
    entries = llm.list_models()
    st = llm.status()

    print("=" * 62)
    print(" 本地模型工作台")
    print("=" * 62)
    print(" 界面地址   http://%s:%d" % (host, port))
    print(" 执行适配器 %s（%s）" % (st.get("adapter"), "托管进程" if st.get("managed") else "外部端点"))
    if st.get("base"):
        print(" 引擎地址   %s" % st["base"])
    print(" 模型目录   %s" % models.models_dir())
    print(" 已登记     %d 个模型" % len(entries))
    for e in entries:
        est = e.get("est") or {}
        fit = {True: "装得下", False: "装不下", None: "未知"}[e.get("fits")]
        print("            · %-28s %5.1f GB  %-7s %s ctx%d ≈ %d MiB（%s）" % (
            e["id"], e.get("size_gb") or 0, e.get("quant") or "?", fit,
            est.get("ctx", 0), est.get("total_mb", 0), fit))
    if not entries:
        print("            （空）把 .gguf 放进 models 目录即可，本程序不负责下载")
    if gpu:
        print(" 显存       %s  已用 %d / %d MiB" % (gpu["name"], gpu["used_mb"], gpu["total_mb"]))
    else:
        print(" 显存       未检测到 nvidia-smi（不影响使用）")
    print(" 上下文     默认 %d，硬上限 %d" % (config.CFG["ctx_max"], config.CFG["ctx_ceiling"]))
    if restored:
        print(" 默认参数   已从数据库恢复 %d 项（温度 %.2f / top_p %.2f / 触发 %.0f%%）" % (
            restored, config.CFG["temperature"], config.CFG["top_p"],
            config.CFG["trigger_ratio"] * 100))
    print(" 自动压缩   %s  触发 %.0f%% → 目标 %.0f%%  保留最近 %d 轮" % (
        "开启" if config.CFG["compact_enabled"] else "关闭",
        config.CFG["trigger_ratio"] * 100, config.CFG["target_ratio"] * 100,
        config.CFG["keep_recent_turns"]))
    print(" 请求快照   %s" % snapshot.level())
    print("=" * 62)
    print(" 引擎按需启动：第一次对话时会自动加载模型")
    print(" 按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        llm.stop_engine()


if __name__ == "__main__":
    main()
