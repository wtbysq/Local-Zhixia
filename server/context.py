"""上下文预算、轮次分组与自动压缩（方案 2.3.1 的实现）。

三条不可妥协的规则：
  1. 压缩是"发送时视图"，本模块**从不修改数据库**，只返回要发送的 payload。
     原始消息由 store 层永久保留，用户可以随时回看原文。
  2. 压缩以"轮次"为单位，不拆开一问一答。
  3. 压缩失败不能让对话停下，必须有降级路径（LLM → 抽取式 → 滑窗截断）。
"""
import re
import time

from . import config
from .store import now_ms
from .tokens import estimate, estimate_message, estimate_messages

# ------------------------------------------------------------------ 轮次分组

def group_turns(messages):
    """把消息序列切成轮次。

    返回 (leading_system_msgs, turns)
    turn = {"user": msg|None, "assistant": msg|None, "notes": [msg], "tokens": int}
    """
    leading_system = []
    turns = []
    cur = None

    for m in messages:
        role = m.get("role")
        if role == "system":
            if not turns and cur is None:
                leading_system.append(m)
            else:
                if cur is None:
                    cur = {"user": None, "assistant": None, "notes": [], "tokens": 0}
                cur["notes"].append(m)
                cur["tokens"] += estimate_message(m)
            continue

        if role == "user":
            if cur is not None:
                turns.append(cur)
            cur = {"user": m, "assistant": None, "notes": [], "tokens": estimate_message(m)}
        else:  # assistant 及异常角色一律按模型输出来处
            if cur is None:
                cur = {"user": None, "assistant": m, "notes": [], "tokens": estimate_message(m)}
            elif cur["assistant"] is None:
                cur["assistant"] = m
                cur["tokens"] += estimate_message(m)
            else:
                turns.append(cur)
                cur = {"user": None, "assistant": m, "notes": [], "tokens": estimate_message(m)}

    if cur is not None:
        turns.append(cur)
    return leading_system, turns


def turn_messages(turn):
    """把一个轮次还原成 chat 消息序列（保证角色顺序合法）。"""
    out = []
    for note in turn.get("notes") or []:
        out.append({"role": "system", "content": note.get("content") or ""})
    if turn.get("user"):
        out.append({"role": "user", "content": turn["user"].get("content") or ""})
    if turn.get("assistant"):
        out.append({"role": "assistant", "content": turn["assistant"].get("content") or ""})
    return out


def turn_plain_text(turn, clip=1200):
    """把轮次压成一段纯文本，供摘要或抽取式压缩使用。"""
    parts = []
    if turn.get("user"):
        parts.append("用户：" + (turn["user"].get("content") or "")[:clip])
    if turn.get("assistant"):
        parts.append("助手：" + (turn["assistant"].get("content") or "")[:clip])
    return "\n".join(parts)


# ------------------------------------------------------------------ 抽取式压缩

_SENT_END = re.compile(r"[。！？!?\n；;]")


def _first_sentences(text, n, cap):
    text = (text or "").strip()
    if not text:
        return ""
    pieces, buf = [], ""
    for ch in text:
        buf += ch
        if _SENT_END.match(ch):
            if buf.strip():
                pieces.append(buf.strip())
            buf = ""
            if len(pieces) >= n:
                break
    if buf.strip() and len(pieces) < n:
        pieces.append(buf.strip())
    joined = " ".join(pieces) if pieces else text
    return joined[:cap]


def extractive_summary(turns, prev_text="", cap_chars=1400):
    """本地抽取式压缩：零推理，永不失败。作为 LLM 摘要的降级方案。"""
    lines = []
    for t in turns:
        q = _first_sentences((t.get("user") or {}).get("content", ""), 1, 80)
        a = _first_sentences((t.get("assistant") or {}).get("content", ""), 2, 160)
        if not q and not a:
            continue
        lines.append("- 问：%s｜答：%s" % (q or "（略）", a or "（略）"))
    body = "\n".join(lines) if lines else "- （无可提取内容）"
    head = ""
    if prev_text:
        head = "## 更早历史的摘要\n%s\n\n" % prev_text.strip()[:600]
    text = (head
            + "## 目标与约束\n（本地抽取式压缩，未经过模型，细节可能有损）\n\n"
            + "## 已达成的结论\n" + body + "\n\n"
            + "## 关键事实与数据\n（详见原始对话）\n\n"
            + "## 未决问题\n（详见原始对话）")
    return text[:cap_chars]


def _truncate_to_tokens(text, max_tokens):
    """按 token 估算截断。中英混排下不能按字符数截——中文 1 字约 1 token，
    英文 3.5 字符才 1 token，用字符数当上限会超出一到三倍。"""
    if not text or estimate(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if estimate(text[:mid]) <= max_tokens:
            lo = mid + 1
        else:
            hi = mid
    cut = max(0, lo - 1)
    return text[:cut].rstrip() + "…"


def _clip_middle(text, keep_chars):
    """保留首尾、切掉中间。长文截断时保住开头的指令与结尾的问题。"""
    if len(text) <= keep_chars:
        return text
    head = int(keep_chars * 0.55)
    tail = max(0, keep_chars - head - 12)
    return text[:head].rstrip() + "……（中间已截断）……" + (text[-tail:].lstrip() if tail else "")


# ------------------------------------------------------------------ 摘要提示词

SUMMARY_SYSTEM = """你是一个对话历史压缩器。你的任务是把给定的对话压缩成结构化摘要，供后续对话继续使用。

必须遵守：
1. 只保留对后续对话有用的信息，删除寒暄、重复、客套与无信息量的内容。
2. 严格按下面四个小标题输出，不要增加额外章节，不要写开场白或总结语：
## 目标与约束
## 已达成的结论
## 关键事实与数据
## 未决问题
3. 禁止出现"用户：""助手："这类对话标记，禁止模仿对话格式。
4. 保留具体的数字、文件名、代码标识符、错误信息原文，不要模糊化。
5. 总字数不超过 %d 字。"""


def build_summary_request(prev_text, turns, max_tokens, single_msg_cap=None):
    """构造摘要请求的 messages。"""
    sys_prompt = SUMMARY_SYSTEM % max(120, int(max_tokens * 1.4))
    chunks = []
    if prev_text:
        chunks.append("【更早历史的已有摘要】\n" + prev_text.strip())
    body = []
    for i, t in enumerate(turns, 1):
        body.append("--- 第 %d 轮 ---\n%s" % (i, turn_plain_text(t, clip=single_msg_cap or 1500)))
    chunks.append("【需要压缩的对话】\n" + "\n".join(body))
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "\n\n".join(chunks)},
    ]


# ------------------------------------------------------------------ 主流程

SUMMARY_TAG = "[对话历史摘要 · 覆盖前 %d 轮 · 由系统自动生成，非原始对话]"


def build_context(messages, system_prompt="", summarize_fn=None, session_summary=None, cfg=None):
    """构造本次请求的 payload，必要时自动压缩。

    summarize_fn(prev_text, turns, max_tokens) -> str
        由调用方注入（内部走 llm.chat_once），失败时抛异常即可触发降级。

    返回 dict：
        messages    实际发送给模型的 messages
        tokens      预计占用
        ctx_max / usable / ratio
        summary     需要写回数据库的新摘要（未变化则为 None）
        info        压缩详情，用于 SSE compact 事件
    """
    cfg = cfg or config.CFG
    ctx_max = int(cfg["ctx_max"])
    max_tokens = int(cfg["max_tokens"])
    margin = int(cfg["safety_margin"])
    usable = max(256, ctx_max - max_tokens - margin)
    keep_recent = int(cfg["keep_recent_turns"])
    min_turns = int(cfg["min_turns"])
    max_batch = int(cfg["max_batch_turns"])
    trigger = int(ctx_max * float(cfg["trigger_ratio"]))
    target = int(ctx_max * float(cfg["target_ratio"]))
    summary_cap = min(int(cfg["summary_cap"]), max(120, int(ctx_max * 0.22)))
    compact_enabled = bool(cfg["compact_enabled"])
    single_cap = int(ctx_max * 0.30)

    started = time.time()
    info = {
        "compacted": False, "mode": None, "turns": 0, "before": 0, "after": 0,
        "covered_turns": 0, "trimmed": 0, "ms": 0, "reason": "",
        "summary_tokens": 0, "detail": "",
    }

    leading_system, turns = group_turns(messages)
    base_system = (system_prompt or "").strip()
    if not base_system and leading_system:
        base_system = "\n\n".join(m.get("content") or "" for m in leading_system)

    summary = session_summary or None
    summary_text = (summary or {}).get("text") or ""
    covered = int((summary or {}).get("covered_turns") or 0)
    covered = max(0, min(covered, len(turns)))

    sys_tokens = estimate(base_system) if base_system else 0
    live = turns[covered:]
    full = sys_tokens + estimate(summary_text) + sum(t["tokens"] for t in live)
    info["before"] = full
    info["covered_turns"] = covered

    new_summary = None
    compressible = max(0, len(turns) - covered - keep_recent)

    # ---- C1 触发判定：用量超过阈值
    if not compact_enabled:
        info["reason"] = "disabled"
    elif full <= trigger:
        info["reason"] = "below_trigger"
    elif compressible < min_turns:
        info["reason"] = "too_few_turns"
    else:
        need_free = full - target
        picked, acc = [], 0
        for i in range(covered, len(turns) - keep_recent):
            picked.append(i)
            acc += turns[i]["tokens"]
            if acc >= need_free or len(picked) >= max_batch:
                break

        # ---- C5 收益判定：省不下 8% 就不值得多花一次推理
        est_summary = min(summary_cap, max(80, int(acc * 0.18)))
        projected = full - acc + est_summary
        if picked and projected < full * 0.92:
            selected = [turns[i] for i in picked]
            mode = None
            new_text = ""
            if summarize_fn is not None:
                try:
                    new_text = (summarize_fn(summary_text, selected, est_summary) or "").strip()
                    if not new_text:
                        raise ValueError("空摘要")
                    mode = "llm"
                except Exception as exc:
                    info["detail"] = "摘要失败，降级为抽取式：%s" % type(exc).__name__
                    mode = None
            if mode is None:
                new_text = extractive_summary(selected, summary_text, cap_chars=4000)
                mode = "extractive"

            # 摘要自身的硬上限：按 token 截断（不能按字符）
            new_text = _truncate_to_tokens(new_text, summary_cap)

            covered += len(picked)
            summary_text = new_text
            new_summary = {
                "text": new_text,
                "covered_turns": covered,
                "tokens": estimate(new_text),
                "mode": mode,
                "at": now_ms(),
            }
            info.update({
                "compacted": True, "mode": mode, "turns": len(picked),
                "summary_tokens": new_summary["tokens"], "covered_turns": covered,
            })
        else:
            info["reason"] = "no_gain"

    # ---- 组装 payload：system → 摘要 → 保留轮次
    live = turns[covered:]

    def assemble():
        msgs = []
        if base_system:
            msgs.append({"role": "system", "content": base_system})
        if summary_text:
            msgs.append({"role": "system", "content": (SUMMARY_TAG % covered) + "\n\n" + summary_text})
        for t in live:
            msgs.extend(turn_messages(t))
        return msgs

    payload = assemble()
    total = estimate_messages(payload)

    # ---- 仍然超出可用空间：滑窗丢弃最旧轮次（降级第三档）
    while total > usable and len(live) > 1:
        live.pop(0)
        info["trimmed"] += 1
        payload = assemble()
        total = estimate_messages(payload)

    # ---- 极端情况：单轮本身就超长，保留首尾、切掉中间
    # 先动助手内容（上一轮回答，信息密度最低），再动用户内容。
    # 注意：绝不从头部整段切掉用户消息——那会把真正的问题切没。
    if total > usable and live:
        last = dict(live[-1])
        live[-1] = last
        for field in ("assistant", "user"):
            target = last.get(field)
            if not target:
                continue
            content = (target.get("content") or "")
            keep = len(content)
            while total > usable and keep > 100:
                keep = int(keep * 0.7)
                last[field] = dict(target, content=_clip_middle(content, keep))
                payload = assemble()
                total = estimate_messages(payload)
            if total <= usable:
                break
        info["detail"] = (info["detail"] + "；" if info["detail"] else "") + "单条消息过长，已保留首尾截断中间"

    if info["trimmed"] and not info["compacted"]:
        info["mode"] = "trim"
    info["after"] = total
    info["ms"] = int((time.time() - started) * 1000)

    return {
        "messages": payload,
        "tokens": total,
        "ctx_max": ctx_max,
        "usable": usable,
        "max_tokens": max_tokens,
        "ratio": round(total / float(ctx_max), 4) if ctx_max else 0,
        "summary": new_summary,
        "info": info,
        "turns_total": len(turns),
        "reason": info["reason"],
    }
