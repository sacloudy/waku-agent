"""演示 Agent Loop 的最大迭代次数保护。

``max_iterations`` 是硬退出条件。它不负责判断任务是否正确完成, 只保证模型持续
请求工具时循环不会无限运行。与自然退出不同: 自然退出由模型返回无工具响应触发,
硬退出由 Harness 在达到固定次数后触发。

这个 demo 的用法是注入一个永远请求 ``demo_ping`` 的模型, 再把迭代上限设为 3。
Loop 会真实执行工具三次, 然后返回明确的 iteration limit 文本。

运行命令:
    .venv/bin/python learning/playground/project_demos/agent_turn/02_iteration_guardrail_demo.py

前置条件:
    从仓库根目录运行; 已安装项目; 不需要文件、网络或环境变量。

预期效果:
    看到 3 次 llm 和 3 次 tool 事件, 最终回复说明达到迭代上限。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from waku.loop.agent import run_loop
from waku.tools.registry import Tool, ToolRegistry


class NeverStopClient:
    """每次都返回同一个工具请求, 用确定方式模拟失控模型。"""

    def __init__(self) -> None:
        self.call_count = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs) -> SimpleNamespace:
        """忽略输入并持续请求工具, 让 Harness 而不是模型决定何时停止。"""
        self.call_count += 1
        block = SimpleNamespace(
            type="tool_use",
            id=f"toolu_{self.call_count}",
            name="demo_ping",
            input={"iteration": self.call_count},
        )
        return SimpleNamespace(
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            content=[block],
        )


def observer(kind: str, event: dict) -> None:
    """输出每轮可见事件, 让调用次数和工具次数可以直接核对。"""
    print(f"{kind:<5} | {json.dumps(event, ensure_ascii=False, default=str)}")


def main() -> None:
    """注册无副作用工具并运行一个最多三轮的真实 Agent Loop。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="demo_ping",
            description="Return the requested iteration number.",
            input_schema={
                "type": "object",
                "properties": {"iteration": {"type": "integer"}},
                "required": ["iteration"],
            },
            fn=lambda iteration: f"pong from iteration {iteration}",
        )
    )
    client = NeverStopClient()

    result = run_loop(
        client=client,
        model="scripted-never-stop",
        system="This model intentionally never stops for a guardrail lesson.",
        messages=[{"role": "user", "content": "keep calling the tool"}],
        tools=registry,
        max_iterations=3,
        max_tokens=100,
        observer=observer,
    )

    print("\nresult:")
    print("iterations:", result.iterations)
    print("model calls:", client.call_count)
    print("tool calls:", len(result.tool_calls))
    print("reply:", result.reply)

    assert result.iterations == 3
    assert client.call_count == 3
    assert len(result.tool_calls) == 3
    assert "iteration limit" in result.reply
    print("验证通过: Harness 在第 3 轮后硬停止, 没有进入无限循环")


if __name__ == "__main__":
    main()
