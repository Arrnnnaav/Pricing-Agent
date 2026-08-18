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
    schema_success_rate = 1.0 if all_recs or not decision_events else 0.0

    hallucination_flags = []
    for rec in all_recs:
        reasoning = rec.get("reasoning", "").lower()
        if not any(word in reasoning for word in _HALLUCINATION_SAFE_WORDS):
            hallucination_flags.append(rec.get("sku"))

    return {
        "schema_success_rate": schema_success_rate,
        "hallucination_flags": hallucination_flags,
        "tool_success_rate": tool_success_rate,
    }
