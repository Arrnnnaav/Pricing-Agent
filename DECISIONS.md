# Decisions

What was decided, what the alternatives were, and why. Read this first when
picking up work — reasoning's already worked out, don't re-decide it.

Newest at top.

---

## SQL/RAG in Researcher: removed, replaced with parameterized query
**Decided:** 2026-08-19

Researcher originally generated its retrieval SQL via an LLM call per batch
("SQL/RAG" pattern) — the idea being genuine natural-language-to-SQL judgment.

Reviewed after implementation and found the query never actually varied in
shape between calls — every run needed "all competitor rows for these SKUs,"
full stop. An LLM call with no real decision behind it costs hallucination
risk, latency, $$, and a single point of failure in the pipeline's first
stage, for zero judgment exercised.

**Alternative considered:** make the query shape artificially vary (e.g. add
a date-range or competitor filter) just to give the LLM something real to
decide. Rejected — that's inventing a requirement to justify a design
choice, not solving a real problem.

**What shipped instead:** one parameterized query in `data/price_source.py`
(`fetch_batch`), `?` placeholders, never string-formatted — kept the
injection-safety habit even though this pipeline generates its own SKU list
internally (no untrusted input reaches this query today, but the habit is
the point).

Genuine retrieval-needs-judgment case still exists in the codebase:
`tools/semantic_matcher.py` — fuzzy-matches a competitor's free-text listing
name to a SKU code, a case where deterministic matching actually fails.
That's the retrieval story worth telling, not the SQL generation.

Full before/after detail: `docs/superpowers/specs/2026-08-19-enterprise-pricing-agent-design.md`
(gitignored, local-only — see [[FLOW.md]] for why).

---

## Hallucination heuristic: anchor to real trend data, not reasoning text
**Decided:** 2026-08-19

Original heuristic flagged a recommendation as a possible hallucination by
checking whether the LLM's *reasoning text* contained words like "trend" or
"elasticity." That only checks whether the model claimed to have a reason —
not whether the reason was true.

**Fix:** pull `avg_competitor_trend_pct` (a real number, computed from
actual competitor price history) onto the audit event next to
`min_competitor_price`/`max_competitor_price`. Flag a recommendation only
when **both**: price falls outside the competitor's min/max range, **and**
`abs(avg_competitor_trend_pct) < 0.01` (i.e., the real trend is statistical
noise, not a genuine signal). A recommendation with a real 5% trend behind
an out-of-range price is not flagged; one with a 0.1% "trend" and a
templated-sounding justification is.

Verified live: flag count dropped from 2 → 1 on the same run, with the
remaining flag traceable to an actually-low trend + out-of-range price —
i.e., the heuristic got *more* precise, not just quieter.

---

## Storage: SQLite over flat CSV/Postgres
**Decided:** 2026-08-19 (brainstorming phase)

Flat CSVs (`catalog.csv`, `competitor_price_history.csv`) don't support
concurrent-safe updates or real queries, and were already showing rot (one
history file was found 0 bytes mid-session, an empty-DataFrame crash
waiting to happen). Postgres is the "correct" enterprise answer but adds
infra the project doesn't need to demonstrate the pattern.

**Chosen:** SQLite. Real SQL, real schema (`db/schema.sql`), zero
infrastructure to stand up, good enough for a single-process daily batch
job. `db/connection.py`, `db/catalog_repo.py`, `db/competitor_repo.py` are
the only things that know it's SQLite — swapping to Postgres later is a
repo-layer change, not a rewrite.

---

## Human approval channel: Slack (Block Kit) with CLI fallback
**Decided:** 2026-08-19 (brainstorming phase)

Considered: CLI-only (simplest, but doesn't demonstrate a real HITL
integration pattern) vs. Slack (free-tier viable, demonstrates
webhook + signature verification + interactive approval — a real enterprise
pattern) vs. a custom web dashboard (Streamlit etc. — explicitly ruled out,
adds a frontend the project doesn't need).

**Chosen:** Slack Block Kit buttons via FastAPI webhook
(`slack_webhook.py`, `agents/slack_approval.py`), with a CLI prompt as the
fallback when `SLACK_BOT_TOKEN` isn't set
(`agents/approval.py`'s `_ask_human` swap-point). Signature verification
(`slack_sdk.signature.SignatureVerifier`) is strict when
`SLACK_SIGNING_SECRET` is configured, skipped with a console warning
otherwise — matches the rest of the integration's dev-friendly optionality.

---

## Multi-SKU pricing: LP optimizer, not a second LLM call
**Decided:** 2026-08-19 (brainstorming + Task 8 build)

Single-SKU price decisions go through Gemini (recommendation + confidence +
reasoning). For categories with 2+ flagged SKUs at once, an LLM call per SKU
loses the cross-SKU tradeoff (e.g. a shared category budget, or one SKU's
price change affecting a sibling's competitiveness) — and an LLM call for
the *whole batch* can't guarantee a jointly-optimal, constraint-respecting
solution the way a solver can.

**Chosen:** `tools/optimizer.py` — PuLP-based LP, maximizes category
revenue subject to margin floor, max price change, category budget, and
stock constraints. No LLM in this path at all; confidence is a fixed 0.85
(deterministic decisions don't need a model's confidence score).

**Bug caught in review (Task 8):** the `lo > hi` fallback branch silently
favored the max-price-change bound over the margin floor, allowing a
recommended price to violate the margin guardrail. Fixed to favor margin
floor when the two bounds conflict; added a solver-status check
(`pulp.LpStatus`) so a non-Optimal solve raises instead of returning a
silently-wrong price.

---

## Tool registry: formal pattern, no real MCP server
**Decided:** 2026-08-19 (brainstorming phase)

Considered standing up a real MCP server for tool calls (closer to how a
production agent stack would look) vs. a lightweight in-process pattern
that captures the same audit/validation discipline without the
infrastructure.

**Chosen:** in-process `Tool` dataclass + `register()` + `call_tool()`
(`tools/registry.py`) — validates input/output via Pydantic and logs a
`tool_call` audit event (name, latency_ms, success) for every call. Real
MCP server explicitly scoped out ("multi-tool architecture pattern only,"
not "iteration 3" depth with a standing server or geofencing).

---

## LLM provider: Gemini, kept throughout
**Decided:** 2026-08-19 (brainstorming phase, reaffirmed after model churn)

Kept Gemini (`google-genai`) rather than switching providers — free tier
sufficient for a portfolio project's call volume, and native
schema-constrained JSON output fits the "Pydantic everywhere" design.

**Model string churn (same day):** `gemini-2.0-flash` → retired (404 on a
live call) → tried `gemini-2.5-flash` → also retired (404) → Google's own
404 error body named `gemini-3.6-flash` as current → set it, verified
working on a live end-to-end run, independently confirmed as a real model
(launched 2026-07-21) via web search. `config.py`'s `GEMINI_MODEL` comment
documents the churn so the next retirement doesn't require rediscovering
this.

---

## Scope: everything in one pass, not phased
**Decided:** 2026-08-19 (brainstorming phase)

Given the choice between a phased rollout and building the full design in
one implementation pass, chose one pass — executed as a 15-task plan via
`superpowers:subagent-driven-development` (fresh subagent per task, task-
level review + fix loop, then one final whole-branch review + single fix
wave, then merged to `master`).


## 2026-09-30: Gemini replaced by a local model; backtest added

**Why.** Free-tier Gemini quotas ran out in a single day of testing, and
Google retired model IDs twice (see above). A daily batch job should not
depend on either. The only LLM call is the single-SKU decision; a local
Qwen3-4B through Ollama with JSON-schema-constrained output (`format`) has
no quota, costs $0 and keeps pricing data on the machine.
`LLM_PROVIDER=openai_compat` swaps in OpenRouter or NIM without code changes.

**What the backtest changed.**
- The first elasticity estimator was biased toward zero (stock-capped
  days, and a through-origin fit on SKUs that sit persistently off
  market). The LP optimizer then under-priced the risk of raising prices,
  and the pipeline lost money (-0.56% vs static). Per-SKU fits with
  capped days excluded cut the error from 0.66 to 0.17 and turned the
  result to +2.48%.
- A fixed optimizer confidence of 0.85 sent every recommendation to a
  human (228 a day). The auto-approval policy is now explicit: changes of
  5% or less that are guardrail-clean and reversible auto-execute, and
  anything else is escalated (about 11 a day).
- Tools registered but unused (scraper, matcher) are now the live
  ingestion path, measured against ground truth.
