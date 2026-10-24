"""执行层（L1）的接口与 OpenAI 兼容实现。

核心代码只依赖本文件：换引擎 = 换一个 Adapter 子类 + 改一行配置。

为什么把 HTTP 客户端放在基类里：
llama-server、任意 OpenAI 兼容端点、mock 服务，暴露的都是同一套 /v1 契约，
差别只在「谁来启动进程」和「模型名怎么映射」。把共用的传输层放在基类，
子类只剩真正的差异（进程生命周期），这也是「可替换」在代码上的落点。
"""
import http.client
import json
import socket
import subprocess
import time
from urllib.parse import urlsplit


class BackendError(Exception):
    """执行层异常。code 会透传给前端与 CLI。"""

    def __init__(self, code, message, status=None, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


_OOM_HINTS = ("out of memory", "cublas", "cuda error", "unable to allocate",
              "failed to allocate", "not enough memory", "oom", "insufficient memory")


def classify(status, body):
    """把引擎的 HTTP 错误翻译成稳定的错误码，前端与脚本都按它判断。"""
    low = (body or "").lower()
    if any(h in low for h in _OOM_HINTS):
        return "OOM"
    if status == 404:
        return "MODEL_NOT_FOUND"
    if status == 503:
        return "ENGINE_LOADING"
    if status in (400, 422):
        if "context" in low or "too long" in low or "exceed" in low:
            return "CTX_OVERFLOW"
        return "BAD_REQUEST"
    return "BACKEND_ERROR"


def _progress(msg):
    """启动期把关键日志打到 stderr，方便 doctor / 用户看到卡在哪一步。"""
    try:
        import sys
        sys.stderr.write("  [engine] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def gpu_info(force=False):
    """读取显存占用。5 秒缓存，失败返回 None 而不是抛错。

    force=True 供"停引擎后等显存回落"的场景使用，否则会一直读到旧值。
    """
    global _GPU_CACHE
    now = time.time()
    if not force and _GPU_CACHE["data"] is not None and now - _GPU_CACHE["at"] < 5:
        return _GPU_CACHE["data"]
    data = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, creationflags=flags,
        )
        if out.returncode == 0 and out.stdout.strip():
            name, total, used = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
            data = {"name": name, "total_mb": int(float(total)), "used_mb": int(float(used)),
                    "free_mb": int(float(total) - float(used))}
    except Exception:
        data = None
    _GPU_CACHE.update({"at": now, "data": data})
    return data


_GPU_CACHE = {"at": 0.0, "data": None}


class Adapter:
    """所有执行适配器的共同契约。"""

    name = "base"
    label = "未知执行器"
    managed = False          # 是否由本程序负责拉起/回收进程

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self._active = 0
        self._active_lock = None

    # ---- 生命周期：managed 适配器覆写这两个 ----

    def ensure(self, model_id, ctx):
        """确保引擎就绪、且加载的是指定模型。返回描述 dict。"""
        return {"ok": True, "adapter": self.name, "managed": self.managed}

    def stop(self):
        return None

    def status(self):
        return {"adapter": self.name, "label": self.label, "managed": self.managed,
                "base": self.base}

    # ---- 传输 ----

    @property
    def base(self):
        return (self.cfg.get("base") or "").rstrip("/")

    def _target(self, path):
        parts = urlsplit(self.base)
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if scheme == "https" else 80)
        base_path = (parts.path or "").rstrip("/")
        return scheme, host, port, base_path + path

    def _open(self, method, path, payload=None, timeout=60):
        scheme, host, port, full = self._target(path)
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, full, body=body, headers=headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            conn.close()
            raise BackendError("BACKEND_DOWN",
                               "无法连接执行器 %s：%s" % (self.base, exc))
        return conn, resp

    @staticmethod
    def _read_all(resp):
        try:
            return resp.read().decode("utf-8", "replace")
        except Exception:
            return ""

    # ---- 能力 ----

    def list_models(self):
        conn, resp = self._open("GET", "/models", timeout=5)
        try:
            raw = self._read_all(resp)
            if resp.status != 200:
                raise BackendError(classify(resp.status, raw), raw[:300] or "模型列表获取失败", resp.status)
            data = json.loads(raw or "{}")
            out = []
            for m in data.get("data", []) or []:
                mid = m.get("id") or m.get("model") or m.get("name")
                if mid:
                    out.append({"id": mid, "served_as": mid})
            return out
        except json.JSONDecodeError:
            raise BackendError("BAD_RESPONSE", "执行器返回了无法解析的模型列表")
        finally:
            conn.close()

    def chat_once(self, messages, temperature=0.2, max_tokens=600, timeout=None, model=None):
        payload = {
            "model": self.resolve_name(model),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        conn, resp = self._open("POST", "/chat/completions", payload, timeout=timeout or 60)
        try:
            raw = self._read_all(resp)
            if resp.status != 200:
                raise BackendError(classify(resp.status, raw), raw[:300] or "请求失败", resp.status)
            data = json.loads(raw)
            choices = data.get("choices") or []
            if not choices:
                raise BackendError("EMPTY_RESPONSE", "执行器返回为空")
            msg = choices[0].get("message") or {}
            return (msg.get("content") or "").strip()
        except json.JSONDecodeError:
            raise BackendError("BAD_RESPONSE", "执行器返回了无法解析的内容")
        finally:
            conn.close()

    def resolve_name(self, model):
        """把本程序的模型 id 映射成引擎认识的名字。默认原样透传。"""
        return model or self.cfg.get("default_model") or ""

    def build_payload(self, messages, params, model):
        params = params or {}
        payload = {
            "model": self.resolve_name(model),
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": float(params.get("temperature", 0.7)),
            "top_p": float(params.get("top_p", 0.9)),
            "max_tokens": int(params.get("max_tokens", 1024)),
        }
        for extra in ("top_k", "repeat_penalty", "stop"):
            if params.get(extra) not in (None, "", []):
                payload[extra] = params[extra]
        # 思考开关：Qwen3 等模型的对话模板参数。默认关闭——
        # 实测开启时正文会是空的（内容全在 reasoning_content 里），对使用者是坏的默认值。
        tpl = dict(self.cfg.get("chat_template_kwargs") or {})
        if params.get("enable_thinking") is not None:
            tpl["enable_thinking"] = bool(params["enable_thinking"])
        if tpl:
            payload["chat_template_kwargs"] = tpl
        return payload

    @staticmethod
    def _iter_lines(resp):
        """稳定逐行读取，跨 chunk 的多字节字符不会被截断。"""
        read1 = getattr(resp, "read1", None)
        if callable(read1):
            buf = b""
            while True:
                try:
                    blk = read1(4096)
                except socket.timeout:
                    raise BackendError("BACKEND_TIMEOUT", "执行器响应超时")
                if not blk:
                    if buf.strip():
                        yield buf.decode("utf-8", "replace")
                    return
                buf += blk
                while True:
                    idx = buf.find(b"\n")
                    if idx < 0:
                        break
                    line, buf = buf[:idx], buf[idx + 1:]
                    yield line.decode("utf-8", "replace").rstrip("\r")
        else:
            for raw in resp:
                yield raw.decode("utf-8", "replace").rstrip("\r\n")

    def chat_stream(self, messages, params=None, model=None):
        """流式补全。逐块 yield 引擎原始 chunk，另加 think 通道。"""
        payload = self.build_payload(messages, params, model)
        conn, resp = self._open("POST", "/chat/completions", payload, timeout=600)
        try:
            if resp.status != 200:
                raw = self._read_all(resp)
                raise BackendError(classify(resp.status, raw), raw[:400] or "推理请求失败", resp.status)
            for line in self._iter_lines(resp):
                if not line or not line.startswith("data:"):
                    continue
                s = line[5:].strip()
                if not s:
                    continue
                if s == "[DONE]":
                    return
                try:
                    yield json.loads(s)
                except json.JSONDecodeError:
                    continue
        finally:
            conn.close()
