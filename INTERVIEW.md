# Interview prep: Enterprise Pricing Intelligence Agent

Questions you should be able to answer cold about this project, grouped by
theme, plus what to study to answer them well. Pair with [[DECISIONS.md]]
(the "why") and [[FLOW.md]] (the "how") — most answers live there already.

## Architecture / system design

- Walk me through what happens when the pipeline runs, start to finish.
  *(→ FLOW.md, top to bottom)*
- Why a sequential pipeline instead of an event-driven / queue-based
  multi-agent system? What would change if this needed to scale to
  thousands of SKUs or run continuously instead of daily-batch?
- Where are the trust boundaries in this system, and what validates them?
  *(Pydantic models at every agent/tool boundary — models.py)*
- What's a "tool registry" here, and why build one instead of just calling
  functions directly? What would change with a real MCP server?
- Why SQLite instead of Postgres? What breaks first if this had to support
  concurrent writers?
- If you had to add a second data source (e.g. a real scraper) tomorrow,
  what's the blast radius of that change? *(→ price_source.py's swap point,
  discussed in README "Running it")*

**Study:** multi-agent orchestration patterns (sequential vs. graph vs.
event-driven), MCP (Model Context Protocol) basics — what it standardizes
and why a lightweight in-process registry is a smaller bet, SQLite vs.
Postgres tradeoffs (concurrency, WAL mode, when SQLite stops being enough).

## The SQL/RAG reversal (your best "judgment" story)

- Tell me about a design decision you reversed. Why did you build it the
  first way, what made you catch the problem, and what did you replace it
  with?
- The Researcher agent used to call an LLM to generate SQL. Why is that
  risky, concretely — not "LLMs can be wrong" in the abstract, but for
  *this* query?
- How do you tell the difference between "this needs an LLM because the
  input space is genuinely open-ended" and "this looks like it needs an LLM
  but doesn't"? *(min_competitor_price)*
- Where does the codebase still use fuzzy/LLM-driven judgment for
  retrieval, and why is that case different? *(semantic_matcher.py — SKU
  names aren't structured, competitor listing text is)*
- What would you have needed to see to be convinced NOT to revert this?

**Study:** be ready to state the actual mechanism plainly — "an LLM call
with a fixed input→output shape and no real decision in it is pure risk: it
can hallucinate, it costs latency and money, and it's a single point of
failure, for exactly zero judgment exercised." Also read up on prompt
injection risk in generated-SQL patterns generally (why "the SKU list is
internally generated, so it's safe" is exactly the kind of assumption that
stops being true later — see the parameterized-query decision).

## Reliability / production-readiness

- What happens when the Gemini API call fails or times out?
  *(retry → fallback_factory rule-based recommendation — llm_client.py)*
- What stops a single bad run from blowing through your API budget?
  *(cost/budget guard in main.py, raises RuntimeError before overspending)*
- Why memoize prompts within a run instead of across runs?
- What's your hallucination detection strategy, and what's wrong with the
  naive version of it? *(lexical "did it say the word trend" → anchored to
  real avg_competitor_trend_pct instead — DECISIONS.md)*
- How would you know, from the audit log alone, that the system is
  behaving correctly in production? What can't you currently answer from
  it? *(→ PROJECT.md "Known limitations" — no fallback_used flag, no
  latency_ms consumer yet — be honest about these, don't paper over them)*

**Study:** retry/backoff strategies, circuit breakers vs. simple retry
budgets, what "LLM-as-judge" eval means and why this project deliberately
avoids it in favor of deterministic metrics computed from structured data
already on hand.

## Optimization / the LP solver

- Why route multi-SKU categories through a linear program instead of just
  asking the LLM to price the whole category at once?
- What are the actual constraints in the LP, and why each one? (margin
  floor, max price change, category budget, stock)
- Walk through the margin-floor-vs-max-change-bound conflict bug: what was
  wrong, why did it matter, how did you catch it, how did you prove the fix
  was right? *(DECISIONS.md — inelastic-demand test masked the bug, fixed
  by using elastic demand so the constraint actually binds)*
- Why check `pulp.LpStatus` instead of trusting the solver always returns
  Optimal?

**Study:** basic LP formulation (objective, constraints, feasible region),
what "the solver reports Optimal but the answer is still wrong" usually
means in practice (constraint bugs, not solver bugs), price elasticity
basics (why an inelastic-demand test can't distinguish "budget constraint
enforced" from "budget constraint irrelevant").

## Human-in-the-loop / approval gate

- Why auto-approve anything at all instead of always requiring a human?
  What are the auto-approve conditions? *(confidence threshold + no
  guardrail violation + reversible)*
- Why Slack over a custom dashboard? What did you explicitly rule out and
  why? *(Streamlit/frontend — out of scope by design, see DECISIONS.md)*
- Walk through what verifies a Slack button click is really from Slack.
  *(SignatureVerifier, conditional on SLACK_SIGNING_SECRET — and why that
  conditionality is a deliberate, reviewed tradeoff, not a gap)*
- What happens if Slack isn't configured at all? *(CLI fallback — the
  actual swap-point line in approval.py)*

**Study:** webhook signature verification generally (HMAC, timing-safe
compare, replay protection via timestamp), what "human-in-the-loop" means
as a system design pattern vs. as a UX feature.

## Testing / process

- How was this actually built — one big session, or incrementally? What
  caught the bugs that made it into DECISIONS.md?
- Give an example of a bug that was in your own design/plan, not just
  implementation. How did you find it, and how did you know the fix was
  right rather than just "different"? *(optimizer margin-floor bug — two
  fix rounds, second one caught because the first "fix" used a scenario
  where the constraint being tested never actually bound)*
- What's your test coverage strategy — what's unit-tested vs. what would
  need a live run to actually verify? *(tests/ directory — one file per
  module; live-run verification called out explicitly for the Gemini model
  string and the SQL/RAG removal)*
- Tell me about a time you were told you were wrong about something you'd
  claimed was done, and how you responded. *(be honest: verify, don't
  defend — re-read the actual files/tests instead of re-asserting the
  summary, and it turned out two of three concerns were real gaps)*

**Study:** TDD basics if rusty, the difference between "test passes" and
"test actually exercises the constraint/behavior you think it does" (the
optimizer bug is your concrete example of this).

## Rapid-fire technical facts to have ready

- Storage: SQLite (`db/schema.sql`, `catalog` + `competitor_prices` tables)
- LLM: Google Gemini (`google-genai`), currently `gemini-3.6-flash`
- Optimization: PuLP (LP), constraints = margin floor, max price change,
  category budget, stock
- Approval: Slack Block Kit + FastAPI webhook, CLI fallback
- Fuzzy matching: rapidfuzz, in `tools/semantic_matcher.py`
- Scheduling: APScheduler, daily job in `scheduler.py`
- Audit: append-only JSONL, `audit/agent_audit.jsonl`, metrics computed
  post-run in `audit/metrics.py`
- Validation: Pydantic v2 everywhere a boundary is crossed
- No LLM calls in: Researcher (parameterized SQL), Analyst (guardrail
  math), multi-SKU Decision path (LP optimizer)
- LLM call only in: single-SKU Decision path

## Questions to ask them (turn it around)

- How do you currently decide what needs a human in the loop vs. what
  auto-executes, and has that threshold ever burned you?
- Do you have an equivalent of "caught an LLM call that wasn't earning its
  keep" story on your stack — how do you audit for that?
- What does your eval/observability story look like for agent pipelines —
  deterministic metrics from logs, or LLM-as-judge, or both?
