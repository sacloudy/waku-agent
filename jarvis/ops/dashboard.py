"""Dashboard — every pillar on one local page. Zero new dependencies.

    make dashboard        # → http://localhost:7777

One stdlib HTTP server reading the files Jarvis already writes:
  loop + harness   traces/*.jsonl   (turns, gate decisions, tool calls, tokens)
  memory           state.db         (facts, episodes, chat log, consolidation)
  tools            state.db + calendar.ics + outbox/
  eval             eval_report.json (written by `make gate`)

This is the "open the hood" view for humans who won't run SQL. It reads and
displays — it never mutates. For deep trace waterfalls use Phoenix
(`make trace`); this page is the product-simple summary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jarvis.config import load_settings
from jarvis.db import connect

PORT = 7777


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
    for path in sorted((home / "traces").glob("*.jsonl")):
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
        current["reply"] = "⚠ TURN NEVER FINISHED — check for a hang after this point"
        turns.append(current)

    tokens_in = sum(c.get("usage", {}).get("in", 0) for t in turns for c in t["llm_calls"])
    tokens_out = sum(c.get("usage", {}).get("out", 0) for t in turns for c in t["llm_calls"])

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
            "gate_skips": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "skip"),
            "gate_retrieves": sum(1 for t in turns if t["gate"] and t["gate"].get("decision") == "retrieve"),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
        "turns": turns[::-1][:50],          # newest first
        "wake_scans": wake_scans[::-1][:25],
        "facts": rows("SELECT subject, content, source, created_at FROM facts ORDER BY id DESC"),
        "episodes": rows("SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC"),
        "chat_pending": conn.execute("SELECT COUNT(*) FROM chat_log WHERE consolidated=0").fetchone()[0],
        "consolidate_every": settings.consolidate_every,
        "calendar": rows('SELECT title, start, "end", attendees, created_at FROM calendar_events ORDER BY start'),
        "outbox": outbox,
        "eval_report": eval_report,
    }


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Jarvis — dashboard</title>
<style>
  :root{--bg:#0f1117;--card:#181b24;--line:#262b38;--tx:#e6e9f0;--dim:#8b93a7;
        --harness:#e5484d;--loop:#f5a524;--memory:#46a758;--ops:#4c8bf5;}
  *{box-sizing:border-box;margin:0}
  body{background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,system-ui,sans-serif;padding:24px;max-width:1100px;margin:auto}
  h1{font-size:20px;margin-bottom:2px} .sub{color:var(--dim);font-size:12px;margin-bottom:20px}
  .stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
  .stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;min-width:110px}
  .stat b{font-size:20px;display:block} .stat span{color:var(--dim);font-size:11px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;margin:26px 0 10px;padding-left:10px;border-left:3px solid var(--c,#888)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
  .badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;margin-right:6px}
  .skip{background:#2a2417;color:var(--loop)} .retrieve{background:#15251a;color:var(--memory)}
  .tool{background:#141d2f;color:var(--ops);font-family:ui-monospace,monospace;font-size:11px;padding:4px 8px;border-radius:6px;display:block;margin:4px 0;white-space:pre-wrap;word-break:break-all}
  .u{color:var(--tx);font-weight:600} .r{color:var(--dim);white-space:pre-wrap}
  .meta{color:var(--dim);font-size:11px;margin-top:6px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
  th{color:var(--dim);font-size:11px;text-transform:uppercase}
  .pass{color:var(--memory);font-weight:700}.fail{color:var(--harness);font-weight:700}
  .empty{color:var(--dim);font-style:italic}
  code{background:#11141c;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body>
<h1>🤖 Jarvis dashboard</h1><div class="sub" id="sub">loading…</div>
<div class="stats" id="stats"></div>
<div id="main"></div>
<script>
const esc = s => (s??"").toString().replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function refresh(){
  const d = await (await fetch("/api/data")).json();
  document.getElementById("sub").textContent =
    `${d.home} · provider: ${d.provider} · model: ${d.model} · refreshed ${d.generated_at}`;
  const s = d.stats;
  document.getElementById("stats").innerHTML = [
    [s.turns,"turns"],[s.tool_calls,"tool calls"],
    [s.gate_skips+" / "+s.gate_retrieves,"gate skip / retrieve"],
    [s.tokens_in.toLocaleString(),"tokens in"],[s.tokens_out.toLocaleString(),"tokens out"],
    [d.facts.length,"facts"],[d.calendar.length,"events"]
  ].map(([v,l])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");

  let h = "";
  h += `<h2 style="--c:var(--ops)">Eval — the release gate</h2>`;
  h += d.eval_report ? `<div class="card">
      deterministic: <span class="${d.eval_report.deterministic}">${d.eval_report.deterministic.toUpperCase()}</span> ·
      LLM-judge: <span class="${d.eval_report.judge==='pass'?'pass':d.eval_report.judge==='fail'?'fail':''}">${d.eval_report.judge.toUpperCase()}</span>
      <div class="meta">last run ${d.eval_report.ran_at} — run <code>make gate</code> to refresh</div></div>`
    : `<div class="card empty">no eval report yet — run <code>make gate</code></div>`;

  h += `<h2 style="--c:var(--loop)">Loop — recent turns (from the trace)</h2>`;
  h += d.turns.length ? d.turns.map(t => `<div class="card">
      <div class="u">you › ${esc(t.user_message)}</div>
      ${t.gate?`<div class="meta"><span class="badge ${esc(t.gate.decision)}">🚪 gate: ${esc(t.gate.decision)}</span>${esc(t.gate.reason||"")}</div>`:""}
      ${(t.tools||[]).map(x=>`<span class="tool">⚙ ${esc(x.tool)}(${esc(JSON.stringify(x.args))})\n→ ${esc(x.output)}</span>`).join("")}
      <div class="r">jarvis › ${esc(t.reply)}</div>
      <div class="meta">${esc(t.ts)} · ${t.iterations??"?"} iteration(s) · ${(t.llm_calls||[]).map(c=>`${c.usage?.in??0}→${c.usage?.out??0} tok`).join(", ")}
      ${t.consolidation?` · 🧠 consolidated ${t.consolidation.new_facts} fact(s)`:""}</div>
    </div>`).join("") : `<div class="card empty">no turns yet — talk to Jarvis first</div>`;

  h += `<h2 style="--c:var(--memory)">Memory — semantic facts</h2>`;
  h += d.facts.length ? `<div class="card"><table><tr><th>subject</th><th>fact</th><th>source</th><th>when</th></tr>
      ${d.facts.map(f=>`<tr><td><code>${esc(f.subject)}</code></td><td>${esc(f.content)}</td><td>${esc(f.source)}</td><td>${esc(f.created_at)}</td></tr>`).join("")}</table></div>`
    : `<div class="card empty">nothing remembered yet — tell it something durable</div>`;

  h += `<h2 style="--c:var(--memory)">Memory — episodes & consolidation</h2><div class="card">`;
  h += d.episodes.length ? `<table><tr><th>date</th><th>episode</th></tr>${d.episodes.map(e=>`<tr><td>${esc(e.happened_at)}</td><td>${esc(e.summary)}</td></tr>`).join("")}</table>` : `<span class="empty">no episodes yet</span>`;
  h += `<div class="meta">consolidation: ${d.chat_pending} unconsolidated message(s) — summarizer runs every ${d.consolidate_every} exchanges</div></div>`;

  h += `<h2 style="--c:var(--harness)">Tools — calendar</h2>`;
  h += d.calendar.length ? `<div class="card"><table><tr><th>event</th><th>start</th><th>end</th><th>with</th></tr>
      ${d.calendar.map(e=>`<tr><td>${esc(e.title)}</td><td>${esc(e.start)}</td><td>${esc(e.end)}</td><td>${esc(e.attendees)}</td></tr>`).join("")}</table>
      <div class="meta">also in <code>calendar.ics</code> — import with: <code>open .jarvis/calendar.ics</code></div></div>`
    : `<div class="card empty">no events yet</div>`;

  h += `<h2 style="--c:var(--harness)">Tools — outbox</h2>`;
  h += d.outbox.length ? d.outbox.map(o=>`<div class="card"><b>${esc(o.name)}</b><div class="r">${esc(o.text)}</div></div>`).join("") : `<div class="card empty">no drafted messages</div>`;

  if (d.wake_scans.length){
    h += `<h2 style="--c:var(--harness)">Voice — recent wake near-misses</h2><div class="card">`;
    h += d.wake_scans.map(w=>`<div class="meta">👂 "${esc(w.heard)}" — not matched (${esc(w.ts)})</div>`).join("") + `</div>`;
  }
  document.getElementById("main").innerHTML = h;
}
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server API
        if self.path == "/api/data":
            body = json.dumps(collect(), default=str).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> None:
    print(f"Jarvis dashboard → http://localhost:{PORT}  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
