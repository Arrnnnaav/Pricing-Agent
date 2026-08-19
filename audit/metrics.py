"""Computes lightweight, deterministic eval metrics from one run's
audit log entries -- no separate LLM-judge call, since this is a batch
job, not a chat product, and the audit log already has everything
needed. See design spec section 7 for the metric definitions."""

from audit.logger import read_run

_HALLUCINATION_SAFE_WORDS = ("trend", "elasticity")


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
    # AND its reasoning lacks a safe word. Reasoning alone (the old
    # logic) flagged every optimizer-derived recommendation, since their
    # templated reasoning never mentions "trend"/"elasticity" -- but
    # LP-derived prices are the least likely to be hallucinated.
    hallucination_flags = []
    for rec in all_recs:
        reasoning = rec.get("reasoning", "").lower()
        has_safe_word = any(word in reasoning for word in _HALLUCINATION_SAFE_WORDS)

        min_price = rec.get("min_competitor_price")
        max_price = rec.get("max_competitor_price")
        price = rec.get("recommended_price")
        if min_price is None or max_price is None or price is None:
            # No competitor price-range data available -- can't evaluate
            # the price-range condition, so don't flag on that basis alone.
            out_of_range = False
        else:
            out_of_range = price < min_price or price > max_price

        if out_of_range and not has_safe_word:
            hallucination_flags.append(rec.get("sku"))

    return {
        "schema_success_rate": schema_success_rate,
        "hallucination_flags": hallucination_flags,
        "tool_success_rate": tool_success_rate,
    }
