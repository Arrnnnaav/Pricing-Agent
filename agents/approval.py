"""
Approval agent / gate: the human-in-the-loop checkpoint. A
recommendation auto-executes if its confidence >=
AUTO_APPROVE_CONFIDENCE_THRESHOLD AND it has no guardrail violation AND
it's marked reversible. Everything else routes through whichever "ask a
human" implementation is active: _ask_human_slack (real Slack
Approve/Reject buttons, via agents/slack_approval.py) if
config.SLACK_BOT_TOKEN is set, otherwise _ask_human_cli (a terminal
input() prompt) as the fallback for local/offline runs. main.py doesn't
need to know or care which one is active -- both implement the same
"return True for approved, False for rejected" contract behind the
module-level `_ask_human` name.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from models import (
    PriceRecommendation,
    GuardrailViolation,
    ApprovalOutcome,
    ExecutionResult,
)


def _ask_human_cli(rec: PriceRecommendation) -> bool:
    """CLI stand-in for the Slack approve/reject buttons. Swap point:
    a future slack_approval.py would implement this same "return True
    for approved, False for rejected" contract using a real button
    click instead of terminal input().
    """
    print(f"\n--- Approval needed: {rec.sku} ---")
    print(
        f"  ${rec.current_price:.2f} -> ${rec.recommended_price:.2f} "
        f"({(rec.recommended_price - rec.current_price) / rec.current_price:+.1%})"
    )
    print(f"  Confidence: {rec.confidence:.0%}")
    print(f"  Projected margin: {rec.projected_margin_pct:.1%}")
    print(f"  Reasoning: {rec.reasoning}")
    answer = input("  Approve? [y/n]: ").strip().lower()
    return answer == "y"


def _ask_human_slack(rec: PriceRecommendation) -> bool:
    from agents.slack_approval import ask_slack

    return ask_slack(rec)


# Swap point: set to _ask_human_slack to route through Slack instead of
# the CLI. Kept as a module-level name (not hardcoded inline) so tests
# can patch agents.approval._ask_human directly.
_ask_human = _ask_human_slack if config.APPROVAL_MODE == "slack" else _ask_human_cli

# Set by run_approval when APPROVAL_MODE=queue: escalations are written to
# pending_approvals instead of blocking the batch on a human.
_queue_conn = None
_queue_run_id = None


def _enqueue_for_review(rec: PriceRecommendation) -> None:
    from datetime import datetime, timezone

    _queue_conn.execute(
        """INSERT INTO pending_approvals (run_id, sku, current_price, recommended_price,
           confidence, guardrail_violation, reasoning, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_queue_run_id, rec.sku, rec.current_price, rec.recommended_price, rec.confidence,
         rec.guardrail_violation.value, rec.reasoning,
         datetime.now(timezone.utc).isoformat()),
    )  # fmt: skip


def route_recommendation(rec: PriceRecommendation) -> ApprovalOutcome:
    """Decides the approval outcome for one recommendation. A guardrail
    violation forces human review regardless of confidence -- auto-approval
    is only for clean, high-confidence, reversible recommendations.
    """
    can_auto_approve = (
        rec.confidence >= config.AUTO_APPROVE_CONFIDENCE_THRESHOLD
        and rec.guardrail_violation == GuardrailViolation.NONE
        and rec.reversible
    )

    if can_auto_approve:
        return ApprovalOutcome.AUTO_APPROVED

    if rec.guardrail_violation != GuardrailViolation.NONE:
        # A guardrail violation is never auto-executed, but we still let
        # a human explicitly override it via the CLI prompt -- guardrails
        # protect against silent automation, not against informed humans.
        print(
            f"\n[GUARDRAIL VIOLATION: {rec.guardrail_violation.value}] "
            f"for {rec.sku} -- requires explicit human approval."
        )

    if _queue_conn is not None:
        _enqueue_for_review(rec)
        return ApprovalOutcome.QUEUED_FOR_REVIEW

    approved = _ask_human(rec)
    return (
        ApprovalOutcome.HUMAN_APPROVED if approved else ApprovalOutcome.HUMAN_REJECTED
    )


def execute_recommendation(
    rec: PriceRecommendation, outcome: ApprovalOutcome, catalog: pd.DataFrame
) -> ExecutionResult:
    """Applies the price change to the in-memory catalog DataFrame if
    the outcome was any form of approval, and returns an ExecutionResult
    describing what happened either way (including rejections -- a
    rejected recommendation is still a real event worth recording)."""
    executed = outcome in (
        ApprovalOutcome.AUTO_APPROVED,
        ApprovalOutcome.HUMAN_APPROVED,
    )

    if executed:
        catalog.loc[catalog["sku"] == rec.sku, "our_price"] = rec.recommended_price

    return ExecutionResult(
        sku=rec.sku,
        outcome=outcome,
        old_price=rec.current_price,
        new_price=rec.recommended_price if executed else rec.current_price,
        executed=executed,
    )


def run_approval(
    recommendations: list[PriceRecommendation],
    catalog: pd.DataFrame,
    conn=None,
    run_id: str | None = None,
) -> list[ExecutionResult]:
    """Routes every recommendation through the approval gate and
    executes the approved ones against the catalog DataFrame in place.
    Caller (main.py) is responsible for persisting the updated catalog.
    """
    global _queue_conn, _queue_run_id
    queue = config.APPROVAL_MODE == "queue" and conn is not None
    _queue_conn, _queue_run_id = (conn, run_id) if queue else (None, None)
    results = []
    for rec in recommendations:
        outcome = route_recommendation(rec)
        result = execute_recommendation(rec, outcome, catalog)
        results.append(result)

        status = "EXECUTED" if result.executed else "SKIPPED"
        print(f"[{status}] {rec.sku}: {outcome.value}")

    if queue:
        conn.commit()
    _queue_conn = _queue_run_id = None
    return results
