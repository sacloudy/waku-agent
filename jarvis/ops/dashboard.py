"""Dashboard — every pillar on one local page. Zero new dependencies.

    make dashboard        # → http://localhost:7777

One stdlib HTTP server reading the files Jarvis already writes:
  loop + harness   traces/*.jsonl   (turns, gate decisions, tool calls, tokens)
  memory           state.db         (facts, episodes, chat log, consolidation)
  tools            state.db + calendar.ics + outbox/
  eval             eval_report.json (written by `make gate`)

The overview mirrors the architecture diagram — every box is clickable and
opens that section's live data. The Chat tab is a real gateway: type a message
and watch the same harness (gate, loop, tools, memory) that the CLI/voice/
telegram gateways drive — the pipeline lights up in the browser as it runs.
Bound to 127.0.0.1 only. For deep trace waterfalls use Phoenix (`make trace`).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jarvis.config import load_settings
from jarvis.db import connect

PORT = 7777

# One shared agent for the browser gateway. Built lazily (first chat), reused
# across the threaded server's workers via a cross-thread connection + a lock
# so chats run one at a time — correct for a single-user local tool.
_agent = None
_agent_lock = threading.Lock()


def _get_agent():
    global _agent
    if _agent is None:
        from jarvis.app import Jarvis

        settings = load_settings()
        settings.ensure_home()
        conn = connect(settings.home, check_same_thread=False)
        _agent = Jarvis(settings=settings, conn=conn)
    return _agent


def chat(message: str) -> dict:
    """Run one real turn through the harness and return the structured result —
    gate decision, tool calls, reply, latency — so the browser can render the
    pipeline as it happened. Writes traces + memory like any other gateway."""
    events: list[dict] = []
    with _agent_lock:
        agent = _get_agent()
        start = datetime.now(timezone.utc)
        result = agent.respond(message, observer=lambda kind, ev: events.append({"kind": kind, **ev}))
        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    gate = next((e for e in events if e["kind"] == "gate"), None)
    cons = next((e for e in events if e["kind"] == "consolidation"), None)
    return {
        "reply": result.reply,
        "gate": {"decision": gate["decision"], "reason": gate.get("reason")} if gate else None,
        "tools": [
            {"tool": c["tool"], "args": c["args"], "output": c["output"],
             "status": _tool_status(c["output"]), "summary": (c["output"] or "").split(". ")[0][:120]}
            for c in result.tool_calls
        ],
        "consolidation": {"new_facts": cons["new_facts"]} if cons else None,
        "iterations": result.iterations,
        "latency_ms": latency_ms,
    }

# Rough $/million tokens (in, out) for a dollar ESTIMATE — the number humans
# actually feel. Keyed by provider; deliberately approximate and labelled "est".
PRICING = {
    "anthropic": (3.0, 15.0), "openai": (2.5, 15.0), "gemini": (0.3, 2.5),
    "kimi": (0.6, 2.5), "glm": (0.6, 2.2),
}


def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _tool_status(output: str) -> str:
    """Classify a tool result for the UI: ok / warn / error — from the output
    string alone (tools already report honestly, so trust their words)."""
    low = (output or "").lower()
    if "failed" in low or "timed out" in low or low.startswith("error"):
        return "error"
    if "already exists" in low or "not synced" in low or "skipped" in low:
        return "warn"
    return "ok"


def collect() -> dict:
    """Everything the page shows, in one JSON blob."""
    settings = load_settings()
    settings.ensure_home()
    home = settings.home
    conn = connect(home)

    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    # --- traces → turns (group events between turn_start and turn_end)
    events = []
    trace_files = sorted((home / "traces").glob("*.jsonl"))
    for path in trace_files:
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    turns, current, wake_scans = [], None, []
    for ev in events:
        kind = ev.get("type")
        if kind == "turn_start":
            current = {"user_message": ev.get("user_message"), "ts": ev.get("ts"),
                       "gate": None, "llm_calls": [], "tools": [], "reply": None}
        elif kind == "wake_scan":
            wake_scans.append(ev)
        elif current is not None:
            if kind == "gate":
                current["gate"] = ev
            elif kind == "llm":
                current["llm_calls"].append(ev)
            elif kind == "tool":
                current["tools"].append(ev)
            elif kind == "consolidation":
                current["consolidation"] = ev
            elif kind == "turn_end":
                current["reply"] = ev.get("reply")
                current["iterations"] = ev.get("iterations")
                turns.append(current)
                current = None
    if current is not None:  # a turn that never ended = the smoking gun for hangs
        current["reply"] = "TURN NEVER FINISHED — check for a hang after this point"
        current["unfinished"] = True
        turns.append(current)

    # --- derive per-turn latency + dollar cost (the ops numbers humans feel)
    price_in, price_out = PRICING.get(settings.provider, (3.0, 15.0))
    for t in turns:
        start, end = _parse_ts(t["ts"]), None
        last = t["llm_calls"][-1]["ts"] if t["llm_calls"] else None
        end = _parse_ts(last)
        t["latency_ms"] = int((end - start).total_seconds() * 1000) if start and end else None
        tin = sum(c.get("usage", {}).get("in", 0) for c in t["llm_calls"])
        tout = sum(c.get("usage", {}).get("out", 0) for c in t["llm_calls"])
        t["cost"] = tin / 1e6 * price_in + tout / 1e6 * price_out
        for x in t["tools"]:
            x["status"] = _tool_status(x.get("output", ""))
            x["summary"] = (x.get("output", "") or "").split(". ")[0][:120]

    latencies = sorted(t["latency_ms"] for t in turns if t["latency_ms"] is not None)
    total_cost = sum(t["cost"] for t in turns)

    def pct(p: float) -> int:
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))] if latencies else 0

    from jarvis.memory.procedural.loader import SkillLoader
    from jarvis.memory import REPO_SKILLS

    skills = [{"name": s.name, "description": s.description, "path": str(s.path)}
              for s in SkillLoader([REPO_SKILLS, home / "skills"]).skills]

    eval_report = None
    report_path = home / "eval_report.json"
    if report_path.exists():
        eval_report = json.loads(report_path.read_text())

    outbox = [{"name": p.name, "text": p.read_text()[:400]}
              for p in sorted((home / "outbox").glob("*.txt"), reverse=True)[:20]]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": str(home.resolve()),
        "provider": settings.provider,
        "model": settings.model or "(provider default)",
        "stats": {
            "turns": len(turns),
            "tool_calls": sum(len(t["tools"]) for t in turns),
            "tool_errors": sum(1 for t in turns for x in t["tools"] if x["status"] == "error"),
            "gate_skips": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "skip"),
            "gate_retrieves": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "retrieve"),
            "tokens_in": sum(c.get("usage", {}).get("in", 0) for t in turns for c in t["llm_calls"]),
            "tokens_out": sum(c.get("usage", {}).get("out", 0) for t in turns for c in t["llm_calls"]),
            "cost": round(total_cost, 4),
            "latency_avg": int(sum(latencies) / len(latencies)) if latencies else 0,
            "latency_p95": pct(0.95),
            "trace_files": len(trace_files),
        },
        "turns": turns[::-1][:50],
        "wake_scans": wake_scans[::-1][:25],
        "facts": rows("SELECT subject, content, source, created_at FROM facts ORDER BY id DESC"),
        "episodes": rows("SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC"),
        "chat_pending": conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated=0").fetchone()[0],
        "chat_log": rows("SELECT role, content, consolidated, created_at FROM chat_log ORDER BY id DESC LIMIT 60")[::-1],
        "consolidate_every": settings.consolidate_every,
        "calendar": rows('SELECT title, start, "end", attendees, created_at FROM calendar_events ORDER BY start'),
        "outbox": outbox,
        "skills": skills,
        "eval_report": eval_report,
    }


def events_since(cursor):
    """New trace events past `cursor` (a line count in today's trace file).
    Any gateway — browser, CLI, voice, Telegram — appends to this same file,
    so the live diagram lights up for all of them. cursor=None returns just
    the current tail so the browser starts fresh instead of replaying history."""
    settings = load_settings()
    settings.ensure_home()
    path = settings.home / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    if not path.exists():
        return {"events": [], "cursor": 0}
    lines = path.read_text().splitlines()
    if cursor is None or cursor < 0 or cursor > len(lines):
        return {"events": [], "cursor": len(lines)}
    out = []
    for ln in lines[cursor:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return {"events": out, "cursor": len(lines)}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis</title>
<style>
  :root{
    --bg:#fafaf9;--panel:#ffffff;--line:#e7e6e4;--line2:#d9d8d5;
    --ink:#21201d;--ink2:#6f6e69;--ink3:#a3a29d;
    --accent:#5e6ad2;--accent-soft:#eef0fb;
    --good:#1f7a4d;--good-soft:#e8f4ee;--bad:#c0392b;--bad-soft:#faeceb;
    --mono:ui-monospace,'SF Mono',Menlo,monospace;
  }
  @media (prefers-color-scheme:dark){:root{
    --bg:#101012;--panel:#18181b;--line:#26262a;--line2:#333338;
    --ink:#ececea;--ink2:#96959f;--ink3:#5f5e66;
    --accent:#7c8aec;--accent-soft:#20223a;
    --good:#4cc38a;--good-soft:#12291d;--bad:#e5655a;--bad-soft:#331714;
  }}
  *{box-sizing:border-box;margin:0}
  body{background:var(--bg);color:var(--ink);
       font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       display:flex;min-height:100vh}
  nav{width:208px;flex-shrink:0;border-right:1px solid var(--line);padding:20px 12px;
      position:sticky;top:0;height:100vh}
  .brand{font-weight:650;font-size:15px;padding:0 10px 4px}
  .brand small{display:block;color:var(--ink3);font-weight:400;font-size:11px;margin-top:2px}
  nav a{display:flex;justify-content:space-between;align-items:center;color:var(--ink2);
        text-decoration:none;padding:6px 10px;border-radius:6px;font-size:13.5px;margin-top:2px}
  nav a:hover{background:var(--panel);color:var(--ink)}
  nav a.on{background:var(--accent-soft);color:var(--accent);font-weight:550}
  nav .n{font-size:11px;color:var(--ink3);font-variant-numeric:tabular-nums}
  nav .grp{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink3);
           padding:16px 10px 4px}
  main{flex:1;padding:32px 40px;max-width:960px}
  h1{font-size:17px;font-weight:600;margin-bottom:2px}
  .sub{color:var(--ink3);font-size:12px;margin-bottom:24px;font-family:var(--mono)}
  h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--ink2);
     font-weight:600;margin:28px 0 10px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
  .tile b{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums;display:block}
  .tile span{color:var(--ink2);font-size:11.5px}
  .map{display:flex;flex-direction:column;gap:10px;margin-top:6px}
  .lane{display:flex;align-items:stretch;gap:0;flex-wrap:wrap}
  .lane-label{width:86px;flex-shrink:0;color:var(--ink3);font-size:11px;text-transform:uppercase;
              letter-spacing:.07em;padding-top:14px}
  .box{background:var(--panel);border:1px solid var(--line);border-radius:8px;
       padding:10px 14px;cursor:pointer;min-width:118px;transition:border-color .1s}
  .box:hover{border-color:var(--accent)}
  .box b{font-size:13px;font-weight:550;display:block}
  .box span{color:var(--ink2);font-size:11.5px}
  .arrow{align-self:center;color:var(--ink3);padding:0 8px;font-size:13px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:14px 16px;margin-bottom:10px}
  .badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;
         border:1px solid var(--line2);color:var(--ink2);margin-right:8px}
  .badge.retrieve{border-color:var(--accent);color:var(--accent)}
  .pill{font-size:11.5px;padding:2px 9px;border-radius:99px;font-weight:600}
  .pill.pass{background:var(--good-soft);color:var(--good)}
  .pill.fail{background:var(--bad-soft);color:var(--bad)}
  .pill.skip{background:var(--accent-soft);color:var(--accent)}
  .u{font-weight:550}
  .r{color:var(--ink2);white-space:pre-wrap;margin-top:6px}
  .meta{color:var(--ink3);font-size:11.5px;margin-top:8px;font-variant-numeric:tabular-nums}
  .tool{border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin-top:8px;background:var(--bg)}
  .tool.error{border-color:var(--bad);background:var(--bad-soft)}
  .tool.warn{border-color:var(--line2)}
  .tool-head{display:flex;align-items:center;gap:8px;font-size:12.5px}
  .dot{width:7px;height:7px;border-radius:99px;flex-shrink:0;background:var(--good)}
  .dot.error{background:var(--bad)} .dot.warn{background:#c8951f}
  .tool code{border:none;background:transparent;padding:0;color:var(--ink)}
  .tool details{margin-top:6px}
  .tool summary{font-size:11px;color:var(--ink3);cursor:pointer;list-style:none}
  .tool pre{font-family:var(--mono);font-size:11px;color:var(--ink2);white-space:pre-wrap;
            word-break:break-all;margin-top:6px;max-height:180px;overflow:auto}
  .live{display:inline-flex;align-items:center;gap:6px}
  .live .dot{animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .splitbar{display:flex;height:26px;border-radius:6px;overflow:hidden;border:1px solid var(--line);margin-top:2px}
  .splitbar div{display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#fff;min-width:2px}
  .seg-skip{background:var(--accent)} .seg-ret{background:#c8951f}
  .tile b.money{color:var(--good)}
  .arch{width:100%;min-width:760px;height:auto;font-family:-apple-system,system-ui,sans-serif}
  .arch .container{fill:none;stroke:var(--line2);stroke-dasharray:5 4}
  .arch .container.ops{stroke:var(--accent);opacity:.9}
  .arch .bx{fill:var(--panel);stroke:var(--line);stroke-width:1}
  .arch .node{cursor:pointer}
  .arch .node:hover .bx{stroke:var(--accent);stroke-width:1.5}
  .arch .loopbox{fill:none;stroke:var(--accent);stroke-width:1.5}
  .arch .memgroup{fill:var(--accent-soft);opacity:.4;stroke:var(--line2);stroke-width:1;stroke-dasharray:5 4}
  .arch .gate{fill:var(--accent-soft);stroke:var(--accent);stroke-width:1.2}
  .arch .nt{fill:var(--ink);font-size:12.5px;font-weight:600}
  .arch .ns{fill:var(--ink2);font-size:10.5px}
  .arch .grp{fill:var(--ink3);font-size:10px;font-weight:700;letter-spacing:.07em}
  .arch .fl{fill:var(--ink3);font-size:9.5px}
  .arch .flow{fill:none;stroke:var(--ink3);stroke-width:1.3;marker-end:url(#arr)}
  .arch .flow.dash{stroke-dasharray:4 3;opacity:.75}
  .arch .head{fill:var(--ink3)}
  /* live animation: node lights up (fill+stroke) + flowing edge (n8n-style).
     drop-shadow(var()) is unreliable on SVG, so we light the fill instead. */
  .arch .bx{transition:stroke .15s ease, fill .15s ease, stroke-width .15s ease}
  .arch .node.hot .bx{stroke:var(--accent) !important;stroke-width:2.6;fill:var(--accent-soft) !important}
  .arch .gate{transition:stroke .15s ease, stroke-width .15s ease}
  .arch .gate.hot{stroke:var(--accent) !important;stroke-width:4}
  .arch .flow.live{stroke:var(--accent) !important;stroke-width:2.6;opacity:1;stroke-dasharray:6 5;
                   animation:flowdash .5s linear infinite}
  @keyframes flowdash{to{stroke-dashoffset:-22}}
  .arch-status{font-size:11px;font-weight:600;color:var(--accent);text-transform:none;
               letter-spacing:0;margin-left:10px}
  .arch-status .live-dot{display:inline-block;width:7px;height:7px;border-radius:99px;
               background:var(--accent);margin-right:5px;vertical-align:middle;animation:pulse 1s ease-in-out infinite}
  @media (prefers-reduced-motion:reduce){.arch .flow.live{animation:none}.arch-status .live-dot{animation:none}}
  .convo{display:flex;flex-direction:column;gap:8px}
  .msg{border:1px solid var(--line);border-radius:9px;padding:10px 13px;max-width:78%}
  .msg.user{align-self:flex-end;background:var(--accent-soft);border-color:transparent}
  .msg.assistant{align-self:flex-start;background:var(--panel)}
  .msg .who{font-size:11px;color:var(--ink3);font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px}
  .msg .mtext{font-size:13.5px;white-space:pre-wrap;color:var(--ink)}
  .chip-c{display:inline-block;font-size:9.5px;font-weight:600;padding:1px 6px;border-radius:99px;
          background:var(--good-soft);color:var(--good);text-transform:none;letter-spacing:0;vertical-align:middle}
  .chatlog{display:flex;flex-direction:column;gap:10px;margin-bottom:96px}
  .bubble{align-self:flex-end;background:var(--accent);color:#fff;padding:8px 13px;
          border-radius:14px 14px 3px 14px;max-width:75%;font-size:13.5px}
  .chatbar{position:fixed;bottom:0;left:208px;right:0;background:var(--bg);
           border-top:1px solid var(--line);padding:14px 40px;display:flex;gap:10px;max-width:1000px}
  .chatbar input{flex:1;background:var(--panel);border:1px solid var(--line2);border-radius:8px;
                 padding:10px 14px;color:var(--ink);font-size:14px;outline:none}
  .chatbar input:focus{border-color:var(--accent)}
  .chatbar button{background:var(--accent);color:#fff;border:none;border-radius:8px;
                  padding:0 18px;font-weight:600;font-size:13.5px;cursor:pointer}
  .chatbar button:disabled{opacity:.5;cursor:default}
  .stages{display:flex;gap:6px;margin:2px 0 4px}
  .stage{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid var(--line2);color:var(--ink3)}
  .stage.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
  .stage.done{border-color:var(--good);color:var(--good)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td,th{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  tr:last-child td{border-bottom:none}
  th{color:var(--ink3);font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;font-weight:600}
  .empty{color:var(--ink3);font-style:normal;font-size:13px}
  code{font-family:var(--mono);font-size:12px;background:var(--bg);border:1px solid var(--line);
       padding:1px 5px;border-radius:4px}
</style></head><body>
<nav>
  <div class="brand">Jarvis<small id="model"></small></div>
  <div class="grp">Test</div>
  <a href="#chat" data-v="chat">Chat &amp; watch</a>
  <div class="grp">System</div>
  <a href="#overview" data-v="overview">Overview</a>
  <a href="#sessions" data-v="sessions">Sessions <span class="n" id="n-sess"></span></a>
  <a href="#loop" data-v="loop">Loop <span class="n" id="n-loop"></span></a>
  <a href="#memory" data-v="memory">Memory <span class="n" id="n-mem"></span></a>
  <a href="#tools" data-v="tools">Tools <span class="n" id="n-tools"></span></a>
  <a href="#ops" data-v="ops">Ops <span class="n" id="n-ops"></span></a>
</nav>
<main>
  <h1 id="title">Overview</h1>
  <div class="sub" id="sub"></div>
  <div id="view"></div>
</main>
<script>
const esc = s => (s??"").toString().replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let D = null;

const money = n => "$" + (n < 0.01 ? n.toFixed(4) : n.toFixed(2));
const secs = ms => ms==null ? "—" : (ms/1000).toFixed(1)+"s";

const gateBadge = g => !g ? "" :
  `<span class="badge ${g.decision==="retrieve"?"retrieve":""}">gate · ${esc(g.decision)}</span><span class="meta" style="margin:0">${esc(g.reason||"")}</span>`;

// A tool call renders as a status row (dot + one-line summary); the raw output
// hides behind a disclosure so an ugly osascript error never floods the page.
const toolRow = x => `<div class="tool ${x.status}">
  <div class="tool-head"><span class="dot ${x.status}"></span><code>${esc(x.tool)}</code>
    <span style="color:var(--ink2)">${esc(x.summary)}</span></div>
  <details><summary>args &amp; raw output</summary>
    <pre>${esc(x.tool)}(${esc(JSON.stringify(x.args,null,1))})\\n\\n${esc(x.output)}</pre>
  </details>
</div>`;

const turnCard = t => `<div class="card">
  <div class="u">${esc(t.user_message)}</div>
  <div class="meta" style="margin-top:4px">${gateBadge(t.gate)}</div>
  ${(t.tools||[]).map(toolRow).join("")}
  <div class="r">${esc(t.reply)}</div>
  <div class="meta">${esc((t.ts||"").replace("T"," ").slice(0,19))} · ${secs(t.latency_ms)} · ${t.iterations??"?"} iter · ${money(t.cost||0)}${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>
</div>`;

const table = (heads, rows) => rows.length
  ? `<div class="card" style="padding:4px 8px"><table><tr>${heads.map(h=>`<th>${h}</th>`).join("")}</tr>${rows.join("")}</table></div>`
  : `<div class="card empty">nothing here yet</div>`;

const gateSplit = s => {
  const tot = s.gate_skips + s.gate_retrieves || 1;
  const skipPct = Math.round(s.gate_skips/tot*100), retPct = 100-skipPct;
  return `<div class="splitbar">
    <div class="seg-skip" style="width:${skipPct}%">${s.gate_skips} skipped</div>
    <div class="seg-ret" style="width:${retPct}%">${s.gate_retrieves} retrieved</div>
  </div><div class="meta" style="margin-top:6px">the retrieval gate skipped memory on ${skipPct}% of turns — that's latency and bias saved</div>`;
};

// --- Chat gateway: type here, watch the harness run (turns kept in memory)
const CHAT = [];
const chatTurnCard = t => `<div class="card">
  ${t.gate?`<div class="stages"><span class="stage done">gate · ${esc(t.gate.decision)}</span>${(t.tools||[]).map(x=>`<span class="stage done">tool · ${esc(x.tool)}</span>`).join("")}<span class="stage done">reply</span></div>
    <div class="meta" style="margin:0 0 6px">${esc(t.gate.reason||"")}</div>`:""}
  ${(t.tools||[]).map(toolRow).join("")}
  <div class="r" style="margin-top:8px">${esc(t.reply)}</div>
  <div class="meta">${secs(t.latency_ms)} · ${t.iterations??"?"} iter${t.consolidation?` · consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>
</div>`;

function chatView(){
  return `<div class="chatlog" id="chatlog">${
    CHAT.length ? CHAT.map(m => m.role==="user"
        ? `<div class="bubble">${esc(m.text)}</div>`
        : m.pending ? `<div class="card"><div class="stages"><span class="stage on">gate</span><span class="stage">loop</span><span class="stage">tools</span><span class="stage">reply</span></div><div class="meta" style="margin:0">running the harness…</div></div>`
        : chatTurnCard(m)).join("")
      : `<div class="empty">Type a message and watch the gate, loop, tools, and memory react — the same harness the CLI, voice, and Telegram gateways drive.</div>`
  }</div>
  <div class="chatbar">
    <input id="msg" placeholder="Message Jarvis — e.g. schedule a swim with Sergey Saturday 5pm" autocomplete="off">
    <button id="send">Send</button>
  </div>`;
}

async function sendChat(){
  const input = document.getElementById("msg");
  const text = (input.value||"").trim();
  if (!text) return;
  input.value = "";
  CHAT.push({role:"user", text});
  const pending = {role:"jarvis", pending:true};
  CHAT.push(pending);
  document.getElementById("view").innerHTML = chatView();
  wireChat(); scrollChat();
  try {
    const res = await (await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({message:text})})).json();
    Object.assign(pending, {pending:false}, res.error ? {reply:"Error: "+res.error} : res);
  } catch(e){ Object.assign(pending, {pending:false, reply:"Error: "+e}); }
  document.getElementById("view").innerHTML = chatView();
  wireChat(); scrollChat();
  refresh();  // update the other tabs' data (Loop, Memory, Ops) live
}
function wireChat(){
  const b = document.getElementById("send"), i = document.getElementById("msg");
  if (b) b.onclick = sendChat;
  if (i){ i.focus(); i.onkeydown = e => { if (e.key==="Enter") sendChat(); }; }
}
function scrollChat(){ const l=document.getElementById("chatlog"); if(l) window.scrollTo(0, document.body.scrollHeight); }

// --- Architecture: a calm live SVG that mirrors the whiteboard's structure
// (Harness wraps the ephemeral run · Loop is a cycle · memory feeds up through
// the gate · LLM Ops is a separate loop). Deliberately few arrows + lots of
// air — the detail lives in each tab. Every node is live and clickable.
function archSVG(d){
  const s = d.stats;
  const box = (x,y,w,h,title,sub,view,cls="",nid="") =>
    `<g class="node ${cls}" ${nid?`id="n-${nid}"`:""} ${view?`onclick="location.hash='${view}'"`:""}>
       <rect class="bx" x="${x}" y="${y}" width="${w}" height="${h}" rx="9"/>
       <text class="nt" x="${x+13}" y="${y+24}">${title}</text>
       ${sub?`<text class="ns" x="${x+13}" y="${y+42}">${sub}</text>`:""}
     </g>`;
  const lbl = (x,y,t) => `<text class="grp" x="${x}" y="${y}">${t}</text>`;
  const flow = (d2,cls="",id="") => `<path class="flow ${cls}" ${id?`id="${id}"`:""} d="${d2}"/>`;
  const flowLbl = (x,y,t,anchor="start") => `<text class="fl" x="${x}" y="${y}" text-anchor="${anchor}">${t}</text>`;

  return `<div style="overflow-x:auto"><svg viewBox="0 0 1020 700" class="arch" role="img">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>

    <!-- HARNESS container -->
    <rect class="container" x="12" y="20" width="662" height="628" rx="16"/>
    ${lbl(32,48,"HARNESS — one ephemeral turn")}

    <!-- the turn: gateway → working memory → loop → reply -->
    ${box(32,72,128,56,"Gateway","cli · voice · web","chat","","gateway")}
    ${flow("M160 100 L192 100","","e-gw-wm")}
    ${box(192,72,144,56,"Working memory","assembled per turn","memory","","wm")}

    <rect class="loopbox" x="372" y="64" width="164" height="128" rx="12"/>
    ${lbl(386,56,"LOOP")}
    ${box(386,76,136,44,"LLM agent","reason","loop","","llm")}
    ${box(386,132,136,48,"Tools","create_event…","tools","","tools")}
    ${flow("M446 120 L446 132")}${flow("M468 132 L468 120")}
    ${flow("M336 100 L372 100","","e-wm-loop")}
    ${flow("M536 110 L556 110")}${flowLbl(540,104,"reply")}
    ${box(556,84,104,52,"Reply","→ back to you","loop","","reply")}
    <!-- reply loops back to the gateway (next turn) -->
    <path class="flow" id="e-reply-gw" d="M608 84 C608 42 360 42 96 68" marker-end="url(#arr)"/>
    ${flowLbl(372,38,"next turn")}
    <!-- every turn is saved for consolidation: down the right inner lane,
         then left into the consolidation box -->
    <path class="flow dash" id="e-reply-save" d="M652 136 C666 150 666 200 666 600 L648 600" marker-end="url(#arr)"/>
    ${flowLbl(662,340,"save chats",'end')}

    <!-- retrieval gate feeding working memory (the hero) -->
    <path class="gate" id="n-gate" d="M264 250 L340 296 L264 342 L188 296 Z"/>
    <text class="nt" x="264" y="292" text-anchor="middle">Retrieval gate</text>
    <text class="ns" x="264" y="310" text-anchor="middle">${s.gate_skips} skip · ${s.gate_retrieves} retrieve</text>
    ${flow("M264 250 L264 128","dash","e-gate-wm")}${flowLbl(274,196,"only if needed")}

    <!-- MEMORY: grouped section with a direct link from the gate to each pillar -->
    ${lbl(40,404,"MEMORY — three pillars")}
    <rect class="memgroup" x="28" y="414" width="632" height="128" rx="12"/>
    ${flow("M150 452 L246 336","dash","e-gate-proc")}
    ${flow("M344 452 L272 344","dash","e-gate-sem")}
    ${flow("M556 452 L286 338","dash","e-gate-epi")}
    ${flowLbl(360,392,"the gate reads all three",'middle')}
    ${box(44,452,212,72,"Procedural","how to act · SKILL.md · "+d.skills.length+" skill(s)","memory","","procedural")}
    ${box(268,452,212,72,"Semantic · FTS5","durable facts · "+d.facts.length+" facts","memory","","semantic")}
    ${box(492,452,152,72,"Episodic",d.episodes.length+" episodes","memory","","episodic")}

    <!-- consolidation writes back into memory -->
    ${box(44,576,600,52,"Consolidation · every "+d.consolidate_every+" exchanges",d.chat_pending+"/"+d.consolidate_every*2+" queued → distilled into facts","memory","","consolidation")}
    ${flow("M340 576 L340 528","","e-consol-sem")}${flowLbl(350,560,"distill")}

    <!-- LLM OPS: the outer loop — observes the run, then improves it -->
    <rect class="container ops" x="700" y="20" width="308" height="392" rx="16"/>
    ${lbl(720,48,"LLM OPS — the outer loop")}
    <!-- every turn feeds the trace -->
    <path class="flow" id="e-reply-trace" d="M660 100 C700 94 722 92 748 92" marker-end="url(#arr)"/>
    ${flowLbl(700,80,"each turn")}
    ${box(720,68,272,52,"Trace",s.trace_files+" file(s) · always on","ops","","trace")}
    ${flow("M856 120 L856 138")}
    ${box(720,138,272,52,"Eval","deterministic + judge","ops")}
    ${flow("M856 190 L856 208")}
    ${box(720,208,272,52,"Release gate",d.eval_report?"det "+d.eval_report.deterministic+" · judge "+d.eval_report.judge:"run make gate","ops")}
    ${flow("M856 260 L856 278")}
    ${box(720,278,272,52,"Release","new prompt · model · config","ops")}
    <!-- feedback: release improves the harness — the outer loop closes,
         routed under everything in open canvas so it crosses nothing -->
    <path class="flow dash" d="M856 330 V672 H24 V100 H30" marker-end="url(#arr)"/>
    ${flowLbl(430,666,"improved prompt + config",'middle')}
  </svg></div>`;
}

const VIEWS = {
  chat(){ return chatView(); },
  overview(d){
    const s = d.stats;
    const tiles = [
        [money(s.cost),"spent (est)","money"],[secs(s.latency_avg),"avg turn",""],
        [s.turns,"turns",""],[s.tool_calls,"tool calls",""],
        [d.facts.length,"facts",""],[d.calendar.length,"events",""],
      ].map(([v,l,c])=>`<div class="tile"><b class="${c}">${v}</b><span>${l}</span></div>`).join("");
    return `<div class="tiles">${tiles}</div>
    <h2>Retrieval gate — the hero decision</h2>${gateSplit(s)}
    <h2 style="margin-top:26px">Architecture — click any box <span id="arch-status" class="arch-status"></span></h2>
    ${archSVG(d)}
    <h2>Latest turn</h2>${d.turns.length?turnCard(d.turns[0]):'<div class="card empty">no turns yet — talk to Jarvis first</div>'}`;
  },
  loop(d){
    return d.turns.length ? d.turns.map(turnCard).join("") : `<div class="card empty">no turns yet</div>`;
  },
  sessions(d){
    // the persistent conversation (working-memory history) across ALL gateways —
    // the "Current Chat History" box from the whiteboard, made real.
    const log = d.chat_log || [];
    if (!log.length) return `<div class="card empty">no conversation yet — talk to Jarvis and it shows up here</div>`;
    let h = `<div class="meta" style="margin-bottom:12px">The running conversation Jarvis remembers — every gateway (browser, phone, CLI) writes here. Rows marked <span class="chip-c">consolidated</span> have been distilled into semantic + episodic memory.</div>`;
    h += `<div class="convo">` + log.map(m => `
      <div class="msg ${m.role}">
        <div class="who">${m.role==="user"?"you":"jarvis"}${m.consolidated?` <span class="chip-c">consolidated</span>`:""}</div>
        <div class="mtext">${esc(m.content)}</div>
        <div class="meta" style="margin-top:4px">${esc((m.created_at||"").slice(0,19))}</div>
      </div>`).join("") + `</div>`;
    return h;
  },
  memory(d){
    let h = `<h2>Semantic — durable facts</h2>`;
    h += table(["subject","fact","source","when"], d.facts.map(f =>
      `<tr><td><code>${esc(f.subject)}</code></td><td>${esc(f.content)}</td><td>${esc(f.source)}</td><td class="meta">${esc(f.created_at)}</td></tr>`));
    h += `<h2>Episodic — what happened, when</h2>`;
    h += table(["date","episode"], d.episodes.map(e =>
      `<tr><td class="meta">${esc(e.happened_at)}</td><td>${esc(e.summary)}</td></tr>`));
    h += `<h2>Consolidation</h2><div class="card">${d.chat_pending} unconsolidated message(s).
          The summarizer distills chats into facts + an episode every ${d.consolidate_every} exchanges.</div>`;
    h += `<h2>Procedural — loaded skills</h2>`;
    h += table(["skill","when it triggers"], d.skills.map(s =>
      `<tr><td><code>${esc(s.name)}</code></td><td>${esc(s.description)}</td></tr>`));
    return h;
  },
  tools(d){
    let h = `<h2>Calendar events</h2>`;
    h += table(["event","start","end","with"], d.calendar.map(e =>
      `<tr><td>${esc(e.title)}</td><td class="meta">${esc(e.start)}</td><td class="meta">${esc(e.end)}</td><td>${esc(e.attendees)}</td></tr>`));
    h += `<div class="meta" style="margin-bottom:16px">also written to <code>calendar.ics</code> — import with <code>open .jarvis/calendar.ics</code></div>`;
    h += `<h2>Outbox — drafted messages</h2>`;
    h += d.outbox.length ? d.outbox.map(o=>`<div class="card"><span class="u">${esc(o.name)}</span><div class="r">${esc(o.text)}</div></div>`).join("")
                         : `<div class="card empty">no drafted messages</div>`;
    return h;
  },
  ops(d){
    const s = d.stats;
    let h = `<div class="tiles">${[
        [money(s.cost),"spent (est)","money"],[s.tokens_in.toLocaleString(),"tokens in",""],
        [s.tokens_out.toLocaleString(),"tokens out",""],[secs(s.latency_avg),"avg turn",""],
        [secs(s.latency_p95),"p95 turn",""],[`${s.tool_errors}`,"tool errors",s.tool_errors?"":""],
      ].map(([v,l,c])=>`<div class="tile"><b class="${c}">${v}</b><span>${l}</span></div>`).join("")}</div>`;

    h += `<h2>Retrieval gate</h2>${gateSplit(s)}`;

    h += `<h2>Release gate</h2>`;
    h += d.eval_report ? `<div class="card">
        <span class="pill ${d.eval_report.deterministic}">deterministic · ${d.eval_report.deterministic}</span>
        <span class="pill ${d.eval_report.judge==="pass"?"pass":d.eval_report.judge==="fail"?"fail":"skip"}" style="margin-left:8px">llm-judge · ${d.eval_report.judge}</span>
        <div class="meta">last run ${esc(d.eval_report.ran_at)} — refresh with <code>make gate</code></div></div>`
      : `<div class="card empty">no eval report yet — run <code>make gate</code></div>`;

    h += `<h2>Slowest turns</h2>`;
    const slow = [...d.turns].filter(t=>t.latency_ms!=null).sort((a,b)=>b.latency_ms-a.latency_ms).slice(0,6);
    h += table(["turn","latency","cost","tools"], slow.map(t =>
      `<tr><td>${esc((t.user_message||"").slice(0,48))}</td><td class="meta">${secs(t.latency_ms)}</td><td class="meta">${money(t.cost||0)}</td><td class="meta">${(t.tools||[]).map(x=>x.tool).join(", ")||"—"}</td></tr>`));

    h += `<h2>Tracing</h2><div class="card">${s.trace_files} trace file(s) in <code>traces/</code> — every turn as JSONL.
          Span waterfalls: <code>make trace</code> + <code>OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317</code>.</div>`;

    if (d.wake_scans.length){
      h += `<h2>Voice — wake near-misses</h2>`;
      h += table(["heard","when"], d.wake_scans.map(w =>
        `<tr><td>${esc(w.heard)}</td><td class="meta">${esc((w.ts||"").replace("T"," ").slice(0,19))}</td></tr>`));
    }
    return h;
  },
};

// ---- Live harness animation: light up the diagram as a turn flows through,
// driven by the trace stream so ANY gateway (browser, phone, CLI) triggers it.
const STAGE = {
  turn_start:    {nodes:["gateway","wm"],            edges:["e-gw-wm"],                 label:"message in"},
  gate:          {nodes:["gate"],                    edges:["e-gate-wm"],               label:"retrieval gate"},
  llm:           {nodes:["llm"],                     edges:["e-wm-loop"],               label:"agent reasons"},
  tool:          {nodes:["tools"],                   edges:[],                          label:"tool runs"},
  turn_end:      {nodes:["reply","trace"],           edges:["e-reply-trace","e-reply-save"], label:"reply"},
  consolidation: {nodes:["consolidation","semantic"],edges:["e-consol-sem"],            label:"consolidating memory"},
};
let evCursor = null, evQueue = [], playing = false, animating = false;

function hot(sel, cls, ms){
  const el = document.querySelector(sel);
  if (!el) return;
  el.classList.add(cls);
  setTimeout(()=>el.classList.remove(cls), ms);
}
function animateStage(ev){
  const spec = STAGE[ev.type];
  if (!spec || !document.querySelector(".arch")) return;
  const status = document.getElementById("arch-status");
  if (status) status.innerHTML = `<span class="live-dot"></span>${spec.label}`;
  spec.nodes.forEach(n => hot("#n-"+n, "hot", 1000));
  spec.edges.forEach(e => hot("#"+e, "live", 1000));
  if (ev.type==="gate" && ev.decision==="retrieve"){
    ["procedural","semantic","episodic"].forEach(n => hot("#n-"+n,"hot",1000));
    ["e-gate-proc","e-gate-sem","e-gate-epi"].forEach(e => hot("#"+e,"live",1000));
  }
}
function playNext(){
  if (!evQueue.length){ playing=false; animating=false;
    const st=document.getElementById("arch-status"); if(st) st.innerHTML=""; return; }
  playing = true; animating = true;
  animateStage(evQueue.shift());
  setTimeout(playNext, 620);   // stagger so stages light up in sequence
}
async function pollEvents(){
  try{
    const r = await (await fetch("/api/events" + (evCursor==null?"":"?cursor="+evCursor))).json();
    if (evCursor != null && r.events.length){
      evQueue.push(...r.events);
      if (!playing) playNext();
    }
    evCursor = r.cursor;
  } catch(e){ /* server busy */ }
}

let activeView = null;
const TITLES = {chat:"Chat & watch", ops:"LLM Ops"};
function render(){
  if (!D) return;
  const v = (location.hash||"#chat").slice(1);
  const view = VIEWS[v] ? v : "overview";
  document.querySelectorAll("nav a").forEach(a=>a.classList.toggle("on", a.dataset.v===view));
  document.getElementById("title").textContent = TITLES[view] || view[0].toUpperCase()+view.slice(1);
  // Chat owns its DOM (don't wipe the input mid-type on the 5s refresh);
  // (re)build it only when first entering the tab.
  if (view === "chat"){
    if (activeView !== "chat"){ document.getElementById("view").innerHTML = chatView(); wireChat(); }
  } else if (view === "overview"){
    // don't rebuild mid-animation or the glowing SVG gets wiped
    if (activeView !== "overview" || !animating){ document.getElementById("view").innerHTML = VIEWS.overview(D); }
  } else {
    document.getElementById("view").innerHTML = VIEWS[view](D);
  }
  activeView = view;
  document.getElementById("model").textContent = `${D.provider} · ${D.model}`;
  document.getElementById("n-sess").textContent = (D.chat_log||[]).length;
  document.getElementById("n-loop").textContent = D.stats.turns;
  document.getElementById("n-mem").textContent = D.facts.length + D.episodes.length;
  document.getElementById("n-tools").textContent = D.calendar.length + D.outbox.length;
  document.getElementById("n-ops").textContent = D.stats.tool_errors || (D.eval_report ? "" : "!");
}
let lastFetch = Date.now();
function tickLive(){
  if (!D) return;
  const ago = Math.round((Date.now()-lastFetch)/1000);
  document.getElementById("sub").innerHTML =
    `<span class="live"><span class="dot"></span>live</span> · updated ${ago}s ago · ${esc(D.home)}`;
}
async function refresh(){
  try { D = await (await fetch("/api/data")).json(); lastFetch = Date.now(); render(); tickLive(); }
  catch(e){ /* server restarting — keep showing last data */ }
}
window.addEventListener("hashchange", render);
window.__hold = (v)=>{ animating = v; };   // test hook: freeze the diagram
refresh(); setInterval(refresh, 5000); setInterval(tickLive, 1000);
pollEvents(); setInterval(pollEvents, 450);   // live harness animation
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server API
        if self.path == "/api/data":
            self._send(json.dumps(collect(), default=str).encode(), "application/json")
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse

            raw = parse_qs(urlparse(self.path).query).get("cursor", [None])[0]
            cursor = int(raw) if raw and raw.lstrip("-").isdigit() else None
            self._send(json.dumps(events_since(cursor)).encode(), "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):  # noqa: N802 — browser gateway: run a real turn
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or "{}")
        message = (payload.get("message") or "").strip()
        try:
            out = chat(message) if message else {"error": "empty message"}
        except Exception as exc:  # surface, don't 500 — the browser shows it
            out = {"error": f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(out, default=str).encode(), "application/json")

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> None:
    import os

    base = int(os.getenv("JARVIS_DASHBOARD_PORT", str(PORT)))
    for port in range(base, base + 10):  # walk past a busy port instead of crashing
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            print(f"port {port} busy, trying {port + 1}…")
            continue
        print(f"Jarvis dashboard → http://localhost:{port}  (Ctrl-C to stop)")
        server.serve_forever()
        return
    raise SystemExit(f"no free port in {base}–{base + 9}")


if __name__ == "__main__":
    main()
