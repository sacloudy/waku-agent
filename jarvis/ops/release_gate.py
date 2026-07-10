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


def main() -> None:
    failed = run("deterministic")
    if failed:
        print("\n⛔ GATE CLOSED — deterministic evals failed. Fix before releasing.")
        sys.exit(1)

    if os.getenv("ANTHROPIC_API_KEY"):
        failed = run("judge")
        if failed:
            print("\n⛔ GATE CLOSED — judge scores below threshold.")
            sys.exit(1)
    else:
        print("\n(judge suite skipped — no ANTHROPIC_API_KEY)")

    print("\n✅ GATE OPEN — safe to release.")


if __name__ == "__main__":
    main()
