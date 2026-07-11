# Demo / filming checklist

The workflow to walk through on camera, in order. Each beat has the exact prompt, what it
proves, where to look, and whether it's been dry-run verified. Keep this updated as we test.

## Pre-flight

- [x] Provider = `anthropic` (best streaming; Gemini breaks multi-turn tool use) — Settings
- [x] Free `TAVILY_API_KEY` pasted on the Settings page (for the World Cup beat)
- [x] Clean curated state — `python scripts/demo_seed.py` (keeps traces + `usage.jsonl` spend)
- [x] `waku dashboard` running on your own machine (also starts Telegram if a token is set) → `localhost:7777` in a real browser

## The beats

| # | Beat (pillar) | Say this | Watch | Verified |
|---|---|---|---|---|
| 1 | Cockpit tour (Harness) | — (click around) | Overview: stats, gate bar, clickable diagram | [x] |
| 2 | Gateways (Harness) | chat from `make run` **and** the browser | Gateway tab tags each `cli` / `dashboard` | [x] |
| 3 | The Loop + streaming | *"Schedule a tennis game with Raj this Saturday at 8am"* | reply streams; LOOP box pulses; Loop tab `iter 2` | [x] |
| 4 | Calendar read | *"What's on my calendar today?"* | `list_events` fires; answers from `state.db` | [x] |
| 5 | Retrieval gate (Memory) | *"When am I swimming with Sergey?"* then *"what's 12 × 8?"* | gate retrieve vs skip; Overview bar; Ops decisions | [x] |
| 6 | Memory self-management | *"Remember Raj prefers morning tennis"* | `save_note`; Memory ▸ Semantic + `MEMORY.md` update | [x] |
| 7 | **Multi-tool loop (money shot)** | *"Search the World Cup games still left and add each to my calendar"* | Loop tab `iter 8`: `search_web` × N → `create_event` × N | [x] |
| 8 | Consolidation (Memory) | keep chatting past N exchanges | Memory ▸ Consolidation; a new episode + distilled facts | [x] |
| 9 | Telegram gateway | message the bot from your phone | Gateway tab shows it tagged `telegram` | [x] |
| 10 | **Voice** | click the mic in the dock, speak | local Whisper transcribes into the input | [ ] |
| 11 | Eval / LLM-Ops (hero 2) | `make gate` in a terminal | Ops: release gate + eval history grows; traces inline | [ ] |
| 12 | Spend ledger | (just look) | Ops: all-time cost/tokens, per-day — survives resets | [x] |

## Still to verify

- [ ] **Voice** (beat 10) — mic → `/api/voice` → local Whisper. Needs mic permission in the browser.
- [ ] **`make gate` on camera** (beat 11) — show the eval history row appear on the Ops tab.

## Where the git history lives

Every feature above is a commit on `main` with a WHY-focused message — `git log --oneline` is the
changelog. Key ones: streaming, `search_web` (World Cup loop), `list_events` (calendar read),
permanent spend ledger + `MEMORY.md`, source-tagged Gateway, coming-soon skeletons.

## Reset between takes

`python scripts/demo_seed.py` — clears the memory/calendar for a clean take but **keeps** traces and
`usage.jsonl` (your real spend) and backs up the whole `.jarvis` first. It never deletes the db file,
so a running `make dashboard`/`make telegram` keeps working. Nothing else clears your data.
