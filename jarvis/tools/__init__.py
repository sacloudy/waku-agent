"""The agent's tools. Flagship-task tools (calendar/notes/messages), memory
self-management (manage_memory/update_soul/create_skill), and opt-in adapters:
Apple ecosystem (JARVIS_APPLE_TOOLS=1) and MCP servers (.jarvis/mcp.json)."""

from __future__ import annotations

import os
import sqlite3

from jarvis.config import Settings
from jarvis.tools import calendar, memory_admin, messages, notes, search
from jarvis.tools.registry import ToolRegistry


def build_registry(conn: sqlite3.Connection, settings: Settings, memory=None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(calendar.make_tool(conn, settings.home, apple_calendar=settings.apple_calendar))
    registry.register(calendar.make_list_tool(conn))   # read side: "what's on my calendar?"
    registry.register(notes.make_tool(conn))
    registry.register(messages.make_tool(settings.home))
    # Web search — pairs with create_event for the multi-tool loop demo
    # ("find the World Cup games left and add them to my calendar").
    registry.register(search.make_tool())

    # Memory self-management — the agent can correct/forget memory, learn rules,
    # and author its own skills (feels like a personal agent, not a black box).
    if memory is not None:
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))

    # Roadmap/skeleton tools (sub-agents, terminal, browser, cron) — off by
    # default; opt in with JARVIS_EXPERIMENTAL=1. They report "coming soon".
    if os.getenv("JARVIS_EXPERIMENTAL", "") in ("1", "true", "yes"):
        from jarvis.tools import experimental

        for t in experimental.make_tools():
            registry.register(t)

    # Apple ecosystem readers/writers (opt-in; first use triggers macOS prompts).
    if settings.apple_tools:
        from jarvis.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # MCP servers (opt-in via .jarvis/mcp.json).
    mcp_config = settings.home / "mcp.json"
    if mcp_config.exists():
        try:
            from jarvis.tools.mcp_client import MCPBridge

            bridge = MCPBridge(mcp_config)
            for t in bridge.start():
                registry.register(t)
            registry.mcp_bridge = bridge  # so Jarvis.close() can stop the servers
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'launch-jarvis[mcp]'")

    return registry
