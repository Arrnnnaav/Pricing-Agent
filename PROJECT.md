# Project: Enterprise Pricing Intelligence Agent

One-stop overview. `README.md` is the pitch; `DECISIONS.md` is the reasoning
log; `FLOW.md` is the call graph; this file ties all three together plus
gives the "why does this exist" and "what's the story" framing.

## What it is

A daily-scheduled, multi-agent pipeline: watches competitor prices against
an internal catalog, recommends (or auto-executes) price changes inside
margin/business guardrails, routes anything low-confidence or risky through
a human approval gate (Slack, falls back to CLI), and keeps an append-only
audit trail of every decision it made and why.

Built as a portfolio project to demonstrate enterprise agent-system
patterns end to end, not a real production pricing tool — data is synthetic
(`data/generate_catalog.py`, seeded and reproducible), and it never scrapes
a real competitor site.

## The pipeline, one line each

```
Researcher   -- pulls competitor price history, parameterized SQL, no LLM
Analyst      -- computes price gap vs competitor avg, flags SKUs, no LLM
Decision     -- 1 SKU/category: Gemini call. 2+ SKUs/category: LP optimizer, no LLM.
Approval     -- auto-executes above confidence threshold, else Slack/CLI human gate
Execution    -- writes new price to SQLite
Audit        -- every stage logs to JSONL; end-of-run metrics computed from that log
```

Full detail on each stage: [[FLOW.md]].

## Patterns this project is built to demonstrate

- **Multi-agent pipeline** — sequential handoff (Researcher → Analyst →
  Decision → Approval → Execution), each stage a typed boundary, not a
  monolith prompt.
- **Structured LLM output everywhere** — Pydantic models validate every
  agent input/output and every tool call's input/output; nothing free-texts
  across a boundary.
- **Human-in-the-loop approval gate** — confidence-threshold auto-approve,
  Slack Block Kit + FastAPI webhook for everything else, CLI fallback.
- **Formal tool registry** — `tools/registry.py`'s `Tool` dataclass +
  `register()` + `call_tool()`: validates input/output, logs latency and
  success per call. A lightweight, MCP-flavored discipline without standing
  up a real MCP server (explicitly out of scope — see [[DECISIONS.md]]).
- **Reliability layer** — retry + fallback chain on the Gemini call
  (`agents/llm_client.py`'s `generate_structured`), prompt-level memoization
  within a run, cost/budget guard that aborts a run before it overspends.
- **Deterministic eval metrics** — computed from the audit trail after a
  run, not a second LLM-judge call: schema success rate, a hallucination
  heuristic anchored to real competitor-price numbers, tool-call success
  rate. See `audit/metrics.py`.
- **LP optimization** — `tools/optimizer.py`, PuLP, jointly optimizes a
  category's SKU prices under margin/change/budget/stock constraints — the
  multi-SKU path deliberately has no LLM in it at all.
- **A documented design reversal** — the project shipped an LLM-based
  "SQL/RAG" retrieval stage, then killed it after review found it added
  risk for zero real judgment. That reversal — what was caught, why, and
  what replaced it — is preserved in [[DECISIONS.md]] as evidence of
  engineering judgment, not just the final code.

## Repo map

```
agents/           Researcher, Analyst, Decision, Approval agents; Gemini client wrapper (llm_client.py); guardrails.py
db/               SQLite schema + repos (catalog, competitor prices)
data/             Synthetic data generators (generate_catalog.py, mock_price_generator.py) + retrieval (price_source.py)
tools/            Tool registry: LP optimizer (called live), semantic_matcher + scraper_tool (registered, not wired into live pipeline)
audit/            Append-only JSONL logger + post-run eval metrics
models.py         Pydantic schemas for every agent/tool boundary
config.py         Thresholds, guardrails, paths, GEMINI_MODEL (see churn note in DECISIONS.md)
main.py           Orchestrates one full pipeline run (run_pipeline)
scheduler.py      Wraps main.py in a daily APScheduler job
slack_webhook.py  FastAPI receiver for Slack approval button clicks
tests/            pytest — one file per module, run with `pytest tests/ -v`
```

## Known limitations (honest, not hidden)

- Synthetic data only. No real scraping — `data/mock_price_generator.py` is
  a drop-in swap point if a real source is added later; nothing downstream
  changes.
- `semantic_matcher` and `scraper` tools are registered but not called from
  the live pipeline — they demonstrate the registry pattern, not load-bearing.
- LP optimizer's elasticity coefficients and category budget are simplified,
  config-driven constants, not estimated from real demand data.
- Slack signature verification is conditional on `SLACK_SIGNING_SECRET`
  being set (warns and skips otherwise) — a deliberate dev-friendly
  tradeoff, not an oversight.
- `latency_ms` is logged on every audit event but has no reader/consumer
  yet (no dashboard, no alerting on it).
- No `fallback_used: true` flag on the audit event when the Gemini fallback
  chain actually fires — the fallback works, but you can't currently query
  "how often did we fall back" from the audit log alone.

## Where to start reading code

`main.py: run_pipeline` top to bottom, following [[FLOW.md]] alongside it.
Then `models.py` to see every schema that constrains the boundaries.
