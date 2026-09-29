# Flow

How execution actually travels between files/functions, right now. Update
this whenever the call graph changes — not just at milestone ends.

## You are here

```
[BUILT] Researcher --parameterized SQL--> Analyst --guardrail math--> Decision --LLM or LP--> Approval --Slack/CLI--> Execution --audit log--> Metrics
[NOT BUILT] real competitor scraper (mock generator stands in), MCP server, dashboard/frontend, semantic_matcher/scraper wired into live pipeline
```

Everything below `main.py:run_pipeline` is built and tested. Nothing in this
repo talks to a real external competitor site — `data/mock_price_generator.py`
is the entire "external world" today.

## Entry point

`main.py: run_pipeline(...)`

1. `db.connection.get_connection(config.DB_PATH)` → `init_db(conn)` if needed
2. Cost/budget guard checked up front — if tripped, raises `RuntimeError`,
   caught in `main.py`, `conn.close()` called, run aborts early (fixed leak,
   see [[DECISIONS.md]] tech-debt notes weren't needed here — this one's
   already patched)
3. `agents.researcher.run(conn, skus)` — see below
4. `agents.analyst.run(...)` — see below
5. `agents.decision` — per-category branch (single-SKU vs multi-SKU) — see below
6. Enrichment loop (`main.py` ~line 68-80): copies
   `min_competitor_price` / `max_competitor_price` / `avg_competitor_trend_pct`
   from each `Analysis` onto its matching `PriceRecommendation` dict before
   it's handed to Approval/audit — this is what lets `audit/metrics.py`
   anchor the hallucination check to real numbers later
7. `agents.approval.route_recommendation(rec)` per recommendation — see below
8. Execution: on approval, `db.catalog_repo.update_price(conn, sku, new_price)`
9. `audit.logger.log_event(...)` fires at every stage boundary above
   (append-only JSONL, `audit/agent_audit.jsonl`)
10. End of run: `audit.metrics.compute_run_metrics(run_id)` reads that run's
    own audit events back out and computes eval metrics

## Researcher (`agents/researcher.py`)

`run(conn, skus)`
→ `data.price_source.fetch_batch(conn, skus)`
  → one parameterized SQL query (`SELECT * FROM competitor_prices WHERE sku IN (?,?,...)`)
    covering every SKU in the batch — not one query per SKU, not an LLM call
  → returns `dict[str, list[CompetitorPriceHistory]]`
→ wraps result into `ResearchResult` (Pydantic), logs `research_complete` audit event

No LLM in this stage. `data.price_source.assert_readonly_sql(sql)` exists as
future-proofing insurance (rejects anything but a single SELECT) but has
nothing calling it right now since there's no LLM-generated SQL left to
guard — kept because the guard itself is cheap and someone re-adding dynamic
SQL later will want it back immediately.

## Analyst (`agents/analyst.py`)

`run(catalog_df, research_result)`
→ for each SKU: pure guardrail math (no LLM) — computes price gap vs.
  competitor average, `min_competitor_price`, `max_competitor_price`,
  `avg_competitor_trend_pct`
→ flags SKUs whose gap crosses `config`'s threshold as needing a decision
→ returns `AnalysisResult` (Pydantic), one `Analysis` per SKU (flagged or not)

## Decision (`agents/decision.py`)

`agents.decision.group_flagged_by_category(...)` splits flagged SKUs by
category, then per category:

- **1 SKU in category** → `agents.llm_client.generate_structured(...)`
  (LLM call, local Ollama by default) → `PriceRecommendation` with real confidence + reasoning.
  Retries on schema-validation failure; falls back to a rule-based
  recommendation (`fallback_factory`) after retries exhausted. Cache-checked
  first (`RunCostTracker.get_cached`/`put_cached`) to avoid re-paying for an
  identical prompt within a run.
- **2+ SKUs in category** → `decide_for_category(skus, catalog_by_sku, analyses_by_sku, run_id)`
  → builds `OptimizerInput` → `tools.registry.call_tool("optimizer", ..., run_id=run_id)`
  → `tools/optimizer.py: optimize_category_prices()` (PuLP LP solve) →
  `OptimizerOutput` → one `PriceRecommendation` per SKU, confidence hardcoded
  0.85, reasoning templated ("LP-optimized category price..."). **No LLM
  call anywhere in this branch.** Per-SKU work is wrapped in its own
  try/except so one SKU's construction failure doesn't discard the rest of
  the category's recommendations.

Both branches log through `tools.registry.call_tool`, which validates
input/output against the tool's registered Pydantic schemas and logs a
`tool_call` audit event (name, `latency_ms`, `success`) regardless of which
branch called it.

## Approval (`agents/approval.py`)

`route_recommendation(rec)`
→ if `rec.confidence >= config.AUTO_APPROVE_THRESHOLD` and no guardrail
  violation and `rec.reversible` → auto-execute, no human involved
→ else → `_ask_human(rec)`, swapped at import time:
  `_ask_human = _ask_human_cli if not config.SLACK_BOT_TOKEN else _ask_human_slack`
  - CLI path: blocking `input()` prompt
  - Slack path (`agents/slack_approval.py: ask_slack(rec)`): posts Block Kit
    message to `config.SLACK_APPROVAL_CHANNEL`, stores the pending decision
    in `PENDING[sku]`, blocks until `slack_webhook.py`'s
    `/slack/interact` FastAPI endpoint receives the button click and
    resolves it. Signature verified via `slack_sdk.signature.SignatureVerifier`
    when `SLACK_SIGNING_SECRET` is set.
→ returns `ExecutionResult`

## Audit + metrics (`audit/logger.py`, `audit/metrics.py`)

Every stage above calls `audit.logger.log_event(run_id, stage, event_type, payload)`
→ appends one JSON line to `audit/agent_audit.jsonl`. Nothing reads this file
during the run itself — it's write-only until the run ends.

At the end of a run, `audit.metrics.compute_run_metrics(run_id)` re-reads
that run's own events and computes, purely from logged data (no separate
LLM-judge call):
- `schema_success_rate` — real `generate_successes / generate_attempts`
  ratio from the `llm_stats` event (`None` if zero attempts, e.g. an
  all-multi-SKU run with no single-SKU LLM calls)
- `hallucination_flags` — SKUs where `recommended_price` fell outside
  `[min_competitor_price, max_competitor_price]` **and**
  `abs(avg_competitor_trend_pct) < 0.01` (see [[DECISIONS.md]])
- `tool_success_rate` — ratio of `tool_call` events with `success: true`

## Registered-but-unused tools

`tools/scraper_tool.py` and `tools/semantic_matcher.py` are registered in
`tools/registry.py` (`register_all_tools()`) and fully tested
(`tests/test_semantic_matcher.py`), but nothing in `main.py`'s live pipeline
calls them today. They exist to demonstrate the registry pattern holding
more than one tool. `semantic_matcher` is the intended fuzzy-match point if
a real (non-SKU-keyed) competitor feed is ever plugged into Researcher —
see the SQL/RAG reversal note in [[DECISIONS.md]].
