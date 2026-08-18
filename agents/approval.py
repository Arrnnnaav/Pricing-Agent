"""
Approval agent / gate: the human-in-the-loop checkpoint. A
recommendation with confidence >= AUTO_APPROVE_CONFIDENCE_THRESHOLD (and
no guardrail violation, and marked reversible) auto-executes. Everything
else goes to a CLI prompt today -- built so main.py doesn't need to
change when we swap this for a real Slack integration later; only this
file's "ask a human" step changes.
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
_ask_human = _ask_human_cli if not config.SLACK_BOT_TOKEN else _ask_human_slack


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
    recommendations: list[PriceRecommendation], catalog: pd.DataFrame
) -> list[ExecutionResult]:
    """Routes every recommendation through the approval gate and
    executes the approved ones against the catalog DataFrame in place.
    Caller (main.py) is responsible for persisting the updated catalog.
    """
    results = []
    for rec in recommendations:
        outcome = route_recommendation(rec)
        result = execute_recommendation(rec, outcome, catalog)
        results.append(result)

        status = "EXECUTED" if result.executed else "SKIPPED"
        print(f"[{status}] {rec.sku}: {outcome.value}")

    return results
