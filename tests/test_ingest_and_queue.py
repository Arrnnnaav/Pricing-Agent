import random
import sqlite3

import pandas as pd

import config
from agents import approval
from data.ingest import matching_quality
from data.listings import noisy_name, to_listings
from db.connection import init_db
from models import ApprovalOutcome, GuardrailViolation, PriceRecommendation
from tools.optimizer import OptimizerInput, optimize_category_prices
from tools.semantic_matcher import MatchInput, match_listing_to_sku

CANDIDATES = {
    "LAP-0001": "Dell Vortex Pro K42",
    "LAP-0002": "Dell Vortex Pro K57",
    "LAP-0003": "HP Vortex Air S19",
    "SMA-0004": "Samsung Nova Max T88",
}


def test_matcher_uses_model_number_to_separate_siblings():
    out = match_listing_to_sku(MatchInput(listing_name="DELL vortex k57 PRO (2026 Edition)",
                                          candidate_skus=CANDIDATES))  # fmt: skip
    assert out.matched_sku == "LAP-0002"


def test_matcher_brand_blocking_and_typos():
    out = match_listing_to_sku(MatchInput(listing_name="hp votrex air s19 | Free Shipping",
                                          candidate_skus=CANDIDATES))  # fmt: skip
    assert out.matched_sku == "LAP-0003"


def test_matcher_leaves_unknown_product_unmatched():
    out = match_listing_to_sku(MatchInput(listing_name="Dell Zephyr Lite D33",
                                          candidate_skus=CANDIDATES))  # fmt: skip
    assert out.matched_sku is None


def test_listings_hide_sku_and_keep_stable_names():
    catalog = pd.DataFrame(
        {"sku": ["A"], "name": ["Dell Vortex Pro K42"], "brand": ["Dell"]}
    )
    history = pd.DataFrame({"sku": ["A", "A"], "competitor": ["C1", "C1"],
                            "date": ["2026-01-01", "2026-01-02"], "price": [10.0, 11.0],
                            "in_stock": [True, True]})  # fmt: skip
    df = to_listings(catalog, history, decoy_rate=0)
    assert (
        df["listing_name"].nunique() == 1
    )  # same product, same listing name every day
    assert "sku" not in df.columns and set(df["true_sku"]) == {"A"}
    assert noisy_name("Dell Vortex Pro K42", random.Random(1))  # non-empty


def test_matching_quality_counts():
    rows = [
        {"competitor": "c", "listing_name": "a", "matched_sku": "A", "true_sku": "A"},
        {"competitor": "c", "listing_name": "b", "matched_sku": "X", "true_sku": "B"},
        {"competitor": "c", "listing_name": "d", "matched_sku": None, "true_sku": None},
    ]
    q = matching_quality(rows)
    assert (
        q["precision"] == 0.5
        and q["recall"] == 0.5
        and q["decoys_wrongly_matched"] == 0
    )


def test_competitor_aware_optimizer_moves_toward_market():
    base = dict(skus=["A"], costs={"A": 50.0}, current_prices={"A": 120.0}, stocks={"A": 1000},
                elasticities={"A": -3.0}, category_budget=1e9, min_margin_pct=0.15,
                max_price_change_pct=0.25, objective="profit", price_steps=21)  # fmt: skip
    # Market at 100: at 120 we are 20% above it with elastic demand -> cut.
    out = optimize_category_prices(
        OptimizerInput(**base, reference_prices={"A": 100.0})
    )
    assert out.recommended_prices["A"] < 120.0


def test_queue_mode_writes_pending_approval_and_does_not_execute(monkeypatch):
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    monkeypatch.setattr(config, "APPROVAL_MODE", "queue")
    rec = PriceRecommendation(sku="A", current_price=100.0, recommended_price=95.0,
                              confidence=0.5, reasoning="r", projected_margin_pct=0.3,
                              reversible=True, guardrail_violation=GuardrailViolation.NONE)  # fmt: skip
    catalog = pd.DataFrame({"sku": ["A"], "our_price": [100.0]})
    [res] = approval.run_approval([rec], catalog, conn=conn, run_id="r1")
    assert res.outcome == ApprovalOutcome.QUEUED_FOR_REVIEW and not res.executed
    assert catalog["our_price"][0] == 100.0
    assert conn.execute("SELECT sku, status FROM pending_approvals").fetchall() == [
        ("A", "pending")
    ]


def test_optimizer_never_exceeds_max_change_after_rounding_and_blocks_low_stock_raise():
    inp = OptimizerInput(skus=["A", "B"], costs={"A": 1307.94, "B": 549.25},
                         current_prices={"A": 1665.32, "B": 717.19}, stocks={"A": 105, "B": 0},
                         elasticities={"A": -0.2, "B": -0.2}, category_budget=1e9,
                         min_margin_pct=0.15, max_price_change_pct=0.25, objective="profit",
                         reference_prices={"A": 2500.0, "B": 900.0}, low_stock_threshold=10,
                         price_steps=21)  # fmt: skip
    out = optimize_category_prices(inp).recommended_prices
    assert abs(out["A"] - 1665.32) / 1665.32 <= 0.25  # the case that failed live (1.2500002)
    assert out["B"] <= 717.19  # zero stock: no raise
