"""Wiring — builds one Waku from its parts. Gateways call `respond()`.

This file is the assembly diagram in code: config → db → tools → memory →
session → loop. If you want to understand the repo in one place, start here.
"""

from __future__ import annotations

from waku.config import Settings, load_settings
from waku.db import connect
from waku.loop.agent import LoopResult, Observer, run_loop
from waku.loop.models import get_client
from waku.ops.tracing import Tracer, compose
from waku.runtime.session import Session
from waku.tools import build_registry


class Waku:
    def __init__(self, settings: Settings | None = None, client=None, conn=None):
        """
        装配一个可执行的 Waku 实例, 这是所有 gateway 进入 Agent runtime 前的进程级依赖入口。

        @param ① settings: 可选运行配置, 缺省时从环境变量加载。
               ② client: 可注入的模型 client, deterministic eval 会传入脚本化实现。
               ③ conn: 可注入的 SQLite connection, Dashboard 会传入可跨线程使用的连接。
        side effect: 创建本地状态目录和数据库连接, 初始化模型 client, 并可能启动配置中的 MCP subprocess。
        called by: CLI/Voice/Telegram gateway 启动时创建实例, Dashboard 与 eval 通过同一入口注入替代依赖。
        """
        # Step 1: 先确定配置、状态目录和两项可注入依赖, 让真实 gateway 与离线 eval 共享同一条装配路径。
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = conn or connect(self.settings.home)
        self.client = client or get_client(self.settings)

        # Step 2: Memory 必须先于 ToolRegistry 创建, 因为 manage_memory 等 tool 需要复用同一个 Memory facade。
        from waku.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.client)
        self.tools = build_registry(self.conn, self.settings, self.memory)
        self.mcp_bridge = getattr(self.tools, "mcp_bridge", None)

        # Step 3: 最后装配每轮会复用的 Session 与 Tracer, 将工作记忆和可观测性接到同一实例上。
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)

    def close(self) -> None:
        """
        释放 Waku 持有的外部资源, 当前主要负责关闭 ToolRegistry 启动的 MCP subprocess。

        side effect: 如果存在 MCP bridge, 会终止其管理的 server 进程和连接。
        called by: Dashboard 切换 provider 后销毁旧实例, 教学 demo 结束时也会显式调用。
        """
        # MCP 是当前装配中唯一需要显式关闭的长生命周期资源, 未配置时保持空操作。
        if self.mcp_bridge is not None:
            self.mcp_bridge.close()

    def respond(self, user_message: str, observer: Observer | None = None,
                source: str = "cli", stream: bool = False) -> LoopResult:
        """
        执行一个完整 Agent turn, 串起工作记忆、Loop、会话持久化和 trace 收尾。

        @param ① user_message: gateway 传入的本轮用户文本。
               ② observer: 可选事件观察者, 用于把 gate、LLM、tool 和流式文本交给 UI。
               ③ source: 输入来源标签, 会随 chat_log 持久化以区分 cli、voice、telegram 或 dashboard。
               ④ stream: 是否请求模型以流式方式产生文本增量。
        @return: LoopResult, 包含最终回复、实际执行过的 tool call 和 Loop 迭代次数。
        side effect: 可能执行 tool, 写入 chat_log、MEMORY.md、trace 与 usage ledger, 并触发记忆 consolidation。
        called by: CLI、Voice、Telegram、Dashboard 和 brief gateway 在收到一条用户输入后调用。
        """
        # Step 1: 合并 gateway observer 与 Tracer, 保证同一事件既能实时展示又能落入可观测记录。
        notify = compose(observer, self.tracer.event)

        with self.tracer.turn(user_message):
            # Step 2: 从持久记忆与当前 Session 构造本轮 system prompt 和消息快照, 不直接修改历史列表。
            system = self.session.build_system(user_message, notify=notify)
            messages = list(self.session.history) + [{"role": "user", "content": user_message}]

            # Step 3: 把规范化后的上下文交给唯一 Agent Loop, tool 执行和退出条件都在该边界内完成。
            result = run_loop(
                client=self.client,
                model=self.settings.model,
                system=system,
                messages=messages,
                tools=self.tools,
                max_iterations=self.settings.max_iterations,
                max_tokens=self.settings.max_tokens,
                observer=notify,
                stream=stream,
            )

            # Step 4: 只有 Loop 得到最终结果后才写回 Session, 并把本轮 tool 事实纳入后续对话历史。
            self.session.add_exchange(user_message, result.reply, tool_calls=result.tool_calls,
                                      source=source)
            if self.memory is not None:
                # consolidation 失败不会丢失原始 chat_log, export_markdown 只是 state.db 的可读镜像。
                self.memory.maybe_consolidate(notify=notify)
                self.memory.export_markdown()   # keep MEMORY.md in sync

        # Step 5: root span 退出后补写 turn_end 并刷新可选 OTel provider, 再把结果交回 gateway。
        self.tracer.end_turn(result.reply, result.iterations)
        return result
