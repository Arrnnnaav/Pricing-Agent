"""LLM-only pricing vs the pipeline, on a sample (LLM calls are the slow part).

Sample: N SKUs per category. Every strategy re-prices on the same days
(every REPRICE_EVERY days of the 30-day test period) against the same
simulated market as simulation/backtest.py.

  static         never change
  match_guarded  competitor average clipped to guardrails
  pipeline       Analyst -> LP optimizer (estimated elasticity) -> guardrails
  llm_only       production single-SKU LLM path (agents/decision.decide_for_sku,
                 local model via Ollama) for every SKU; a recommendation that
                 fails guardrails is not applied, and is counted

Run:  python -m simulation.llm_compare
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
from datetime import date

import numpy as np

import config
from agents.analyst import analyze_sku
from agents.decision import _catalog_item, decide_for_category, decide_for_sku
from agents.llm_client import RunCostTracker
from db.catalog_repo import get_catalog_df
from db.connection import get_connection
from models import GuardrailViolation
from simulation.backtest import OUT, TEST, WARMUP, guard_bounds, histories_for
from simulation.market import build_market
from tools.registry import register_all_tools

PER_CATEGORY, REPRICE_EVERY = 5, 6


def main(skip_llm: bool = False) -> dict:
    t0 = time.perf_counter()
    register_all_tools()
    config.MAX_STEPS_PER_RUN = 10_000  # evaluation run, not the daily job
    conn = get_connection(config.DB_PATH)
    full = get_catalog_df(conn)
    conn.close()
    mkt_full = build_market(full, WARMUP + TEST)
    sample = full.groupby("category").head(PER_CATEGORY).index.to_numpy()
    mkt = mkt_full
    cat = full.loc[sample].reset_index(drop=True)
    cost, p0 = cat["cost"].to_numpy(), cat["our_price"].to_numpy()
    start = date(2026, 1, 1)

    def units(prices, d):
        full_prices = mkt.catalog["our_price"].to_numpy().copy()
        full_prices[sample] = prices
        return mkt.expected_units(full_prices, d)[sample]

    strategies = ["static", "match_guarded", "pipeline"] + ([] if skip_llm else ["llm_only"])
    prices = {s: p0.copy() for s in strategies}
    tot = {s: {"profit": 0.0, "revenue": 0.0} for s in strategies}
    llm = {"calls": 0, "guardrail_rejected": 0, "failed": 0}
    tracker = RunCostTracker()
    for d in range(WARMUP, WARMUP + TEST):
        if (d - WARMUP) % REPRICE_EVERY == 0:
            m = mkt.market_price(d)[sample]
            prices["match_guarded"] = np.round(
                np.clip(m, *guard_bounds(cost, prices["match_guarded"])), 2
            )
            for key in [k for k in ("pipeline", "llm_only") if k in strategies]:
                rows = {
                    r["sku"]: r for _, r in cat.assign(our_price=prices[key]).iterrows()
                }
                analyses = {sku: analyze_sku(_catalog_item(rows[sku]), histories_for(mkt, int(sample[i]), d, start))
                            for i, sku in enumerate(cat["sku"])}  # fmt: skip
                idx = {s: i for i, s in enumerate(cat["sku"])}
                if key == "pipeline":
                    by_cat: dict[str, list[str]] = {}
                    for a in analyses.values():
                        if a.needs_decision:
                            by_cat.setdefault(rows[a.sku]["category"], []).append(a.sku)
                    for skus in by_cat.values():
                        if len(skus) < 2:
                            continue
                        for rec in decide_for_category(
                            skus, rows, analyses, run_id="llm_compare"
                        ):
                            if rec.guardrail_violation == GuardrailViolation.NONE:
                                prices[key][idx[rec.sku]] = rec.recommended_price
                else:
                    for sku, a in analyses.items():
                        if a.avg_competitor_price is None:
                            continue
                        llm["calls"] += 1
                        try:
                            rec = decide_for_sku(_catalog_item(rows[sku]), a, tracker)
                        except Exception:
                            llm["failed"] += (
                                1  # e.g. model price rejected by PriceRecommendation validator
                            )
                            continue
                        if rec.guardrail_violation != GuardrailViolation.NONE:
                            llm["guardrail_rejected"] += 1
                            continue
                        prices[key][idx[sku]] = rec.recommended_price
            print(f"day {d - WARMUP}: llm calls so far {llm['calls']}", flush=True)
        for s in strategies:
            u = units(prices[s], d)
            tot[s]["profit"] += float((prices[s] - cost) @ u)
            tot[s]["revenue"] += float(prices[s] @ u)

    base = tot["static"]["profit"]
    report = {
        "skus": len(cat), "reprice_days": len(range(0, TEST, REPRICE_EVERY)),
        "profit_vs_static_pct": {s: round(100 * (tot[s]["profit"] - base) / base, 2) for s in strategies},
        "margin_pct": {s: round(100 * tot[s]["profit"] / tot[s]["revenue"], 2) for s in strategies},
        "llm": {**llm, **tracker.stats()},
        "seconds": round(time.perf_counter() - t0, 1),
    }  # fmt: skip
    name = "llm_compare_no_llm.json" if skip_llm else "llm_compare.json"
    with open(os.path.join(OUT, name), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    print(json.dumps(main(skip_llm="--no-llm" in sys.argv), indent=2))
