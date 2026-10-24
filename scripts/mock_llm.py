"""离线 mock 推理服务（OpenAI 兼容）。

用途：在没有真实模型、也没有网络的情况下，验证后端的流式转发、SSE 帧、
上下文压缩、落盘与导出是否真的工作。

    python scripts/mock_llm.py --port 11999

它实现两个端点：
    GET  /v1/models
    POST /v1/chat/completions   （stream=true / false 均支持）

特殊行为（便于测试降级）：
    环境变量 MOCK_FAIL_SUMMARY=1 时，凡是摘要请求都返回 500，
    用来验证后端的"LLM 摘要失败 → 抽取式压缩"降级链。
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILLER = ("这一段是用于测试的占位内容，目的是让上下文稳定增长，从而触发自动压缩逻辑。"
          "在真实环境中这里会是模型生成的回答，可能包含分析、步骤、代码与结论。")

STATE = {"requests": 0, "lock": threading.Lock()}


def build_reply(question, target_chars=1100):
    head = "关于「%s」，可以分成三点来看：\n\n" % (question[:40] if question else "这个问题")
    points = ("1. 第一点是从原理层面理解：缓存复用的本质是用空间换时间。\n"
              "2. 第二点是工程取舍：显存是硬约束，超了就直接失败，没有 swap 可退。\n"
              "3. 第三点是可观测性：没有打点就没有优化方向。\n\n")
    code = ("```python\n"
            "def estimate_kv(layers, kv_heads, head_dim, ctx, bytes_per=2):\n"
            "    \"\"\"估算 KV cache 占用字节数\"\"\"\n"
            "    per_token = 2 * layers * kv_heads * head_dim * bytes_per\n"
            "    return per_token * ctx\n"
            "```\n\n")
    body = (head + points + code)
    while len(body) < target_chars:
        body += FILLER + "\n\n"
    body += "以上就是对这个问题的完整说明。"
    return body


def build_summary(req_text):
    return ("## 目标与约束\n"
            "用户在调试本地大模型对话界面的上下文管理，要求离线、低依赖。\n\n"
            "## 已达成的结论\n"
            "- 上下文占用需要按轮次压缩，不能按单条消息切分。\n"
            "- 摘要长度控制在原文的 18%% 左右比较合适。\n\n"
            "## 关键事实与数据\n"
            "- KV cache 每 token 约 128 KiB，8k 上下文约 1 GB。\n"
            "- 8GB 显存下主力配置为 8B Q4_K_M。\n\n"
            "## 未决问题\n"
            "- 是否需要在摘要里保留代码片段原文。")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json({"data": [
                {"id": "mock-8b", "object": "model", "owned_by": "mock",
                 "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"}},
                {"id": "mock-4b", "object": "model", "owned_by": "mock",
                 "details": {"parameter_size": "4B", "quantization_level": "Q4_K_M"}},
            ]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "bad json"}}, 400)

        with STATE["lock"]:
            STATE["requests"] += 1
            n = STATE["requests"]

        messages = payload.get("messages") or []
        is_summary = any("对话历史压缩器" in (m.get("content") or "") for m in messages)
        if is_summary and os.environ.get("MOCK_FAIL_SUMMARY") == "1":
            return self._json({"error": {"message": "模拟摘要失败（测试降级）"}}, 500)

        question = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                question = m.get("content") or ""
                break

        if is_summary:
            text = build_summary(question)
        else:
            text = build_reply(question, int(os.environ.get("MOCK_LEN", "1100")))
        # 让摘要和非摘要内容都可辨识
        if is_summary:
            text = "[#%d 摘要] " % n + text

        if payload.get("stream"):
            return self._stream(text, is_summary)
        return self._json({
            "id": "cmpl-mock-%d" % n,
            "object": "chat.completion",
            "model": payload.get("model") or "mock-8b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": len(text) // 2, "total_tokens": 0},
        })

    def _stream(self, text, is_summary):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8")
            self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        # 摘要请求：模拟一定耗时，便于观察压缩前后的日志
        step = 24 if is_summary else 28
        delay = 0.03 if is_summary else 0.008
        for i in range(0, len(text), step):
            chunk = text[i:i + step]
            send({"id": "cmpl-mock", "object": "chat.completion.chunk",
                  "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]})
            time.sleep(delay)
        send({"id": "cmpl-mock", "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        send({"id": "cmpl-mock", "object": "chat.completion.chunk", "choices": [],
              "usage": {"prompt_tokens": 512, "completion_tokens": len(text) // 2,
                        "total_tokens": 512 + len(text) // 2}})
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11999)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("mock 推理服务已启动: http://%s:%d/v1" % (args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
