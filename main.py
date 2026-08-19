"""
Orchestrates one full run of the pricing pipeline:

    Researcher -> Analyst -> Decision -> Approval gate -> Execution

Every stage's output is written to the audit log before moving to the
next stage, so a crash mid-run still leaves a complete record of
whatever happened up to that point -- you're never debugging a run with
no trail because it failed on step 3 of 4.

Run with:
    python main.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uuid

import config
from agents.researcher import run_researcher
from agents.analyst import run_analyst
from agents.decision import run_decision
from agents.approval import run_approval
from audit.logger import log_event
from db.connection import get_connection
from db.catalog_repo import get_catalog_df, update_price


def run_pipeline() -> dict:
    run_id = str(uuid.uuid4())[:8]
    print(f"=== Pricing pipeline run {run_id} ===\n")

    conn = get_connection(config.DB_PATH)
    catalog = get_catalog_df(conn)

    # --- Researcher ---
    print("[1/4] Researcher agent: gathering competitor prices...")
    research = run_researcher(catalog, run_id=run_id)
    log_event(run_id, "researcher", "batch_complete", research.model_dump(mode="json"))
    print(
        f"      {research.skus_found}/{research.skus_requested} SKUs found, "
        f"{len(research.errors)} errors\n"
    )

    # --- Analyst ---
    print("[2/4] Analyst agent: comparing against catalog and checking guardrails...")
    analysis = run_analyst(catalog, research)
    log_event(run_id, "analyst", "batch_complete", analysis.model_dump(mode="json"))
    print(
        f"      {analysis.flagged_count}/{len(analysis.analyses)} SKUs flagged for decision\n"
    )

    # --- Decision ---
    print("[3/4] Decision agent: requesting price recommendations from Gemini...")
    try:
        decisions = run_decision(catalog, analysis)
    except RuntimeError as e:
        # A cost/step guard tripping is a controlled stop, not a crash --
        # log it and end the run cleanly with whatever we have so far.
        log_event(run_id, "decision", "budget_exceeded", {"error": str(e)})
        print(f"      Run stopped: {e}")
        conn.close()
        return {"run_id": run_id, "status": "stopped_on_budget"}

    log_event(run_id, "decision", "batch_complete", decisions.model_dump(mode="json"))
    print(f"      {len(decisions.recommendations)} recommendations generated\n")

    # --- Approval + execution ---
    print("[4/4] Approval gate: routing recommendations...")
    results = run_approval(decisions.recommendations, catalog)
    for r in results:
        log_event(run_id, "approval", "execution_result", r.model_dump(mode="json"))

    # Persist any executed price changes to the database.
    for r in results:
        if r.executed:
            update_price(conn, r.sku, r.new_price)
    conn.close()

    executed_count = sum(1 for r in results if r.executed)
    print(
        f"\n=== Run {run_id} complete: {executed_count}/{len(results)} price changes executed ==="
    )

    log_event(
        run_id,
        "pipeline",
        "run_complete",
        {
            "skus_flagged": analysis.flagged_count,
            "recommendations": len(decisions.recommendations),
            "executed": executed_count,
        },
    )

    from audit.metrics import compute_run_metrics

    metrics = compute_run_metrics(run_id)
    log_event(run_id, "pipeline", "eval_metrics", metrics)
    print(f"      Eval metrics: {metrics}")

    return {
        "run_id": run_id,
        "status": "complete",
        "flagged": analysis.flagged_count,
        "recommendations": len(decisions.recommendations),
        "executed": executed_count,
    }


if __name__ == "__main__":
    run_pipeline()
