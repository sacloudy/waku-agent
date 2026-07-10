"""Wiring — builds one Jarvis from its parts. Gateways call `respond()`.

This file is the assembly diagram in code: config → db → tools → memory →
session → loop. If you want to understand the repo in one place, start here.
"""

from __future__ import annotations

from jarvis.config import Settings, load_settings
from jarvis.db import connect
from jarvis.loop.agent import LoopResult, Observer, run_loop
from jarvis.loop.models import get_client
from jarvis.ops.tracing import Tracer, compose
from jarvis.runtime.session import Session
from jarvis.tools import build_registry


class Jarvis:
    def __init__(self, settings: Settings | None = None, client=None):
        # `client` is injectable so evals can swap in a scripted fake model —
        # the same seam a production system uses for staging vs prod models.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = connect(self.settings.home)
        self.client = client or get_client(self.settings)
        self.tools = build_registry(self.conn, self.settings)

        # Memory is deliberately unpluggable (memory=None): without it Jarvis
        # still works, it just forgets — the "before" state the video contrasts.
        from jarvis.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.client)
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)

    def respond(self, user_message: str, observer: Observer | None = None) -> LoopResult:
        """One full turn: assemble working memory → run the loop → persist.
        Everything that happens is both shown (observer) and recorded (tracer)."""
        notify = compose(observer, self.tracer.event)

        with self.tracer.turn(user_message):
            system = self.session.build_system(user_message, notify=notify)
            messages = list(self.session.history) + [{"role": "user", "content": user_message}]

            result = run_loop(
                client=self.client,
                model=self.settings.model,
                system=system,
                messages=messages,
                tools=self.tools,
                max_iterations=self.settings.max_iterations,
                max_tokens=self.settings.max_tokens,
                observer=notify,
            )

            self.session.add_exchange(user_message, result.reply, tool_calls=result.tool_calls)
            if self.memory is not None:
                self.memory.maybe_consolidate(notify=notify)

        self.tracer.end_turn(result.reply, result.iterations)
        return result
