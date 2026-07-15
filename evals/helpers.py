"""Shared eval plumbing: a scripted fake LLM client for offline tests, and a
real-Waku factory for live ones."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

# 这是模块 import 时的环境快照, 不是动态配置查询。release_gate 会先加载 .env 再启动
# pytest 子进程；直接运行 pytest 时是否已加载 .env 则取决于此前的模块 import 顺序。
HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))


def text_block(text: str):
    """
    构造 Agent loop 能识别的 Anthropic 形状 text block, 用于离线脚本响应。

    @param text: fake model 要返回给 loop 的文本内容。
    @return: 带 type=text 和 text 字段的轻量对象。
    side effect: 无。
    called by: deterministic eval 组装 Retrieval Gate JSON 或最终 assistant reply 时调用。
    """
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict, call_id: str = "tu_1"):
    """
    构造 Agent loop 能识别的 Anthropic 形状 tool_use block。

    @param ① name: ToolRegistry 中注册的 tool 名称。
           ② args: 模型声明的 tool 参数, 会原样交给 loop 执行。
           ③ call_id: tool_use 与后续 tool_result 配对使用的 id。
    @return: 带 tool_use 协议字段的轻量对象。
    side effect: 无。
    called by: test_tool_trigger.py 编排一次或多次离线 tool call 时调用。
    """
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def response(blocks, stop_reason="end_turn"):
    """
    把一组 fake content blocks 包装成 Messages API 的完整响应外壳。

    @param ① blocks: 本次 fake model response 包含的 text 或 tool_use blocks。
           ② stop_reason: 写入 llm 观察事件的协议字段, 当前执行分支仍由 content blocks 决定。
    @return: 带 content、usage 和 stop_reason 的轻量响应对象。
    side effect: 无。
    called by: deterministic eval 按真实模型调用顺序构造 ScriptedClient script 时调用。
    """
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        content=blocks,
    )


class ScriptedClient:
    """按顺序回放固定 Messages responses, 只替换远端模型而保留真实 Waku 主链路。"""

    def __init__(self, script: list):
        """
        冻结一轮 eval 需要回放的响应序列, 并暴露 messages.create 兼容入口。

        @param script: 按 Retrieval Gate、loop iteration 顺序排列的 fake responses。
        side effect: 复制 script 并创建 messages facade, 不发起网络请求。
        called by: test_tool_trigger.py 在每个 offline case 开始时创建独立 fake client。
        """
        self._script = list(script)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        """
        消费并返回下一条脚本响应, 用 pop 顺序暴露真实调用次数和先后关系。

        @param kwargs: Waku 传入的 Messages API 参数, fake 不解析但保留兼容签名。
        @return: 当前 script 队首的 fake response。
        side effect: 从内部 script 移除一项, 响应不足时抛出 IndexError, 外层调用方可能选择捕获。
        called by: Retrieval Gate 和 Agent loop 通过 client.messages.create() 调用。
        """
        return self._script.pop(0)


def make_waku(home: Path, client=None, **settings_overrides):
    """
    为 eval 构造使用隔离 runtime home 的真实 Waku, 并可注入 fake model client。

    @param ① home: tmp_path 下的隔离 runtime 目录, 承载 SQLite、ICS、trace 等副作用。
           ② client: 可选 ScriptedClient, 为空时由 Waku 按 Settings 创建真实 provider client。
           ③ settings_overrides: 传给 Settings 的其余显式覆盖项。
    @return: 已完成数据库、Memory、Session、ToolRegistry 与 Tracer 装配的 Waku 实例。
    side effect: 初始化隔离 runtime state, 但强制默认关闭真实 Apple Calendar 同步。
    called by: deterministic offline/live case 和 judge quality case 创建被测应用时调用。
    """
    from waku.app import Waku
    from waku.config import Settings

    # Step 1: 本地 runtime 副作用导向 tmp_path。无论 .env 如何设置, 都默认关闭真实 Calendar.app。
    settings_overrides.setdefault("apple_calendar", False)
    settings = Settings(home=home, **settings_overrides)
    # Step 2: 注入 client 后 Waku 会跳过 get_client()。占位 key 只保留明确的 offline Settings 标记。
    if client is not None and not settings.api_key:
        settings.api_key = "offline"
    # Step 3: 只替换 model client。Session、loop、tool 和 storage 仍由真实 Waku 装配。
    return Waku(settings=settings, client=client)
