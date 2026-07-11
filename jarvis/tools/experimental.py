"""Roadmap tools — the whiteboard boxes not wired into the loop yet.

These are SKELETONS on purpose. Each shows the *shape* of a capability from the
architecture chart and returns an honest "coming soon", so the diagram maps to
something real without pretending the feature is done. They are OFF by default;
set `JARVIS_EXPERIMENTAL=1` to register them (they'll just report their status).

Why skeletons, not full builds? Each one crosses the repo's "one flagship task,
readable in an afternoon" line — sub-agents add multi-agent coordination;
terminal/browser tools add a sandbox and a real safety surface. They're the
sequel. The dashboard shows them under "Coming soon" so expectations are set,
not over-promised.
"""

from __future__ import annotations

from jarvis.tools.registry import Tool

# name → what it will do, and which box on the whiteboard it maps to.
PLANNED = [
    {"name": "delegate_task", "box": "Sub-Agents",
     "description": "Spawn a fresh agent run for a self-contained subtask and return its "
                    "result — the whiteboard's Sub-Agent boxes. Left out to keep the core "
                    "single-agent and readable."},
    {"name": "run_command", "box": "Terminal tool",
     "description": "Run a shell command in a sandbox and read the output — Hermes's 'Terminal' "
                    "tool. Needs a real sandbox + safety surface first."},
    {"name": "browse_web", "box": "Browser tool",
     "description": "Open a page and read/click it — Hermes's 'Browser' tool. (search_web already "
                    "covers read-only web lookups.)"},
    {"name": "schedule_task", "box": "Cron Job",
     "description": "Let the agent schedule its own recurring runs. Today `make brief` + a system "
                    "cron line already does scheduled runs; this would move it in-app."},
]


def _stub(name: str, description: str, box: str) -> Tool:
    def fn(**kwargs) -> str:
        return (f"'{name}' maps to the '{box}' box on the architecture chart and isn't wired "
                f"in yet — it's on the roadmap (coming soon). Tell the user honestly.")

    return Tool(name=name, description=f"[coming soon] {description}",
                input_schema={"type": "object", "properties": {}}, fn=fn)


def make_tools() -> list[Tool]:
    """The stub tools, registered only when JARVIS_EXPERIMENTAL=1."""
    return [_stub(p["name"], p["description"], p["box"]) for p in PLANNED]
