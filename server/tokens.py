"""token 估算。

说明：这里是**估算**，不是精确分词。精确值以推理服务返回的 usage 为准，
本地估算只用于两件事：
  1. 压缩触发前的预判（宁可估高，避免撞上下文上限）
  2. 服务端不返回 usage 时的兜底

中英混合的经验系数：中文约 1 字 ≈ 1 token，英文约 3.5 字符 ≈ 1 token。
"""
import re

_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")

# 每条消息的角色/分隔符开销
MSG_OVERHEAD = 4


def estimate(text):
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    # 中文系数取 1.05 略偏保守；英文 3.5 字符/token 接近主流 BPE 分词器
    return int(cjk * 1.05 + other / 3.5) + 1


def estimate_message(msg):
    content = msg.get("content") or ""
    return estimate(content) + MSG_OVERHEAD


def estimate_messages(msgs):
    return sum(estimate_message(m) for m in msgs)
