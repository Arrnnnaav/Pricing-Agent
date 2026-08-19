# Enterprise Pricing Intelligence Agent — Design

## Statement

A daily-scheduled, multi-agent pipeline that monitors competitor prices
against an internal catalog, recommends (or auto-executes) price changes
within margin/business guardrails, routes low-confidence or risky changes
through a Slack approval gate, and keeps a full audit trail of every
decision. Built as a resume-grade portfolio project matching the
"Resume-Level Project" spec from the enterprise-agent doc, upgraded with
capstone-deck iteration-2/3 ideas (LP optimization, tool architecture,
eval metrics) layered onto iteration 1's SQL/RAG retrieval pattern.

Existing codebase (`agents/`, `data/`, `models.py`, `config.py`,
`main.py`, `scheduler.py`) already implements the sequential
Researcher→Analyst→Decision→Approval pipeline over flat CSV files with a
CLI approval gate and JSONL audit log — see baseline commit
`2b65812`. This spec describes what's added/changed on top of that
baseline. Nothing here needs to preserve the CSV data path; SQLite
replaces it outright.

## Decisions locked during brainstorming

- LLM provider: **Gemini** (unchanged from baseline) — not switching to Anthropic.
- Approval channel: **Slack** (free workspace + bot token, Block Kit buttons).
- Storage: **SQLite** replacing CSV, real SQL retrieval (not Postgres).
- Iteration 3 depth: **tool-registry pattern only** — no real MCP server,
  no geofencing (doesn't map to this domain).
- No Streamlit/dashboard frontend — CLI + Slack only.

## Architecture overview

```
Researcher agent --SQL/RAG--> SQLite (catalog, competitor_prices)
      |
      v
Analyst agent --guardrail math--> flagged SKUs
      |
      v
Decision agent --Gemini + tools(optimizer, semantic_matcher)--> PriceRecommendation
      |
      v
Approval gate --Slack Block Kit / auto-approve--> ExecutionResult
      |
      v
Audit log (JSONL) + SQLite catalog updated
```

Pipeline stays sequential (Analyst needs Researcher's output, Decision
needs Analyst's) — no event queue, no parallel agent execution. A queue
adds infra a daily batch job doesn't need and can't be justified in an
interview; the tool-registry pattern (section 4) gets the "multi-tool
architecture" resume language without it.

## 1. Data layer — SQLite

New module `db/schema.sql` + `db/connection.py`. Two tables:

```sql
CREATE TABLE catalog (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category TEXT NOT NULL,
    our_price REAL NOT NULL CHECK (our_price > 0),
    cost REAL NOT NULL CHECK (cost > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
);

CREATE TABLE competitor_prices (
    sku TEXT NOT NULL,
    competitor TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    in_stock INTEGER NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (sku, competitor, date),
    FOREIGN KEY (sku) REFERENCES catalog(sku)
);
```

`data/generate_catalog.py` and `data/mock_price_generator.py` keep their
generation logic, only the sink changes: `INSERT` into SQLite instead of
`to_csv`. `config.py` gains `DB_PATH` replacing `CATALOG_PATH` /
`PRICE_HISTORY_PATH`. `.gitignore` already excludes `*.db`/`*.sqlite3`.

## 2. Researcher agent — real SQL/RAG retrieval

Today `get_competitor_prices(sku)` does a direct pandas filter. New
version: a small LLM call turns a natural-language retrieval request
("competitor price history for SKU LAP-0001, last 7 days") into a SQL
`SELECT` against `competitor_prices`, constrained to read-only via a
fixed prompt + a guard that rejects any non-`SELECT` statement before
execution (never trust generated SQL to only read). Executed through
`sqlite3`, rows mapped back into the existing `CompetitorPriceHistory`
Pydantic model — **no schema change downstream**, so Analyst/Decision
are unaffected by this change.

This is deliberately one LLM call per *batch*, not per SKU (batches SKUs
into a single retrieval prompt covering the whole catalog) — matches the
existing cost-guard philosophy and avoids 100 extra Gemini calls per run.

## 3. Decision agent — LP optimizer tool

New `tools/optimizer.py` using PuLP. When the Analyst flags 2+ SKUs in
the same category, Decision agent calls the optimizer instead of pricing
each SKU independently:

```
maximize:    sum(revenue_i) for i in flagged_skus_in_category
subject to:  sum(discount_cost_i) <= category_budget
             price_bounds_i (guardrails.py MIN_MARGIN_PCT / MAX_PRICE_CHANGE_PCT)
             demand_i = f(price_i, elasticity_i)   # simple linear elasticity assumption
             demand_i <= stock_i
```

`category_budget` and elasticity coefficients are config-driven constants
(synthetic, documented as assumptions — real elasticity estimation is out
of scope). Optimizer output (a price per SKU) becomes the
`recommended_price` fed into the existing `PriceRecommendation`
construction path — the LLM still produces `confidence` and `reasoning`
by evaluating the optimizer's output, rather than the LLM inventing the
price itself for multi-SKU categories. Single flagged SKU in a category
still goes through the existing direct-LLM-recommendation path
unchanged.

## 4. Tool registry (iteration-3 pattern)

New `tools/` package: `tools/registry.py` defines a `Tool` wrapper
(name, input model, output model, callable) and a `call_tool(name,
input_dict)` entry point that validates input against the tool's Pydantic
input model, runs it, validates output against its output model, and
writes a `tool_call` audit event (tool name, input, output, latency_ms,
success/failure) — this is the "tool-call security auditing" story
without a real MCP server. Three tools registered: `scraper_tool` (wraps
existing mock price generation), `optimizer_tool` (section 3),
`semantic_matcher_tool` (fuzzy-matches a competitor's listed product name
to our SKU using `rapidfuzz` string similarity — handles the realistic
case where a competitor lists "Dell Vortex Pro 8GB" and we need to match
it to SKU `LAP-0001`, since competitor data won't always come pre-matched
to our SKU codes).

## 5. Slack approval gate

New `agents/slack_approval.py`, swaps in for the CLI `_ask_human_cli` in
`approval.py` behind the same `bool`-returning contract (only that one
function changes — `route_recommendation`/`execute_recommendation` stay
as-is). Posts a Block Kit message (SKU, price change, confidence,
margin, reasoning, Approve/Reject buttons) via `slack_sdk`. A small
FastAPI app (`slack_webhook.py`) receives the button interaction
callback and resolves a pending-approval future; `APPROVAL_TIMEOUT_SECONDS`
(existing config) still governs the timeout fallback to
`ApprovalOutcome.TIMED_OUT`. Requires `SLACK_BOT_TOKEN` +
`SLACK_SIGNING_SECRET` env vars (added to `.env`, already gitignored).

## 6. Reliability layer upgrades

- **Fallback chain**: `llm_client.generate_structured` gains a fallback
  — after `max_retries` Gemini failures, falls back to a pure
  rule-based recommendation (price = competitor average, confidence
  fixed at a low constant like 0.5, forcing human review) rather than
  failing the SKU outright. Logged distinctly in the audit trail
  (`"fallback_used": true`) so it's visible, not silent.
- **Prompt memoization**: `RunCostTracker` gains a same-run cache keyed
  on a hash of the prompt text — a re-run within the same run_id that
  produces an identical prompt (shouldn't normally happen, but protects
  against any accidental double-call) skips the Gemini call and reuses
  the cached `LLMPriceSuggestion`.
- **Audit log**: `log_event` gains optional `latency_ms` field; every
  call site (agents + tool registry) records wall-clock time around the
  operation it's logging.

## 7. Eval metrics

New `audit/metrics.py`, computed at end of each run from that run's
audit events (not a separate LLM judge — cheap, deterministic, since
this is a batch job not a chat product):

- **Schema validation success rate**: successful `generate_structured`
  calls / total attempts (task success proxy).
- **Hallucination heuristic**: recommendations where
  `recommended_price` falls outside `[min_competitor_price,
  max_competitor_price]` *and* the reasoning text doesn't mention
  "trend" or "elasticity" (the two legitimate reasons to go outside
  that range) — flagged as a potential hallucination for manual review.
- **Tool-call success rate**: from the tool registry's audit events.

Written as one `eval_summary` audit event per run — no new storage
needed, reuses the existing JSONL log.

## Testing

- `db/`, `tools/optimizer.py`, `tools/registry.py`, hallucination
  heuristic in `audit/metrics.py`: unit tests with plain data, no LLM/
  network calls — mirrors the existing `guardrails.py` testability
  pattern.
- Researcher's SQL-generation step: test the read-only guard (reject
  `DROP`/`UPDATE`/`INSERT` etc.) with fixed strings, no live LLM call
  needed for that part; a small number of live-Gemini smoke tests for
  the actual generation, run manually/optionally (not in CI) given API
  cost.
- Slack webhook: test `route_recommendation`'s contract with a fake
  Slack responder (same interface swap the CLI stub already permits),
  not against a real Slack workspace in automated tests.
- Full pipeline: manual `python main.py` run against the SQLite-backed
  synthetic data remains the end-to-end smoke test, same as today.

## Out of scope

- Real competitor scraping (ToS/legal risk) — synthetic data generation
  continues to stand in for the Researcher's data source.
- Real MCP protocol server/client.
- Geofencing.
- Streamlit/web dashboard.
- Real elasticity estimation (config-driven constants only).

## Revision — 2026-08-19: SQL/RAG reversed after implementation review

Section 2 above (as planned and as implemented) turned out to be wrong.
Left in place rather than edited, because the reasoning is more useful
than a silently-corrected diagram.

**What shipped first:** exactly section 2 — an LLM call per batch that
wrote a SQL `SELECT` against `competitor_prices`, validated by a
read-only guard, then executed. It worked: 50/50 SKUs retrieved
correctly against live data.

**What a second pass caught:** "worked" isn't the same question as
"needed." The prompt template
(`_SQL_PROMPT_TEMPLATE` in the original `data/price_source.py`) had
exactly one shape — `SELECT * FROM competitor_prices WHERE sku IN
(...)`, with only the SKU list changing between calls. Every run needs
identical data: the full 7-day window, every competitor, in-stock and
out-of-stock rows both (out-of-stock rows are what keep `trend_pct`
honest). There was no real fork in what the system needed the LLM to
decide — the "RAG" framing described a capability the implementation
never actually exercised.

That's a cost, not a feature, once named:

- **Hallucination risk** on a step with zero actual query-shape
  variance to justify carrying it.
- **Latency and per-call cost** for work a parameterized query does
  identically.
- **A new single point of failure** in the first pipeline stage
  everything downstream depends on — this was not hypothetical:
  `config.GEMINI_MODEL` going stale (`gemini-2.0-flash`, then
  `gemini-2.5-flash`, both retired by Google mid-project) took the
  Researcher stage down and zeroed the entire run, twice, before the
  model string was corrected. Removing the LLM call from Researcher
  removes that whole failure class from the one stage nothing else can
  route around.

**Option considered and rejected:** make the query shape genuinely vary
(e.g. let the LLM choose between a recent-snapshot query, a full
trend-window query, an in-stock-only query, based on what the Analyst
needs). Rejected because the Analyst's need doesn't actually vary run to
run — it always wants the full window. Branching the query on an LLM
decision that has no real input to respond to isn't restoring the RAG
justification, it's manufacturing a decision point to make one exist.
The honest fix was removing the call, not disguising it better.

**What shipped instead:** `data/price_source.py`'s `fetch_batch` is now
one parameterized query — `conn.execute("SELECT * FROM
competitor_prices WHERE sku IN (?,?,...)", skus)`. `assert_readonly_sql`
(the guard from section 2) stays in the file, unused today, as cheap
insurance for any future retrieval path that does need to vary its
query shape for a real reason.

**Where the genuine "retrieval needs judgment" story still lives in this
codebase:** `tools/semantic_matcher.py` — fuzzy-matching a competitor's
free-text listing name ("Dell Vortex Pro 8GB") to our SKU code
(`LAP-0001`) is a case where deterministic string equality actually
fails and judgment helps. That tool carries the RAG-flavored weight this
section originally assigned to Researcher.

**Also fixed in the same pass — the hallucination heuristic (section
7):** the shipped version required the recommendation's *reasoning
text* to contain the word "trend" or "elasticity" to avoid being
flagged — a lexical check a model can satisfy by writing the word
without a real trend existing in the data. Fixed to cross-reference the
Analyst's actual computed `avg_competitor_trend_pct` against a noise
threshold (`0.01`) instead of trusting the model's own explanation of
itself. Same principle as `guardrails.py`: check the number, not the
narrative.

**How this was caught:** not by the per-task implementation reviews —
each task's diff looked correct in isolation (the guard worked, the SQL
executed, 50/50 SKUs came back). It took a step back after a live
end-to-end run, prompted by direct questioning of *why* the LLM call was
there rather than *whether* it worked, to notice the query never
actually needed to vary. "Does this pass its tests" and "does this
design decision hold up" are different questions; this revision is what
it looks like when the second one gets asked after the first one already
said yes.
