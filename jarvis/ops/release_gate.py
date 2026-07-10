"""Release gate — the diamond before "Release" on the whiteboard.

Changed the prompt? Swapped the model? Tuned retrieval top-k? Run the gate:

    python -m jarvis.ops.release_gate     (or: make gate)

Deterministic evals must pass 100% — they are unit tests; one failure blocks.
Judge evals run when a key is present and report scores. Exit code 0 = ship.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # the key check below must see .env, same as the app does

REPO = Path(__file__).resolve().parents[2]


def run(suite: str) -> int:
    print(f"\n=== {suite} ===")
    return subprocess.call(
        [sys.executable, "-m", "pytest", "-q", str(REPO / "evals" / suite)], cwd=REPO
    )


def report(deterministic: str, judge: str) -> None:
    """Persist the verdict so the dashboard can show it."""
    from datetime import datetime, timezone
    import json

    from jarvis.config import load_settings

    settings = load_settings()
    settings.ensure_home()
    (settings.home / "eval_report.json").write_text(json.dumps({
        "deterministic": deterministic,
        "judge": judge,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }))


def main() -> None:
    failed = run("deterministic")
    if failed:
        report("fail", "not run")
        print("\n⛔ GATE CLOSED — deterministic evals failed. Fix before releasing.")
        sys.exit(1)

    if os.getenv("ANTHROPIC_API_KEY"):
        failed = run("judge")
        if failed:
            report("pass", "fail")
            print("\n⛔ GATE CLOSED — judge scores below threshold.")
            sys.exit(1)
        report("pass", "pass")
    else:
        report("pass", "skipped")
        print("\n(judge suite skipped — no ANTHROPIC_API_KEY)")

    print("\n✅ GATE OPEN — safe to release.")


if __name__ == "__main__":
    main()
