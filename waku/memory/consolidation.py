"""Consolidation — distilling chats into durable memory, but only sometimes.

The whiteboard's diamond: "only consolidate after N new chats". Running a
summarizer after every message is wasteful and noisy; batching N exchanges
gives the summarizer enough context to extract facts worth keeping.

A cheap model reads the unconsolidated chat log and produces:
  - facts   → semantic memory ("Alex prefers morning meetings")
  - episode → episodic memory ("2026-07-10: planned the Acme demo with Alex")
"""

from __future__ import annotations

import json
from datetime import date

import anthropic

from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore

SUMMARIZER_PROMPT = """\
You distill a personal assistant's recent conversation into long-term memory.

From the exchanges below, extract:
1. durable facts about the user, their people, projects, or preferences —
   only things worth remembering in a month; skip chit-chat and one-offs.
2. one single-sentence episode summarizing what happened in this conversation.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>"}}

Exchanges:
{log}"""


def consolidate_if_due(
    conn,
    client: anthropic.Anthropic,
    small_model: str,
    every_n: int,
    facts: SqliteFactStore,
    episodes: SqliteEpisodeStore,
) -> int:
    """达到 exchange 阈值时把未巩固 chat 批量提炼为 facts 与一个 episode。

    @param ① conn: 包含 chat_log 的 SQLite 连接。
           ② client: 提供 messages.create() 的 Anthropic shape 模型 client。
           ③ small_model: 执行批量摘要的低成本模型 id。
           ④ every_n: 触发一次 consolidation 所需的 exchange 数量。
           ⑤ facts: semantic store, 接收模型提炼出的 durable facts。
           ⑥ episodes: episodic store, 接收本批对话的一句话事件摘要。
    @return: distilled facts 数组的数量, 未到期、无内容或模型解析失败时为 0。
    side effect: 可能调用模型、写 facts/episodes、把已处理 chat_log 标为 consolidated 并提交。
    called by: Memory.maybe_consolidate() 在每个 exchange 落库之后调用。
    """
    # Step 1: 先读取全部未巩固行。一个 exchange 固定写 user/assistant 两行, 因而阈值乘以 2。
    rows = conn.execute(
        "SELECT id, role, content FROM chat_log WHERE consolidated = 0 ORDER BY id"
    ).fetchall()
    if len(rows) < every_n * 2:  # each exchange = 2 rows (user + assistant)
        return 0

    # Step 2: 把达到阈值的完整批次交给 small model, 只接受约定的 JSON facts/episode 协议。
    log = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    try:
        response = client.messages.create(
            model=small_model,
            max_tokens=600,
            messages=[{"role": "user", "content": SUMMARIZER_PROMPT.format(log=log)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        distilled = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except Exception:
        # 失败不推进状态: rows 继续保持 consolidated=0, 下一次 turn 仍可用原始 chat 重试。
        # 这条兜底优先保证信息不丢失, 即使代价是稍后再次调用 summarizer。
        return 0  # never lose the log — it stays unconsolidated for next time

    # Step 3: 先写长期记忆内容。只有字段完整的 fact 才进入 semantic store。
    for fact in distilled.get("facts", []):
        if fact.get("subject") and fact.get("content"):
            facts.add(fact["subject"], fact["content"], source="consolidation")
    if distilled.get("episode"):
        episodes.add(distilled["episode"], happened_at=date.today().isoformat())

    # Step 4: 仅在模型响应已解析且记忆写入流程完成后推进 chat 状态。
    # 标记发生在最后, 防止 summarizer 失败时把仍未提炼的原始对话误判为已处理。
    conn.execute(
        f"UPDATE chat_log SET consolidated = 1 WHERE id IN ({','.join('?' * len(rows))})",
        [r["id"] for r in rows],
    )
    conn.commit()
    return len(distilled.get("facts", []))
