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

## Architecture

```
Researcher agent --parameterized SQL--> SQLite (catalog, competitor_prices)
      |
      v
Analyst agent --guardrail math--> flagged SKUs
      |
      v
Decision agent --Gemini (single-SKU) or LP optimizer tool (multi-SKU category)--> PriceRecommendation
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

**Decision** asks Gemini for a price recommendation (with confidence +
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

Python · Google Gemini (`google-genai`) · SQLite · Pydantic (structured
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
GEMINI_API_KEY=your-key-here
# Optional — omit to use the CLI approval gate instead of Slack
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
SLACK_APPROVAL_CHANNEL=#pricing-approvals
```

Get a free Gemini key at https://aistudio.google.com/apikey.

## Running it

```bash
# Generate a synthetic catalog + competitor price history (SQLite)
python data/generate_catalog.py
python data/mock_price_generator.py

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
agents/           Researcher, Analyst, Decision, Approval agents; Gemini client wrapper
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

Full design history lives in
`docs/superpowers/specs/2026-08-19-enterprise-pricing-agent-design.md` —
including a documented reversal worth reading: an earlier version had
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

- Synthetic data only — no real competitor scraping.
- `semantic_matcher` and `scraper` tools are registered in the tool
  registry but not called from the live pipeline (the optimizer is);
  they demonstrate the registry pattern rather than being load-bearing
  today.
- LP optimizer's elasticity coefficients and category budget are
  simplified, config-driven assumptions — real elasticity estimation is
  out of scope (see design doc).
- Slack webhook signature verification is skipped (with a warning) when
  `SLACK_SIGNING_SECRET` isn't configured, matching the rest of the
  Slack integration's dev-friendly optionality; it's enforced whenever
  the secret is set.
