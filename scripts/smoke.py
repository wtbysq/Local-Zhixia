"""端到端冒烟测试。

需要同时运行 mock 推理服务与后端（不加载真模型，跑得快）：
    python scripts/mock_llm.py --port 11999
    ADAPTER=openai LLM_BASE=http://127.0.0.1:11999/v1 python server/main.py
    python scripts/smoke.py --base http://127.0.0.1:8787

覆盖：流式逐帧、自动压缩触发、摘要落库、压缩不删原始消息、停止生成、导出、
     模型会话绑定（不可中途更换）、请求快照、以及压缩降级链（单元级）。
"""
import argparse
import http.client
import json
import os
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   → " + detail) if detail else ""))


class Client:
    def __init__(self, base):
        u = urlsplit(base)
        self.host = u.hostname
        self.port = u.port or 80

    def _conn(self, timeout=120):
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def json(self, method, path, body=None):
        c = self._conn(30)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        c.request(method, path, payload, headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return r.status, {"raw": raw[:300]}

    def chat(self, sid, content, params=None):
        """发起一次流式对话，返回 (events, text, ttft_ms, elapsed_ms)。"""
        c = self._conn(180)
        body = {"session_id": sid, "content": content}
        if params:
            body["params"] = params
        c.request("POST", "/api/chat", json.dumps(body).encode(),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        if r.status != 200:
            raw = r.read().decode("utf-8", "replace")
            c.close()
            raise RuntimeError("chat HTTP %d: %s" % (r.status, raw[:300]))

        events, text, buf = [], "", ""
        t0 = time.time()
        ttft = None
        read1 = getattr(r, "read1", None)
        data_lines, ev_name = [], "message"
        while True:
            blk = read1(4096) if read1 else r.read(1)
            if not blk:
                break
            buf += blk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line == "":
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            ev = {"type": ev_name, "data": json.loads(raw)}
                        except Exception:
                            ev = {"type": ev_name, "raw": raw}
                        events.append(ev)
                        if ev["type"] == "delta":
                            if ttft is None:
                                ttft = int((time.time() - t0) * 1000)
                            text += ev["data"].get("t", "")
                    data_lines, ev_name = [], "message"
                elif line.startswith("event:"):
                    ev_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
        c.close()
        return events, text, ttft, int((time.time() - t0) * 1000)


def find(events, name):
    for e in events:
        if e["type"] == name:
            return e["data"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--turns", type=int, default=6)
    args = ap.parse_args()
    cli = Client(args.base)

    print("\n=== 1. 健康检查 ===")
    status, h = cli.json("GET", "/api/health")
    check("GET /api/health 返回 200", status == 200, "status=%d" % status)
    check("推理后端已连通", bool(h.get("backend_ok")),
          str((h.get("error") or {}).get("message", ""))[:80])
    check("能列出模型", bool(h.get("models")), "models=%d" % len(h.get("models") or []))
    check("健康检查带适配器信息", bool((h.get("engine") or {}).get("adapter")),
          str((h.get("engine") or {}).get("adapter")))

    print("\n=== 1.5 模型登记（L0 资产层）===")
    status, d = cli.json("GET", "/api/models")
    ms = d.get("models") or []
    check("GET /api/models 返回 200", status == 200, "status=%d" % status)
    check("模型列表带显存估算", bool(ms) and all("est" in m for m in ms),
          "首个 est.total=%s MiB" % (((ms[0] if ms else {}).get("est") or {}).get("total_mb")))

    print("\n=== 2. 建会话（小 ctx 配置，便于快速触发压缩）===")
    params = {
        "ctx_max": 8192, "max_tokens": 256, "temperature": 0.7,
        "compact_enabled": True, "trigger_ratio": 0.4, "target_ratio": 0.25,
        "keep_recent_turns": 2,
    }
    status, d = cli.json("POST", "/api/sessions", {"title": "冒烟测试", "params": params})
    sid = (d.get("session") or {}).get("id")
    check("创建会话成功", status == 201 and bool(sid), "id=%s" % sid)

    print("\n=== 3. 连续对话 %d 轮，观察流式与压缩 ===" % args.turns)
    compact_seen = None
    all_ttft = []
    for i in range(args.turns):
        events, text, ttft, elapsed = cli.chat(sid, "第 %d 轮提问：解释一下上下文为什么会被压缩" % (i + 1))
        deltas = [e for e in events if e["type"] == "delta"]
        usage = find(events, "usage")
        meta = find(events, "meta")
        comp = find(events, "compact")
        if comp and comp.get("compacted") and not compact_seen:
            compact_seen = comp
        all_ttft.append(ttft or elapsed)
        print("    轮次 %d：delta=%2d 首字=%4s ms 总耗时=%4d ms 上下文=%s 压缩=%s"
              % (i + 1, len(deltas), ttft, elapsed,
                 (meta or {}).get("ctx_used"),
                 (comp.get("mode") if comp else "否")))
        if i == 0:
            check("首轮收到 ≥5 个 delta 帧（真流式）", len(deltas) >= 5, "delta=%d" % len(deltas))
            check("delta 帧到达早于整体完成",
                  ttft is not None and ttft < elapsed * 0.85,
                  "ttft=%sms elapsed=%sms" % (ttft, elapsed))
            check("收到 usage 事件", usage is not None and "tps" in (usage or {}),
                  "tps=%s" % (usage or {}).get("tps"))
        check("第 %d 轮拿到非空回答" % (i + 1), len(text) > 100, "chars=%d" % len(text))

    check("触发了自动压缩", compact_seen is not None,
          "mode=%s turns=%s" % ((compact_seen or {}).get("mode"), (compact_seen or {}).get("turns")))
    if compact_seen:
        check("压缩模式为 llm 或 extractive",
              compact_seen.get("mode") in ("llm", "extractive"), str(compact_seen.get("mode")))
        check("压缩后占用低于压缩前",
              compact_seen.get("after", 0) < compact_seen.get("before", 0),
              "%s → %s" % (compact_seen.get("before"), compact_seen.get("after")))
        check("压缩后占用低于触发阈值",
              compact_seen.get("after", 0) < 8192 * 0.4,
              "after=%s 阈值=%d" % (compact_seen.get("after"), int(8192 * 0.4)))
        check("保留了摘要正文", len(compact_seen.get("summary") or "") > 80,
              "summary_len=%d" % len(compact_seen.get("summary") or ""))

    print("\n=== 4. 原始消息未被删除（压缩只影响发送视图）===")
    status, d = cli.json("GET", "/api/sessions/%s/messages" % sid)
    msgs = d.get("messages") or []
    expect = args.turns * 2
    check("消息条数 = 轮数×2（原文完整）", len(msgs) == expect,
          "实际 %d，期望 %d" % (len(msgs), expect))
    sess = d.get("session") or {}
    check("摘要已落库", bool(sess.get("summary")), "covered=%s" %
          ((sess.get("summary") or {}).get("covered_turns")))
    check("compact_count ≥ 1", (sess.get("compact_count") or 0) >= 1,
          "count=%s" % sess.get("compact_count"))

    print("\n=== 5. 停止生成 ===")
    stop_result = {}

    def do_chat():
        try:
            ev, txt, _, _ = cli.chat(sid, "请写一段很长的内容用于测试中断")
            stop_result["events"] = ev
            stop_result["text"] = txt
        except Exception as exc:
            stop_result["error"] = str(exc)

    t = threading.Thread(target=do_chat)
    t.start()
    time.sleep(0.45)
    cli.json("POST", "/api/chat/stop", {"session_id": sid})
    t.join(30)
    evs = stop_result.get("events") or []
    done = find(evs, "done") or {}
    check("中断请求返回了 done 事件", bool(done), str(stop_result.get("error", ""))[:80])
    check("done.finish_reason 标记为 aborted", done.get("finish_reason") in ("aborted", "stop"),
          str(done.get("finish_reason")))
    status, d = cli.json("GET", "/api/sessions/%s/messages" % sid)
    check("中断后消息仍然落盘", len(d.get("messages") or []) >= expect + 2,
          "messages=%d" % len(d.get("messages") or []))

    print("\n=== 6. 导出 ===")
    c = cli._conn(20)
    c.request("GET", "/api/sessions/%s/export?format=md" % sid)
    r = c.getresponse()
    md = r.read().decode("utf-8", "replace")
    c.close()
    check("导出 Markdown 非空", r.status == 200 and len(md) > 200, "bytes=%d" % len(md))
    check("导出内容包含历史摘要", "历史摘要" in md or "## 我" in md, "")

    print("\n=== 6.5 模型会话绑定（Q2：途中不可换）===")
    status, d = cli.json("POST", "/api/chat",
                         {"session_id": sid, "content": "换模型试试", "model": "另一个模型"})
    check("对话途中换模型被拒绝（409 MODEL_LOCKED）", status == 409 and
          (d.get("error") or {}).get("code") == "MODEL_LOCKED",
          "status=%d code=%s" % (status, (d.get("error") or {}).get("code")))
    status, d = cli.json("PATCH", "/api/sessions/%s" % sid, {"model": "另一个模型"})
    check("PATCH 换模型同样被拒绝", status == 409 and
          (d.get("error") or {}).get("code") == "MODEL_LOCKED",
          "status=%d" % status)
    status, d = cli.json("GET", "/api/sessions/%s/messages" % sid)
    check("拒绝后消息数未变（没有脏写）",
          len(d.get("messages") or []) == expect + 2,
          "messages=%d" % len(d.get("messages") or []))
    status, d2 = cli.json("POST", "/api/sessions", {"title": "空会话可改模型"})
    empty_sid = (d2.get("session") or {}).get("id")
    status, d3 = cli.json("PATCH", "/api/sessions/%s" % empty_sid, {"model": "另一个模型"})
    check("未开始对话的会话允许改模型", status == 200, "status=%d" % status)

    print("\n=== 6.6 请求快照（Q3：按配置记录）===")
    status, d = cli.json("GET", "/api/snapshots?limit=5")
    snaps = d.get("snapshots") or []
    check("快照已落盘", bool(snaps), "level=%s count=%d" % (d.get("level"), len(snaps)))
    check("basic 档不含正文（不膨胀磁盘）",
          all("messages" not in s and "thinking" not in s for s in snaps), "")
    if snaps:
        s0 = snaps[0]
        check("快照记录参数/耗时/上下文",
              bool(s0.get("params")) and bool(s0.get("usage")) and bool(s0.get("ctx")), "")
        check("快照记录了模型与适配器",
              bool(s0.get("model")) and bool(s0.get("adapter")), "")

    print("\n=== 6.7 批量删除 ===")
    made = []
    for i in range(3):
        st, d = cli.json("POST", "/api/sessions", {"title": "待删 %d" % i})
        made.append((d.get("session") or {}).get("id"))
    st, d = cli.json("POST", "/api/sessions/batch-delete", {"ids": made[:2]})
    check("批量删除返回删除数量", st == 200 and d.get("deleted") == 2,
          "status=%d deleted=%s" % (st, d.get("deleted")))
    ids = [s["id"] for s in (cli.json("GET", "/api/sessions")[1].get("sessions") or [])]
    check("被选中的会话已消失", all(x not in ids for x in made[:2]), "")
    check("未选中的会话仍在", made[2] in ids, "")
    st, d = cli.json("POST", "/api/sessions/batch-delete", {"ids": ["s_not_exist"]})
    check("删除不存在的 id 不报错、计数为 0", st == 200 and d.get("deleted") == 0,
          "status=%d deleted=%s" % (st, d.get("deleted")))
    st, d = cli.json("POST", "/api/sessions/batch-delete", {"ids": []})
    check("空 ids 被拒绝（400 BAD_IDS）", st == 400 and
          (d.get("error") or {}).get("code") == "BAD_IDS", "status=%d" % st)
    cli.json("DELETE", "/api/sessions/%s" % made[2])

    print("\n=== 6.8 默认参数持久化 ===")
    st, d = cli.json("POST", "/api/settings",
                     {"temperature": 0.55, "trigger_ratio": 0.82, "enable_thinking": True,
                      "adapter": "hack", "min_turns": 99})
    check("设置接口返回已应用", st == 200 and d.get("applied", {}).get("temperature") == 0.55,
          "applied=%s" % (d.get("applied") or {}))
    check("设置接口拒绝非白名单键",
          "adapter" in (d.get("rejected") or {}), "rejected=%s" % list(d.get("rejected") or {}))
    st, d = cli.json("GET", "/api/settings")
    cfg = d.get("config") or {}
    check("GET /api/settings 反映新值",
          abs(float(cfg.get("temperature", 0)) - 0.55) < 1e-6 and
          abs(float(cfg.get("trigger_ratio", 0)) - 0.82) < 1e-6,
          "temperature=%s trigger=%s" % (cfg.get("temperature"), cfg.get("trigger_ratio")))
    # 还原，别把测试值留给下一次运行
    cli.json("POST", "/api/settings",
             {"temperature": 0.7, "trigger_ratio": 0.75, "enable_thinking": False})

    print("\n=== 6.9 图片/视频生成接口（参数校验与产物登记）===")
    st, d = cli.json("GET", "/api/models")
    counts = d.get("counts") or {}
    check("模型按三类分别计数",
          set(counts.keys()) >= {"text", "image", "video"},
          "counts=%s" % counts)
    check("模型条目带 kind 字段",
          all("kind" in m for m in (d.get("models") or [])),
          "n=%d" % len(d.get("models") or []))

    st, d = cli.json("GET", "/api/models")
    ms = d.get("models") or []
    img_id = next((m["id"] for m in ms if m.get("kind") == "image"), None)
    txt_id = next((m["id"] for m in ms if m.get("kind") == "text"), None)

    st, d = cli.json("POST", "/api/generate", {"model": "不存在的模型", "prompt": "x"})
    check("不存在的模型 → 400 MODEL_NOT_FOUND",
          st == 400 and (d.get("error") or {}).get("code") == "MODEL_NOT_FOUND",
          "status=%s code=%s" % (st, (d.get("error") or {}).get("code")))

    if img_id:
        st, d = cli.json("POST", "/api/generate", {"model": img_id, "prompt": "   "})
        check("空提示词 → 400 EMPTY_PROMPT",
              st == 400 and (d.get("error") or {}).get("code") == "EMPTY_PROMPT",
              "status=%s code=%s" % (st, (d.get("error") or {}).get("code")))
    if txt_id:
        st, d = cli.json("POST", "/api/generate", {"model": txt_id, "prompt": "x"})
        check("用语言模型去生成 → 400 WRONG_KIND",
              st == 400 and (d.get("error") or {}).get("code") == "WRONG_KIND",
              "status=%s code=%s" % (st, (d.get("error") or {}).get("code")))

    st, d = cli.json("GET", "/api/generated")
    check("产物列表可读", st == 200 and isinstance(d.get("generated"), list),
          "n=%d" % len(d.get("generated") or []))

    print("\n=== 6.10 显存估算（图片/视频不走 KV 公式）===")
    from server import models as _models
    for e in _models.scan():
        if e.get("kind") in ("image", "video"):
            dd = _models.decorate(e, 8192, "q8_0")
            est = dd["est"]
            check("%s 估算不用 KV 项" % e["id"],
                  est["kv_mb"] == 0 and est["exact"] is False,
                  "kv=%s total=%s" % (est["kv_mb"], est["total_mb"]))
            check("%s 估算含计算缓冲" % e["id"],
                  est["overhead_mb"] > 0 and est["total_mb"] > est["weights_mb"],
                  "overhead=%s" % est["overhead_mb"])

    print("\n=== 7. 压缩策略（单元级，不依赖服务）===")
    from server import context

    base_cfg = dict(ctx_max=8192, max_tokens=256, safety_margin=256, trigger_ratio=0.55,
                    target_ratio=0.35, keep_recent_turns=2, min_turns=3, max_batch_turns=12,
                    summary_cap=600, summary_timeout=20, compact_enabled=True)

    msgs = []
    for i in range(10):
        msgs.append({"role": "user", "content": "问题 %d：这是一段用于测试压缩的中等长度提问内容。" % i})
        msgs.append({"role": "assistant",
                     "content": "回答 %d：" % i + "这是较长的回答内容，用于把上下文推高。" * 40})
    original_tail = [m["content"] for m in msgs[-4:]]

    def boom(prev, turns, max_tokens):
        raise RuntimeError("模拟摘要失败")

    res = context.build_context(msgs, summarize_fn=boom, cfg=base_cfg)
    check("摘要失败时降级为抽取式", res["info"]["mode"] == "extractive", str(res["info"]["mode"]))
    check("降级后仍有摘要产出", bool(res["summary"] and res["summary"]["text"]),
          "len=%d" % len((res["summary"] or {}).get("text", "")))
    check("降级后占用低于触发阈值", res["info"]["after"] < int(8192 * 0.55),
          "after=%s" % res["info"]["after"])
    check("摘要长度受 token 上限约束",
          (res["summary"] or {}).get("tokens", 0) <= 600,
          "summary_tokens=%s" % (res["summary"] or {}).get("tokens"))
    tail = [m["content"] for m in res["messages"][-4:]]
    check("最近 4 条消息逐字保留原文", tail == original_tail,
          "保留 %d 条，各长度 %s" % (len(tail), [len(x) for x in tail]))
    check("摘要以 system 角色注入在首位",
          res["messages"][0]["role"] == "system", res["messages"][0]["role"])

    res_llm = context.build_context(
        msgs,
        summarize_fn=lambda p, t, m: "## 目标与约束\n本会话在调上下文压缩。\n## 已达成的结论\n按轮次压缩有效。",
        cfg=base_cfg)
    check("摘要正常时走 LLM 模式", res_llm["info"]["mode"] == "llm", str(res_llm["info"]["mode"]))

    res_few = context.build_context(msgs[:4], summarize_fn=boom, cfg=base_cfg)
    check("内容不足时不压缩", res_few["info"]["compacted"] is False, str(res_few["info"]["reason"]))

    res_off = context.build_context(msgs, summarize_fn=boom, cfg=dict(base_cfg, compact_enabled=False))
    check("关闭开关后不压缩", res_off["info"]["compacted"] is False, str(res_off["info"]["reason"]))

    tiny = context.build_context(
        msgs, summarize_fn=boom,
        cfg=dict(base_cfg, ctx_max=2048, trigger_ratio=0.55, target_ratio=0.35))
    tiny_usable = 2048 - 256 - 256
    check("极小上下文下不崩溃且 payload 受控", tiny["tokens"] <= tiny_usable * 1.02,
          "tokens=%s usable=%s" % (tiny["tokens"], tiny_usable))
    check("极小上下文下最新提问未被切坏",
          any("问题 9" in (m.get("content") or "") for m in tiny["messages"]), "")

    print("\n" + "=" * 58)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(" 结果：%d / %d 通过" % (passed, total))
    if passed != total:
        print(" 失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print("   - %s (%s)" % (name, detail))
    print("=" * 58)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
