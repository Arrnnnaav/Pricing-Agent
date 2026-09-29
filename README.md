# Enterprise Pricing Intelligence Agent

A daily-scheduled, multi-agent pipeline that monitors competitor prices
against an internal catalog, recommends (or auto-executes) price changes
within margin/business guardrails, routes low-confidence or risky
changes through a human approval gate, and keeps a full audit trail of
every decision.

Built as a portfolio project demonstrating enterprise agent patterns:
multi-agent pipeline, structured LLM output, human-in-the-loop approval,
cost/step guardrails, a formal tool registry with audit logging, and
deterministic eval metrics computed from the audit trail.

## Results (500 SKUs, synthetic market)

All numbers come from committed scripts, with outputs in `simulation/results/`. The catalog, competitor prices and demand are synthetic (seeded Faker + numpy distributions), so these are controlled comparisons between strategies on the same simulated market, not real revenue.

**30-day backtest** (`python -m simulation.backtest`). Every strategy re-prices daily against the same competitor price paths. Demand comes from a hidden model: per-SKU elasticity is the category mean plus noise. The pipeline never sees the true values; it only gets per-category elasticities estimated from 14 days of noisy warmup sales.

| Strategy | Gross profit vs static | Share of oracle uplift | Avg margin | Guardrail violations | Price changes |
|---|---|---|---|---|---|
| static (never reprice) | 0.00% | 0% | 35.5% | 0 | 0 |
| match competitor average | +1.34% | 29% | 36.8% | 0 | 14,975 |
| **pipeline** (Analyst → LP optimizer → guardrails → approval) | **+2.48%** | **54%** | **38.7%** | **0** | **3,524** |
| oracle (true per-SKU elasticity, upper bound) | +4.58% | 100% | 40.2% | 0 | 14,964 |

- **Profit:** the pipeline earns 1.9× the uplift of matching the market, while making 76% fewer price changes.
- **Routing over 30 days:**
  - 6,989 recommendations produced.
  - 95.1% were auto-executed: guardrail-clean, reversible changes of 5% or less.
  - 343 were escalated to a human, about 11 a day.
  - 21 were blocked by guardrails.
- **Elasticity estimation:** per-SKU OLS fits, stock-capped days excluded, median per category. Mean absolute error vs the true category means is 0.17. The first version had error 0.66 and made the pipeline lose money (-0.56%); fixing the estimator's bias is what turned the result around.

**Real pipeline run** (`python main.py`, 500 SKUs, `APPROVAL_MODE=queue`):
- 145 SKUs were flagged and 145 valid recommendations came from the LP optimizer.
- 44 were auto-executed and 101 were written to `pending_approvals`, so the run never blocked on a human.
- Tool success rate was 1.0.
- The first live run found two bugs, both now covered by regression tests:
  - The optimizer's +25% candidate price rounded to 0.25000000000000006 and was rejected by the max-change guardrail. Bounds are now stepped inward until they pass the same check the guardrail uses.
  - A zero-stock SKU was priced up because every price earns the same zero revenue in the demand model. The optimizer now applies the low-stock rule itself.
- The same run showed that the LLM "hallucination" metric was flagging optimizer prices, which sit above the market on purpose. Recommendations now carry a `source`, and the metric judges only LLM output.

**Competitor listing → SKU matching** (`python -m data.ingest`). Scraped listings carry only the competitor's free-text name (reordered words, typos, marketing text, a dropped tier word), and 10% are decoys for products we don't sell. Matching first narrows candidates by brand, then scores with `token_set_ratio` plus a model-number bonus.

| Distinct listings | Precision | Recall | Decoys wrongly matched | Ingest time |
|---|---|---|---|---|
| 2,200 (27,734 daily price rows) | 99.95% | 99.0% | 0 / 200 | 2.7 s |

LLM_COMPARE_RESULTS

## Architecture

```
Researcher agent --parameterized SQL--> SQLite (catalog, competitor_prices)
      |
      v
Analyst agent --guardrail math--> flagged SKUs
      |
      v
Decision agent --LLM (single-SKU) or LP optimizer tool (multi-SKU category)--> PriceRecommendation
      |
      v
Approval gate --Slack Block Kit, falls back to CLI--> ExecutionResult
      |
      v
Audit log (JSONL) + SQLite catalog updated
```

**Researcher** pulls competitor price history for every SKU in one
parameterized SQL query (no LLM in this stage — see
[Design notes](#design-notes) for why).

**Analyst** computes each SKU's price gap vs. the competitive average
and flags SKUs whose gap is meaningful enough to be worth a pricing
decision, using pure guardrail math (no LLM call).

**Decision** asks the LLM (local Ollama by default) for a price recommendation (with confidence +
reasoning) for single-SKU categories. When 2+ flagged SKUs share a
category, it instead routes them through an LP optimizer
(`tools/optimizer.py`, via PuLP) that jointly maximizes category revenue
subject to margin floor, max price change, a category budget, and stock
constraints — no LLM call in that path either.

**Approval gate** auto-executes recommendations at or above a confidence
threshold (with no guardrail violation and marked reversible); everything
else goes to a human via Slack (Block Kit buttons + a FastAPI webhook) or
a CLI prompt if Slack isn't configured.

**Audit log** — every stage writes to `audit/agent_audit.jsonl`
(append-only JSONL). At the end of each run, `audit/metrics.py` computes
eval metrics from that run's own audit events: LLM schema-success rate,
a hallucination heuristic anchored to real competitor-price and trend
data (not the model's own explanation of itself), and tool-call success
rate.

## Tech stack

Python · Ollama (local Qwen3-4B; OpenRouter/NIM optional) · SQLite · Pydantic (structured
I/O + validation everywhere an agent boundary crosses) · PuLP (LP
optimizer) · rapidfuzz (fuzzy competitor-listing → SKU matching) ·
Slack SDK + FastAPI (approval gate) · APScheduler (daily trigger) ·
pytest

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root (gitignored):

```
# LLM: local by default, no key needed (ollama pull qwen3:4b-instruct)
LLM_PROVIDER=ollama
LLM_MODELS=qwen3:4b-instruct
# or any OpenAI-compatible gateway:
# LLM_PROVIDER=openai_compat
# OPENAI_COMPAT_BASE_URL=https://integrate.api.nvidia.com/v1
# OPENAI_COMPAT_API_KEY=...
# APPROVAL_MODE=queue (default) | cli | slack
# Optional — Slack approval buttons
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
SLACK_APPROVAL_CHANNEL=#pricing-approvals
```

Only the single-SKU decision path calls an LLM; multi-SKU categories go through the LP optimizer.

## Running it

```bash
# Generate a synthetic catalog + competitor price history (SQLite)
python data/generate_catalog.py
python -m data.ingest            # scraper -> fuzzy matcher -> competitor_prices

# Backtest strategies on a simulated market (writes data/elasticity.json)
python -m simulation.backtest
python -m simulation.llm_compare  # LLM-only vs pipeline on a sample (slow: local LLM)

# Run one pipeline pass
python main.py

# Or run on a daily schedule (also runs once immediately on startup)
python scheduler.py
```

Data is synthetic (`data/generate_catalog.py`, seeded, reproducible) —
this project doesn't scrape real competitor sites, for the obvious ToS
reasons. `get_competitor_prices`'s replaceable-source design means a
real scraper/API integration is a swap of `data/mock_price_generator.py`
alone; nothing downstream would need to change.

### Slack approval gate (optional)

Without `SLACK_BOT_TOKEN` set, approvals prompt on the CLI. To use Slack:

1. Create a Slack app in a free workspace, add a bot token with
   `chat:write` scope, enable Interactivity pointed at
   `<your-url>/slack/interact`.
2. Run the webhook receiver: `uvicorn slack_webhook:app --port 3000`
3. Expose it locally with `ngrok http 3000` and point Slack's
   Interactivity Request URL at the ngrok URL.
4. Set `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` / `SLACK_APPROVAL_CHANNEL`
   in `.env` and run `python main.py`.

### Tests

```bash
pytest tests/ -v
```

## Project structure

```
agents/           Researcher, Analyst, Decision, Approval agents; provider-agnostic LLM client
db/               SQLite schema + repos (catalog, competitor prices)
data/             Synthetic data generators + retrieval
tools/            Formal tool registry: LP optimizer, fuzzy matcher, scraper (registered; optimizer is the one the pipeline actually calls)
audit/            Append-only JSONL audit logger + eval metrics
models.py         Pydantic schemas for every agent boundary
config.py         Single source of truth for thresholds, guardrails, paths
main.py           Orchestrates one full pipeline run
scheduler.py      Wraps main.py in a daily APScheduler job
slack_webhook.py  FastAPI receiver for Slack approval button clicks
```

## Design notes

See [DECISIONS.md](DECISIONS.md) for the reasoning log and [FLOW.md](FLOW.md)
for the current call graph. Worth reading: a documented reversal — an earlier version had
the Researcher agent generate its SQL via an LLM call (a "SQL/RAG"
pattern). Implementation review found the query never actually varied in
shape between calls — every run needed identical data — so the LLM call
added hallucination risk, latency, cost, and a single point of failure
in the pipeline's first stage for no real judgment being exercised.
Replaced with a plain parameterized query. The genuine
retrieval-needs-judgment story in this codebase is
`tools/semantic_matcher.py` (fuzzy-matching a competitor's free-text
listing name to a SKU code — a case where deterministic matching
actually fails).

## Known limitations

- Synthetic data only: catalog, competitor feed and demand are seeded
  simulations. Backtest numbers compare strategies on the same simulated
  market; they are not real revenue. `tools/scraper_tool.py` is the swap
  point for a real feed.
- The demand model is linear in the price gap vs the competitor average,
  with no cross-SKU substitution or seasonality. Elasticity is estimated
  per category, not per SKU (the oracle's per-SKU knowledge is why it
  still does better).
- The backtest assumes escalated, guardrail-clean recommendations are
  approved by the reviewer.
- The LLM path only runs for categories with a single flagged SKU, which
  is rare at 500 SKUs; `simulation/llm_compare.py` measures it directly
  on a sample.
- Slack signature verification is conditional on `SLACK_SIGNING_SECRET`
  being set (warns and skips otherwise): a deliberate dev-friendly
  tradeoff.
- `latency_ms` and the LLM fallback counters are logged but there is no
  dashboard or alerting on them yet.
