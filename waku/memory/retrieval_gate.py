"""HERO MOMENT #1 — the gate that decides WHETHER to retrieve memory at all.

The top audience question across platforms: "why hit the memory store every
turn?" Default-on retrieval is (a) slow — an extra search before every reply —
and (b) worse: irrelevant memories bias the answer ("over-interpretation").

So before touching any store, a cheap fast model answers one question:
    does THIS message need the user's memory?
"what's 2+2" → no. "when am I meeting Alex?" → yes, and here's the search query.

Cost: one small-model call (~a few hundred tokens). Payoff: retrieval only
when it helps. This is the same judge pattern as LLM-as-judge in evals —
a small model making one narrow decision.
"""

from __future__ import annotations

import json

import anthropic

GATE_PROMPT = """\
You are a retrieval gate for a personal assistant's long-term memory.
Given the user's message, decide if answering well requires the user's stored
memories (facts about people, projects, preferences, or past events).

Reply with ONLY this JSON, nothing else:
{{"retrieve": true/false, "query": "<search keywords if true, else empty>", "reason": "<5 words>"}}

General knowledge, math, small talk, or self-contained requests → false.
Anything referencing the user's life, people, plans, or history → true.

User message: {message}"""


def should_retrieve(
    client: anthropic.Anthropic, small_model: str, message: str
) -> tuple[bool, str, str]:
    """用 small model 判断当前消息是否需要长期记忆, 并把结果归一化为检索决策元组。

    @param ① client: 提供 messages.create() 的 Anthropic shape 模型 client。
           ② small_model: 专用于窄判定任务的低成本模型 id。
           ③ message: 当前用户原文, gate 失败时也会直接作为回退查询词。
    @return: (是否检索, 搜索 query, 判定原因), 供 Memory.gated_retrieve() 继续执行。
    side effect: 发起一次 small model 网络请求, 但不直接访问 memory store。
    called by: Memory.gated_retrieve() 在每个 turn 构建 system prompt 时调用。
    """
    try:
        # Step 1: 把开放式用户输入压缩成一个严格 JSON 判定, 限制输出 token 控制成本。
        response = client.messages.create(
            model=small_model,
            max_tokens=100,
            messages=[{"role": "user", "content": GATE_PROMPT.format(message=message)}],
        )

        # Step 2: 只截取首尾花括号之间的内容, 容忍模型偶尔包裹少量非 JSON 文本。
        text = "".join(b.text for b in response.content if b.type == "text")
        decision = json.loads(text[text.index("{") : text.rindex("}") + 1])

        # Step 3: 缺少 query 时回退到原消息, 保证 retrieve=True 总有可执行的搜索词。
        return bool(decision.get("retrieve")), decision.get("query", message), decision.get("reason", "")
    except Exception as exc:
        # fail-open 是有意的可用性选择: gate 不是 memory 的单点故障。
        # 判定失败时宁可多做一次检索, 也不能把可能相关的长期记忆静默丢掉。
        return True, message, f"gate failed open ({type(exc).__name__})"
