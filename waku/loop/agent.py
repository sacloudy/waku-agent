"""THE LOOP — observe → reason → act → repeat. This file is the whole trick.

Every agent framework is ultimately this while-loop with more indirection:

    while not done:
        response = llm(messages, tools)          # reason
        if response asks for tools:
            results = run(tool_calls)            # act
            messages += results                  # observe
        else:
            done                                 # reply to the human

End-loop guardrails (the orange box's exit conditions):
  1. the model stops asking for tools  → natural end of turn
  2. max_iterations reached            → hard stop, never spin forever
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from waku.tools.registry import ToolRegistry

# Observers let the gateway show tool calls live and let ops/tracing record
# them — without either being wired into the loop's logic.
LoopEvent = dict[str, Any]
Observer = Callable[[str, LoopEvent], None]


@dataclass
class LoopResult:
    reply: str
    tool_calls: list[LoopEvent] = field(default_factory=list)
    iterations: int = 0


def run_loop(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    tools: ToolRegistry,
    max_iterations: int = 10,
    max_tokens: int = 2048,
    observer: Observer | None = None,
    stream: bool = False,
) -> LoopResult:
    """
    执行一个显式 reason-act-observe Agent Loop, 直到模型自然回复或达到迭代上限。

    @param ① client: 提供 Anthropic Messages 形状的模型 client, 也可以是兼容 adapter 或脚本化 fake。
           ② model: 本轮主模型 id。
           ③ system: Session 已组装完成的 system prompt。
           ④ messages: 当前工作消息列表, Loop 会原地追加 assistant 内容和 tool_result。
           ⑤ tools: ToolRegistry, 同时提供模型可见 schema 和安全执行边界。
           ⑥ max_iterations: 最大 reason-act 循环次数, 防止模型持续请求 tool。
           ⑦ max_tokens: 每次模型调用允许生成的最大 token 数。
           ⑧ observer: 可选事件观察者, 接收 text、llm 和 tool 事件。
           ⑨ stream: 是否优先使用 client.messages.stream() 产生文本增量。
    @return: LoopResult, 包含最终回复、已执行 tool event 和实际迭代次数。
    side effect: 原地修改 messages, 可能调用远程模型和执行有副作用的 tool, 并向 observer 发送事件。
    called by: Waku.respond() 执行生产 turn, iteration guardrail 教学 demo 也会直接调用。
    """
    # Step 1: 先归一化 observer 和结果容器, 并只在调用方请求且 client 支持时启用流式路径。
    notify = observer or (lambda kind, ev: None)
    result = LoopResult(reply="")
    can_stream = stream and hasattr(client.messages, "stream")

    # Step 2: 每轮只做一次 reason 决策, tool_result 会在本轮末尾加入 messages 后驱动下一轮。
    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration

        # Step 3: 流式模式先尝试发送 text delta, 但最终仍要取得完整 response 才能判断 tool_use。
        response = None
        if can_stream:
            try:
                with client.messages.stream(
                    model=model, system=system, messages=messages,
                    tools=tools.schemas(), max_tokens=max_tokens,
                ) as s:
                    for delta in s.text_stream:
                        notify("text", {"delta": delta})
                    response = s.get_final_message()
            except Exception:
                # 流式异常只降级本次 reason, 不让 UI 能力影响 Agent turn 的可完成性。
                response = None  # any streaming hiccup → fall back to one call

        # Step 4: client 不支持 stream 或流式请求失败时走普通调用, 两条路径产出相同 response 协议。
        if response is None:
            response = client.messages.create(
                model=model,
                system=system,
                messages=messages,
                tools=tools.schemas(),
                max_tokens=max_tokens,
            )
        notify("llm", {"iteration": iteration, "stop_reason": response.stop_reason,
                       "usage": {"in": response.usage.input_tokens, "out": response.usage.output_tokens}})

        # Step 5: 先保存完整 assistant content, text 和 tool_use 必须以同一轮原始顺序进入工作记忆。
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # Step 6: 没有 tool_use 表示模型已转向用户回复, 汇总 text block 后自然结束本轮。
        if not tool_uses:
            result.reply = "".join(b.text for b in response.content if b.type == "text")
            return result

        # Step 7: 逐个执行模型请求并记录相同 event, UI、trace 和最终 LoopResult 因而看到同一份事实。
        tool_results = []
        for call in tool_uses:
            output = tools.execute(call.name, call.input)
            event = {"tool": call.name, "args": call.input, "output": output}
            result.tool_calls.append(event)
            notify("tool", event)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": output}
            )

        # Step 8: tool_result 以 user role 回填协议消息, 下一轮 reason 才能基于真实执行结果继续回答。
        messages.append({"role": "user", "content": tool_results})

    # Step 9: 达到上限说明模型一直请求 tool, 返回明确的硬停止语义而不是让进程无限循环。
    result.reply = "(I hit my iteration limit before finishing — try breaking the request into smaller steps.)"
    return result
