"""CLI gateway — the zero-setup way to talk to your Waku.

The Gateway Interface box: a gateway only moves text in and out; everything
interesting happens in the loop. The Telegram gateway is the same ~60 lines
with polling instead of input().
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from jarvis.app import Jarvis

console = Console()


def _observer(kind: str, event: dict) -> None:
    """Show the loop's internals live — the video's 'transparent harness' beat."""
    if kind == "tool":
        console.print(f"  [dim]tool · {event['tool']}({event['args']}) → {event['output'][:80]}[/dim]")
    elif kind == "gate":
        console.print(f"  [dim]gate · {event['decision']} — {event.get('reason','')}[/dim]")
    elif kind == "consolidation":
        console.print(f"  [dim]memory · consolidated {event['new_facts']} fact(s) from recent chats[/dim]")


def main() -> None:
    jarvis = Jarvis()
    console.print(Panel.fit(
        "[bold]Waku[/bold] — local, yours, transparent.\n"
        f"home: {jarvis.settings.home.resolve()}   model: {jarvis.settings.model}\n"
        "Ctrl-D or /quit to exit.",
        border_style="cyan",
    ))
    while True:
        try:
            user_message = console.input("[bold cyan]you ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_message:
            continue
        if user_message in ("/quit", "/exit"):
            break
        result = jarvis.respond(user_message, observer=_observer, source="cli")
        console.print(f"[bold green]waku ›[/bold green] {result.reply}\n")
    console.print("[dim]bye — your memory stays in state.db[/dim]")


if __name__ == "__main__":
    main()
