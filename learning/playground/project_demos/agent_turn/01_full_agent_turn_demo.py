"""运行一次无需 API Key 的完整 Waku Agent Turn。

Agent Turn 是一次从用户输入到最终回复的完整生命周期。它包含工作记忆组装、
记忆检索判定、主模型推理、工具执行、结果回填、持久化和追踪。它和单次 LLM
请求不同: 一个 Turn 可以包含多次模型调用和多个真实副作用。

这个 demo 的用法是向 ``Waku`` 注入脚本化 client。client 依次返回检索判定、
日历工具请求和最终文本, 因而不访问网络; 其余代码全部使用项目真实实现。

运行命令:
    uv run python learning/playground/project_demos/agent_turn/01_full_agent_turn_demo.py

前置条件:
    从仓库根目录运行; 已执行 ``uv pip install -e '.[dev]'``; 不需要环境变量。

预期效果:
    看到 gate -> llm -> tool -> llm 的事件顺序, 两次主 Loop 迭代,
    以及写入 /tmp/waku-agent-turn-demo 的 SQLite、ICS、MEMORY.md 和 trace。
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from waku.app import Waku
from waku.config import Settings


RUNTIME_HOME = Path("/tmp/waku-agent-turn-demo")
USER_MESSAGE = "Schedule a coffee with Alex next Tuesday at 9am"


def text_block(text: str) -> SimpleNamespace:
    """构造 Anthropic Messages 形状的文本内容块。"""
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict, call_id: str = "toolu_demo") -> SimpleNamespace:
    """构造工具请求块, 让真实 Loop 像处理远程模型响应一样处理它。"""
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def response(blocks: list[SimpleNamespace], stop_reason: str = "end_turn") -> SimpleNamespace:
    """构造最小模型响应, 同时提供 Loop 和 Tracer 需要的 usage 字段。"""
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        content=blocks,
    )


class RecordingScriptedClient:
    """按顺序回放固定响应, 并保留每次模型请求供课程观察。

    输入是预先准备的响应列表。``messages.create`` 每调用一次就弹出一个响应,
    输出形状与 Anthropic client 一致。这个测试替身只替换远程推理边界,
    不替换 Waku 的装配、工具、数据库或追踪。
    """

    def __init__(self, scripted_responses: list[SimpleNamespace]) -> None:
        self._responses = list(scripted_responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs) -> SimpleNamespace:
        """冻结模型输入并返回下一条脚本响应, 脚本耗尽时明确失败。

        Loop 会原地追加 ``messages``。如果这里只保存引用, 事后看到的旧请求也会
        变成最新状态, 产生误导性的“时间穿越”。深拷贝保留调用发生时的快照。
        """
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise RuntimeError("脚本化模型响应已经耗尽, 说明实际调用次数超出课程预期")
        return self._responses.pop(0)


def make_client() -> RecordingScriptedClient:
    """准备三次模型响应: 检索门一次, 主 Loop 两次。"""
    gate_decision = response(
        [text_block('{"retrieve": true, "query": "alex", "reason": "personal schedule"}')]
    )
    create_calendar_event = response(
        [
            tool_block(
                "create_event",
                {
                    "title": "Coffee with Alex",
                    "start": "2026-07-21T09:00",
                    "end": "2026-07-21T10:00",
                    "attendees": "Alex",
                    "notes": "Catch up over coffee",
                },
            )
        ],
        stop_reason="tool_use",
    )
    final_reply = response([text_block("Booked coffee with Alex for Tuesday, July 21 at 9:00 AM.")])
    return RecordingScriptedClient([gate_decision, create_calendar_event, final_reply])


def observer(kind: str, event: dict) -> None:
    """把 Harness 旁路事件打印成紧凑 JSON, 用于映射真实执行阶段。"""
    if kind == "text":
        return
    print(f"observer | {kind:<13} | {json.dumps(event, ensure_ascii=False, default=str)}")


def show_model_calls(client: RecordingScriptedClient) -> None:
    """解释三次模型调用的输入差异, 特别展示工具结果如何回填。"""
    print("\n=== step 2: 模型请求快照 ===")
    for index, call in enumerate(client.calls, 1):
        is_gate = "tools" not in call
        phase = "retrieval gate" if is_gate else "main loop"
        roles = [message["role"] for message in call.get("messages", [])]
        tool_count = len(call.get("tools", []))
        print(f"call #{index}: phase={phase}, roles={roles}, tool_schemas={tool_count}")

    first_loop_call = client.calls[1]
    system = first_loop_call.get("system", "")
    print("system has relevant memory:", "Relevant memory:" in system)
    print("system has matching skill:", "Relevant skill instructions:" in system)


def show_persisted_state(app: Waku) -> None:
    """从真实 SQLite 和文件读取结果, 区分业务状态、记忆视图和可观测日志。"""
    print("\n=== step 3: 持久化结果 ===")
    event = dict(
        app.conn.execute(
            'SELECT title, start, "end", attendees, notes FROM calendar_events'
        ).fetchone()
    )
    chat_rows = [
        dict(row)
        for row in app.conn.execute(
            "SELECT role, content, session_id, source FROM chat_log ORDER BY id"
        ).fetchall()
    ]
    print("calendar row:", json.dumps(event, ensure_ascii=False))
    print("chat rows:", json.dumps(chat_rows, ensure_ascii=False))
    print("calendar.ics:\n" + (RUNTIME_HOME / "calendar.ics").read_text())
    print("MEMORY.md:\n" + (RUNTIME_HOME / "MEMORY.md").read_text())

    print("=== step 4: JSONL timeline ===")
    records = [json.loads(line) for line in app.tracer.path.read_text().splitlines()]
    print(" -> ".join(record["type"] for record in records))


def main() -> None:
    """编排课程的四个阶段, 并用断言锁定预期学习现象。"""
    # step1: 只清理 demo 自己的 /tmp 目录, 避免触碰用户真实 .waku 状态。
    shutil.rmtree(RUNTIME_HOME, ignore_errors=True)
    settings = Settings(
        home=RUNTIME_HOME,
        api_key="offline-demo",
        model="scripted-main",
        small_model="scripted-small",
        consolidate_every=99,
        apple_calendar=False,
        apple_tools=False,
    )
    client = make_client()
    app = Waku(settings=settings, client=client)
    app.session.session_id = "lesson-one"
    app.memory.facts.add("alex", "Alex prefers morning meetings")

    print("=== step 1: 运行真实 Waku.respond ===")
    result = app.respond(USER_MESSAGE, observer=observer, source="learning-demo")
    print("reply:", result.reply)
    print("main loop iterations:", result.iterations)
    print("tool calls:", json.dumps(result.tool_calls, ensure_ascii=False))

    show_model_calls(client)
    show_persisted_state(app)

    # step5: 这些断言不是生产测试, 它们帮助学习者确认观察没有偏离设计。
    assert len(client.calls) == 3
    assert [message["role"] for message in client.calls[1]["messages"]] == ["user"]
    assert [message["role"] for message in client.calls[2]["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert result.iterations == 2
    assert [call["tool"] for call in result.tool_calls] == ["create_event"]
    assert app.conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 2
    print("\n验证通过: 1 次 gate + 2 次主模型调用 + 1 次工具副作用 + 2 条聊天记录")

    app.close()
    app.conn.close()


if __name__ == "__main__":
    main()
