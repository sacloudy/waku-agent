"""集中装配 Agent 可见的 tool, 包括核心能力、memory 管理和可选外部 adapter。"""

from __future__ import annotations

import os
import sqlite3

from waku.config import Settings
from waku.tools import calendar, memory_admin, messages, notes, search
from waku.tools.registry import ToolRegistry


def build_registry(conn: sqlite3.Connection, settings: Settings, memory=None) -> ToolRegistry:
    """
    按当前 Settings 装配一次 ToolRegistry, 作为 Waku 到所有 tool 的统一边界。

    @param ① conn: Waku 持有的 SQLite 连接, 供日历、笔记和 memory tool 共享状态。
           ② settings: 启动配置, 决定 home、Apple tool 和 MCP 等可选能力。
           ③ memory: 可选 Memory facade, 存在时才注册自管理 tool。
    @return: 已注册核心和启用项的 ToolRegistry, 交给 Agent loop 导出 schema 并执行调用。
    side effect: 可能读取环境变量、启动 MCP subprocess, 并在可选依赖缺失时打印提示。
    called by: Waku.__init__() 在进程装配阶段调用。
    """
    # Step 1: 核心 tool 始终注册, 它们构成 scheduling 主任务和本地搜索/记录能力。
    registry = ToolRegistry()
    registry.register(calendar.make_tool(conn, settings.home, apple_calendar=settings.apple_calendar))
    registry.register(calendar.make_list_tool(conn))   # read side: "what's on my calendar?"
    registry.register(notes.make_tool(conn))
    registry.register(messages.make_tool(settings.home))
    # search_web 与 create_event 可以在同一轮多次循环, 但 registry 只负责提供能力, 不编排顺序。
    registry.register(search.make_tool())

    # Step 2: Memory 是可选注入依赖。没有它时不能注册闭包型自管理 tool,
    # 这样独立构造 registry 的调用方不会得到运行时必然失败的 schema。
    if memory is not None:
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))

    # Step 3: 实验 tool 只在显式 opt-in 时出现, 避免默认暴露尚未实现的 roadmap 能力。
    if os.getenv("WAKU_EXPERIMENTAL", "") in ("1", "true", "yes"):
        from waku.tools import experimental

        for t in experimental.make_tools():
            registry.register(t)

    # Step 4: Apple tool 独立受 Settings 控制, 因为首次调用可能触发真实系统权限提示。
    if settings.apple_tools:
        from waku.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # Step 5: mcp.json 把装配推进到外部进程边界。
    # start() 返回的远端 tool 与本地 tool 使用同一 registry。
    mcp_config = settings.home / "mcp.json"
    if mcp_config.exists():
        try:
            from waku.tools.mcp_client import MCPBridge

            bridge = MCPBridge(mcp_config)
            for t in bridge.start():
                registry.register(t)
            # bridge 动态挂在 registry 上, Waku.__init__() 会保存它并由 Waku.close() 统一停止 subprocess。
            registry.mcp_bridge = bridge
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'waku-agent[mcp]'")

    return registry
