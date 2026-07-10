# Architecture — the whiteboard, refreshed

The same system as the two whiteboard diagrams from the previous videos
(the generic Harness/Loop/Memory/LLM-Ops one and the Hermes-specific one),
now with a file path on every box.

```mermaid
flowchart TB
    subgraph GW["Gateway Interface — jarvis/gateway/"]
        CLI["cli.py (default)"]
        TG["telegram.py (optional)"]
    end

    subgraph RUN["Ephemeral Agent Run — everything here is rebuilt per turn"]
        WM["Working Memory — runtime/session.py<br/>SOUL.md + memory context + chat history"]
        subgraph LOOP["The Loop — loop/agent.py"]
            LLM["LLM call<br/>(loop/models.py)"]
            TOOLS["Tools — tools/<br/>create_event · save_note · send_message"]
            LLM -->|tool calls| TOOLS -->|results| LLM
        end
        WM --> LLM
        GUARD["end-loop guardrails:<br/>no-tool-call exit · max iterations"]
    end

    GW --> WM
    LLM -->|reply| GW

    subgraph MEM["Memory — jarvis/memory/"]
        GATE{{"retrieval_gate.py<br/>'does this turn need memory?'"}}
        PROC["procedural/ — SKILL.md<br/>how to act"]
        SEM["semantic/ — facts (FTS5,<br/>or Supabase pgvector)"]
        EPI["episodic/ — dated events"]
        CONS{{"consolidation.py<br/>'only after N new chats'"}}
        DB[("state.db — one SQLite file")]
    end

    WM -.->|every turn| GATE
    GATE -->|only if needed| SEM & EPI
    PROC -->|on keyword match| WM
    GW -->|save messages| DB
    CONS -->|distill into facts| SEM
    CONS -->|one episode| EPI
    SEM & EPI --- DB

    subgraph OPS["LLM Ops — jarvis/ops/ + evals/"]
        TRACE["tracing.py — 1 trace/run<br/>JSONL always · OTel → Phoenix/Langfuse"]
        DET["evals/deterministic — 0/1<br/>'did the right tool fire?'"]
        JUDGE["evals/judge — scored %<br/>'was the reply good?'"]
        RGATE{{"release_gate.py"}}
        TRACE --> DET & JUDGE --> RGATE -->|eval passed| SHIP["release: new prompt/<br/>model/config version"]
    end

    RUN -.->|every event| TRACE
```

## Design decisions worth stealing

- **The gate before retrieval** (not retrieval on every turn): a cheap-model judge
  answers "does this message need the user's memory?" — saves latency and, more
  importantly, keeps irrelevant memories from biasing answers.
- **Consolidation is batched** ("after N chats"), asynchronous to the reply path,
  and loss-safe: if the summarizer fails, the chat log stays unconsolidated.
- **Deterministic evals and judge evals never mix.** One is a unit test, the other
  is a scored opinion. The release gate requires 100% of the first and a threshold
  on the second.
- **Every layer has a boring default and a documented upgrade** — FTS5 → pgvector,
  mock calendar → Google Calendar, JSONL → Phoenix/Langfuse. The default is always
  zero-signup.

## What this deliberately is not

Not a framework, not multi-agent, not production. It's the readable blueprint —
OpenClaw and Hermes are the products; this is the afternoon read that explains them.
