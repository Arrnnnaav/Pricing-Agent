"""Backtest: pricing strategies on the same simulated market.

Timeline
  warmup (14 days): current prices with small random experiments (+-8%);
      noisy sales are recorded and per-category elasticity is estimated.
  test (30 days): each strategy re-prices daily against the same
      competitor paths; expected units come from the hidden demand model.

Strategies
  static            never change price (baseline)
  match_market      set price = competitor average, no guardrails
  match_guarded     competitor average clipped to the guardrails
  pipeline          production code: Analyst flags -> LP optimizer per
                    category (estimated elasticity, competitor-aware,
                    profit objective) -> guardrails -> approval routing
  oracle            per-SKU optimum using the TRUE elasticity (upper bound)

The pipeline path imports and runs the real agents/analyst.py,
agents/decision.py and agents/guardrails.py code.

Run:  python -m simulation.backtest
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

import config
from agents.analyst import analyze_sku
from agents.approval import route_recommendation
from agents.decision import decide_for_category, _catalog_item
from data.mock_price_generator import COMPETITORS
from db.catalog_repo import get_catalog_df
from db.connection import get_connection
from models import (
    ApprovalOutcome,
    CompetitorPriceHistory,
    CompetitorPricePoint,
    GuardrailViolation,
)
from simulation import elasticity as el
from simulation.market import TRUE_CATEGORY_ELASTICITY, build_market
from tools.registry import register_all_tools

WARMUP, TEST, WINDOW = 14, 30, 7
OUT = os.path.join(config.PROJECT_ROOT, "simulation", "results")


def guard_bounds(
    cost: np.ndarray, current: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    lo = np.maximum(
        current * (1 - config.MAX_PRICE_CHANGE_PCT), cost / (1 - config.MIN_MARGIN_PCT)
    )
    hi = current * (1 + config.MAX_PRICE_CHANGE_PCT)
    return lo, np.maximum(lo, hi)


def violations(cost, before, after) -> int:
    margin_bad = (after - cost) / after < config.MIN_MARGIN_PCT - 1e-9
    change_bad = np.abs(after - before) / before > config.MAX_PRICE_CHANGE_PCT + 1e-9
    return int(np.sum(margin_bad | change_bad))


def histories_for(mkt, i: int, day: int, start: date) -> list[CompetitorPriceHistory]:
    sku = mkt.catalog.at[i, "sku"]
    out = []
    for j, comp in enumerate(COMPETITORS):
        pts = [
            CompetitorPricePoint(
                sku=sku,
                competitor=comp,
                price=float(mkt.comp_prices[i, j, d]),
                in_stock=bool(mkt.comp_in_stock[i, j, d]),
                date=start + timedelta(days=d),
            )  # fmt: skip
            for d in range(max(0, day - WINDOW + 1), day + 1)
        ]
        out.append(CompetitorPriceHistory(sku=sku, competitor=comp, points=pts))
    return out


def pipeline_step(
    mkt, prices: np.ndarray, day: int, start: date, stats: dict
) -> np.ndarray:
    cat = mkt.catalog.assign(our_price=prices)
    rows = {r["sku"]: r for _, r in cat.iterrows()}
    analyses = {}
    for i, sku in enumerate(cat["sku"]):
        analyses[sku] = analyze_sku(
            _catalog_item(rows[sku]), histories_for(mkt, i, day, start)
        )
    flagged = [a for a in analyses.values() if a.needs_decision]
    stats["flagged"] += len(flagged)
    by_cat: dict[str, list[str]] = {}
    for a in flagged:
        by_cat.setdefault(rows[a.sku]["category"], []).append(a.sku)
    new = prices.copy()
    index = {s: i for i, s in enumerate(cat["sku"])}
    for category, skus in by_cat.items():
        if len(skus) < 2:
            stats["single_sku_skipped"] += 1  # production sends these to the LLM path
            continue
        for rec in decide_for_category(skus, rows, analyses, run_id="backtest"):
            stats["recommendations"] += 1
            if rec.guardrail_violation != GuardrailViolation.NONE:
                stats["blocked_by_guardrail"] += 1
                continue
            outcome = route_recommendation(rec) if rec.confidence >= config.AUTO_APPROVE_CONFIDENCE_THRESHOLD \
                else ApprovalOutcome.QUEUED_FOR_REVIEW  # fmt: skip
            stats[
                "auto_approved"
                if outcome == ApprovalOutcome.AUTO_APPROVED
                else "escalated"
            ] += 1
            # Backtest assumes escalated, guardrail-clean changes are approved by the reviewer.
            new[index[rec.sku]] = rec.recommended_price
    return new


def oracle_prices(mkt, current: np.ndarray, day: int) -> np.ndarray:
    """Per-SKU profit optimum under the true linear demand, clipped to guardrails."""
    m, e, c = mkt.market_price(day), mkt.elasticity, mkt.catalog["cost"].to_numpy()
    p = m * (e - 1) / (2 * e) + c / 2
    lo, hi = guard_bounds(c, current)
    return np.clip(p, lo, hi)


def run() -> dict:
    t0 = time.perf_counter()
    register_all_tools()
    conn = get_connection(config.DB_PATH)
    catalog = get_catalog_df(conn)
    conn.close()
    mkt = build_market(catalog, WARMUP + TEST)
    cost = mkt.catalog["cost"].to_numpy()
    p0 = mkt.catalog["our_price"].to_numpy()
    start = date(2026, 1, 1)

    # --- warmup: price experiments + noisy sales -> elasticity estimate ---
    rng = np.random.default_rng(11)
    sales = []
    for d in range(WARMUP):
        p = p0 * rng.uniform(0.92, 1.08, len(p0))
        m = mkt.market_price(d)
        expected = mkt.expected_units(p, d)
        units = rng.poisson(expected)
        capped = expected >= mkt.catalog["stock"].to_numpy()
        sales.append(pd.DataFrame({"sku": mkt.catalog["sku"], "category": mkt.catalog["category"],
                                   "gap": (p - m) / m, "units": units, "capped": capped}))  # fmt: skip
    est = el.estimate(pd.concat(sales))
    el.save(est, config.ELASTICITY_PATH)

    # --- test period -------------------------------------------------------
    strategies = ["static", "match_market", "match_guarded", "pipeline", "oracle"]
    prices = {s: p0.copy() for s in strategies}
    totals = {s: {"revenue": 0.0, "profit": 0.0, "units": 0.0, "violations": 0, "price_changes": 0}
              for s in strategies}  # fmt: skip
    pstats = {k: 0 for k in ["flagged", "recommendations", "auto_approved", "escalated",
                             "blocked_by_guardrail", "single_sku_skipped"]}  # fmt: skip
    for d in range(WARMUP, WARMUP + TEST):
        m = mkt.market_price(d)
        proposals = {
            "static": prices["static"],
            "match_market": np.round(m, 2),
            "match_guarded": np.round(
                np.clip(m, *guard_bounds(cost, prices["match_guarded"])), 2
            ),
            "pipeline": pipeline_step(mkt, prices["pipeline"], d, start, pstats),
            "oracle": np.clip(np.round(oracle_prices(mkt, prices["oracle"], d), 2),
                              *guard_bounds(cost, prices["oracle"])),
        }
        for s in strategies:
            before, after = prices[s], proposals[s]
            totals[s]["violations"] += violations(cost, before, after)
            totals[s]["price_changes"] += int(np.sum(np.abs(after - before) > 0.005))
            units = mkt.expected_units(after, d)
            totals[s]["revenue"] += float(after @ units)
            totals[s]["profit"] += float((after - cost) @ units)
            totals[s]["units"] += float(units.sum())
            prices[s] = after

    base = totals["static"]["profit"]
    oracle_gain = totals["oracle"]["profit"] - base
    report = {"skus": len(p0), "test_days": TEST, "strategies": {}}
    for s in strategies:
        t = totals[s]
        report["strategies"][s] = {
            "gross_profit": round(t["profit"]), "revenue": round(t["revenue"]),
            "profit_vs_static_pct": round(100 * (t["profit"] - base) / base, 2),
            "share_of_oracle_uplift_pct": round(100 * (t["profit"] - base) / oracle_gain, 1) if oracle_gain else None,
            "avg_margin_pct": round(100 * t["profit"] / t["revenue"], 2),
            "guardrail_violations": t["violations"], "price_changes": t["price_changes"],
        }  # fmt: skip
    true_cat = {c: v for c, v in TRUE_CATEGORY_ELASTICITY.items()}
    report["elasticity"] = {
        c: {"true_category_mean": true_cat[c], "estimated": est.get(c)}
        for c in true_cat
    }
    report["elasticity_mean_abs_error"] = round(
        float(
            np.mean(
                [abs(true_cat[c] - est[c]) for c in true_cat if est.get(c) is not None]
            )
        ),
        3,
    )
    report["pipeline_routing"] = pstats
    report["seconds"] = round(time.perf_counter() - t0, 1)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "backtest.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
