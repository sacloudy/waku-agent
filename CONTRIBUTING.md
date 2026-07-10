# Contributing

This repo is a teaching skeleton — contributions should keep it *small and readable*.
The bar for every PR: would a viewer pausing the video understand this file?

## The easiest contribution: a skill (no Python needed)

1. Copy [`skills/TEMPLATE.md`](skills/TEMPLATE.md) to `skills/community/<your-skill>/SKILL.md`
2. Fill in `name` + `description` (that's the official Agent Skills frontmatter) and the body
3. Test locally: `python scripts/validate_skills.py`, then chat — your skill loads when it matches
4. Open a PR. CI runs the same validator.

Anyone can then try your skill instantly:
`python -m jarvis skill install <link to your SKILL.md>`

## Code contributions

- **Gateways** (`jarvis/gateway/`): implement receive/send for a new channel
  (WhatsApp, Discord, Slack, email). Keep it one file; the CLI gateway is the reference.
- **Memory stores** (`jarvis/memory/semantic/`): match the `add`/`search` interface
  of `SqliteFactStore`. The Supabase adapter is the reference.
- **Tools** (`jarvis/tools/`): the repo deliberately ships only the flagship-task
  tools. New tools belong in your fork or a skill — see "scope" below.

Run the gate before pushing: `make gate` (deterministic evals must pass;
judge evals run if you have a key). `make lint` too.

## Scope (what we'll say no to, kindly)

No frameworks, no multi-agent routing, no enterprise features, no tool sprawl.
If it makes the skeleton harder to read in an afternoon, it doesn't go in —
even if it's good. Fork freely; that's what MIT is for.

## Community

Questions, show-and-tell, pair-debugging: [Discord](https://discord.gg/7Ntxzm3eJ).
