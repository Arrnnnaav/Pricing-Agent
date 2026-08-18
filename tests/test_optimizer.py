from tools.optimizer import OptimizerInput, OptimizerOutput, optimize_category_prices


def test_optimizer_respects_margin_and_change_bounds():
    inp = OptimizerInput(
        skus=["A", "B"],
        costs={"A": 100.0, "B": 200.0},
        current_prices={"A": 150.0, "B": 250.0},
        stocks={"A": 50, "B": 50},
        elasticities={"A": -1.2, "B": -0.8},
        category_budget=1000.0,
        min_margin_pct=0.15,
        max_price_change_pct=0.25,
    )
    out = optimize_category_prices(inp)
    assert isinstance(out, OptimizerOutput)
    for sku in inp.skus:
        price = out.recommended_prices[sku]
        margin = (price - inp.costs[sku]) / price
        change_pct = abs(price - inp.current_prices[sku]) / inp.current_prices[sku]
        assert margin >= inp.min_margin_pct - 1e-6
        assert change_pct <= inp.max_price_change_pct + 1e-6
