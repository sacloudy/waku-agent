"""DETERMINISTIC EVAL — "did the meeting trigger?" This is a unit test.

No LLM judges anything here. Each case asserts a binary, checkable outcome:
the right tool fired (or didn't), with the right arguments, and the artifact
(DB row / outbox file) exists. 0 or 1. This is the half of eval that most
teams skip and shouldn't.

Two tiers:
  offline  — scripted model, always runs, tests OUR code (loop, tools, wiring)
  live     — real model, runs when ANTHROPIC_API_KEY is set, tests the
             MODEL+PROMPT behavior on evals/dataset.jsonl (the real eval)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.helpers import HAS_KEY, ScriptedClient, make_waku, response, text_block, tool_block

# dataset 在模块收集阶段一次性读取。它只驱动下方 live binary case, offline case 使用显式 script。
DATASET = [
    json.loads(line)
    for line in (Path(__file__).resolve().parents[1] / "dataset.jsonl").read_text().splitlines()
    if line.strip()
]

# ---------- offline tier: our plumbing is deterministic-testable without any model


def test_create_event_writes_db_and_ics(tmp_path):
    """
    验证一次 scripted tool_use 会穿过真实 Waku loop 并产生 SQLite 与 ICS 双写。

    @param tmp_path: pytest 提供的隔离目录, 作为本 case 的 Waku runtime home 根目录。
    side effect: 在 tmp_path 创建 state.db、calendar.ics、memory 与 trace 文件。
    called by: pytest 运行 deterministic suite 时执行。
    """
    # Step 1: script 的第一项回答 Retrieval Gate, 后两项模拟 tool_use 与最终文本。
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    turn = [
        response([tool_block("create_event", {"title": "Coffee with Alex", "start": "2026-07-14T09:00"})], "tool_use"),
        response([text_block("Booked!")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient([gate] + turn))
    # Step 2: 从公开入口 respond() 进入, 避免绕过 Session、loop 或 ToolRegistry。
    result = app.respond("coffee with alex tuesday 9am")

    # Step 3: 同时断言协议观察值和真实持久化 artifact, 防止只验证 mock 调用。
    assert [c["tool"] for c in result.tool_calls] == ["create_event"]
    row = app.conn.execute("SELECT title, start FROM calendar_events").fetchone()
    assert row["title"] == "Coffee with Alex"
    assert row["start"] == "2026-07-14T09:00"
    assert "SUMMARY:Coffee with Alex" in (tmp_path / "home" / "calendar.ics").read_text()


def test_create_event_is_idempotent(tmp_path):
    """
    固定同一 title 与分钟级 start 重复调用时只创建一条事件的回归契约。

    @param tmp_path: pytest 提供的隔离目录, 用于观察数据库与 ICS 的最终状态。
    side effect: 在 tmp_path 写入一次事件, 第二个 tool_use 只返回重复说明。
    called by: pytest 运行 deterministic suite 时执行, 防止模型重试造成重复预约。
    """
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    args = {"title": "Swim with Sergey", "start": "2026-07-11T17:00"}
    script = [gate] + [
        response([tool_block("create_event", args, "tu_1"),
                  tool_block("create_event", {**args, "start": "2026-07-11T17:00:00"}, "tu_2")], "tool_use"),
        response([text_block("Booked once.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    result = app.respond("swim with sergey saturday 5pm")

    rows = app.conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]
    assert rows == 1, f"expected 1 event, got {rows}"
    assert "already exists" in result.tool_calls[1]["output"]
    ics = (tmp_path / "home" / "calendar.ics").read_text()
    assert ics.count("SUMMARY:Swim with Sergey") == 1


def test_history_records_tool_use(tmp_path):
    """
    验证 turn 收尾会把已执行 tool 写入 assistant history, 供下一轮 working memory 使用。

    @param tmp_path: pytest 提供的隔离目录, 承载完整 Waku runtime state。
    side effect: 执行一次真实 create_event 并追加当前 Session.history。
    called by: pytest 运行 deterministic suite 时执行, 防止后续 turn 忘记已经行动。
    """
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    script = [gate] + [
        response([tool_block("create_event", {"title": "X", "start": "2026-07-14T09:00"})], "tool_use"),
        response([text_block("Done.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    app.respond("book X monday 9am")
    assert "[tools used: create_event" in app.session.history[-1]["content"]


def test_no_tool_turn_ends_loop_in_one_iteration(tmp_path):
    """
    验证纯文本模型响应会在第一次 loop iteration 结束且不会产生 tool call。

    @param tmp_path: pytest 提供的隔离目录, 保证本 case 不复用其他对话状态。
    side effect: 初始化隔离 Waku 并记录一轮无 tool 的 chat、memory 与 trace。
    called by: pytest 运行 deterministic suite 时执行。
    """
    script = [
        response([text_block('{"retrieve": false, "query": "", "reason": "test"}')]),
        response([text_block("Paris.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient(script))
    result = app.respond("capital of france?")
    assert result.reply == "Paris." and result.iterations == 1 and result.tool_calls == []


def test_iteration_guardrail_stops_runaway_loop(tmp_path):
    """
    验证连续 tool_use 不会无限循环, max_iterations 会转成可观察的终止结果。

    @param tmp_path: pytest 提供的隔离目录, 承载多轮 save_note tool 副作用。
    side effect: 真实执行最多三次 tool 并写入隔离 runtime state。
    called by: pytest 运行 deterministic suite 时执行, 固定 loop guardrail 契约。
    """
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    runaway = [
        response([tool_block("save_note", {"subject": "x", "content": "y"}, f"tu_{i}")], "tool_use")
        for i in range(99)
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient([gate] + runaway), max_iterations=3)
    result = app.respond("loop forever")
    assert result.iterations == 3 and "iteration limit" in result.reply


# ---------- live tier: the actual model eval over the dataset


@pytest.mark.skipif(not HAS_KEY, reason="live eval needs ANTHROPIC_API_KEY")
@pytest.mark.parametrize("case", DATASET, ids=[c["id"] for c in DATASET])
def test_dataset_case(case, tmp_path):
    """
    让真实 provider 处理一条 dataset case, 再用 binary assertion 检查 tool 与关键参数。

    @param ① case: dataset.jsonl 解析出的输入、setup_fact 与预期 tool 契约。
           ② tmp_path: pytest 提供的隔离目录, 防止 live case 触碰用户真实 state。
    side effect: 发起真实模型请求, 并可能在 tmp_path 写 tool artifact、memory 与 trace。
    called by: pytest 在 ANTHROPIC_API_KEY 可见时为 5 条 DATASET 参数化执行。
    """
    # Step 1: 每条 live case 都创建全新 Waku, 可选 fact 也只写进该 case 的隔离 memory。
    app = make_waku(tmp_path / "home")
    if "setup_fact" in case:
        app.memory.facts.add(case["setup_fact"]["subject"], case["setup_fact"]["content"])

    # Step 2: 真实模型决定是否调用 tool, 这里不再注入 ScriptedClient。
    result = app.respond(case["input"])
    fired = [c["tool"] for c in result.tool_calls]

    # Step 3: 判定仍是精确的 binary contract, 不是由另一个 LLM 评分回答质量。
    if case["expect_tool"] is None:
        assert fired == [], f"expected no tools, model called {fired}"
    else:
        assert case["expect_tool"] in fired, f"expected {case['expect_tool']}, model called {fired}"
        args = next(c["args"] for c in result.tool_calls if c["tool"] == case["expect_tool"])
        for key, needle in case.get("expect_in_args", {}).items():
            assert needle.lower() in str(args.get(key, "")).lower(), (
                f"expected '{needle}' in args[{key}], got: {args.get(key)}"
            )
