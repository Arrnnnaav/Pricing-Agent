"""Computes lightweight, deterministic eval metrics from one run's
audit log entries -- no separate LLM-judge call, since this is a batch
job, not a chat product, and the audit log already has everything
needed. See design spec section 7 for the metric definitions."""

from audit.logger import read_run

# A recommendation citing "trend" as justification is only credible if
# the Analyst's own computed trend actually moved by more than this --
# below it, day-to-day noise in a 7-day window, not a real trend. Anchors
# the hallucination check to a number the model can't talk around,
# instead of trusting whether it used the word "trend" in its reasoning.
_TREND_NOISE_THRESHOLD_PCT = 0.01


def compute_run_metrics(run_id: str) -> dict:
    events = read_run(run_id)

    tool_events = [e for e in events if e["agent"] == "tool"]
    tool_success_rate = (
        sum(1 for e in tool_events if e["data"].get("success")) / len(tool_events)
        if tool_events
        else None
    )

    decision_events = [
        e for e in events if e["agent"] == "decision" and e["event"] == "batch_complete"
    ]
    all_recs = [
        rec for e in decision_events for rec in e["data"].get("recommendations", [])
    ]

    # schema_success_rate = successes / attempts of generate_structured()
    # calls, per design spec section 7 -- tracked directly on
    # RunCostTracker (agents/llm_client.py) and logged by run_decision
    # (agents/decision.py) as its own "llm_stats" event, since decision
    # batch output alone can't distinguish 10/10 successful LLM calls
    # from 1/10 (a fallback still produces output).
    llm_stats_events = [
        e for e in events if e["agent"] == "decision" and e["event"] == "llm_stats"
    ]
    generate_attempts = sum(
        e["data"].get("generate_attempts", 0) for e in llm_stats_events
    )
    generate_successes = sum(
        e["data"].get("generate_successes", 0) for e in llm_stats_events
    )
    schema_success_rate = (
        generate_successes / generate_attempts if generate_attempts else None
    )

    # A recommendation is only flagged as a possible hallucination if
    # BOTH: its price falls outside the SKU's competitor price range,
    # AND the Analyst's own computed trend doesn't actually justify going
    # outside that range (abs(avg_competitor_trend_pct) is below the
    # noise threshold). This is anchored to a real number from the
    # Analyst stage rather than whether the model's reasoning text
    # happens to contain the word "trend" -- a model can claim a trend
    # exists without one actually being in the data, and the old
    # lexical check couldn't catch that.
    hallucination_flags = []
    # Only LLM-sourced prices can hallucinate; optimizer prices are
    # deliberately off-market (profit optimum) and bounded by guardrails.
    for rec in [r for r in all_recs if r.get("source", "llm") == "llm"]:
        min_price = rec.get("min_competitor_price")
        max_price = rec.get("max_competitor_price")
        price = rec.get("recommended_price")
        if min_price is None or max_price is None or price is None:
            # No competitor price-range data available -- can't evaluate
            # the price-range condition, so don't flag on that basis alone.
            out_of_range = False
        else:
            out_of_range = price < min_price or price > max_price

        trend = rec.get("avg_competitor_trend_pct")
        trend_justifies_it = (
            trend is not None and abs(trend) >= _TREND_NOISE_THRESHOLD_PCT
        )

        if out_of_range and not trend_justifies_it:
            hallucination_flags.append(rec.get("sku"))

    return {
        "schema_success_rate": schema_success_rate,
        "hallucination_flags": hallucination_flags,
        "llm_recommendations": sum(1 for r in all_recs if r.get("source", "llm") == "llm"),
        "optimizer_recommendations": sum(1 for r in all_recs if r.get("source") == "optimizer"),
        "tool_success_rate": tool_success_rate,
    }
