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


def test_optimizer_favors_margin_floor_over_max_change():
    """When margin floor conflicts with max-price-change bound, margin floor wins.

    Scenario: cost=95, current=100, min_margin=10%, max_change=2%
    - Margin floor price: 95 / (1 - 0.10) = 95 / 0.90 = 105.56
    - Max-change upper bound: 100 * (1 + 0.02) = 102.0
    - Margin floor price (105.56) > max-change bound (102.0), so conflict exists
    - Expected: optimizer returns margin-floor price (105.56), not capped at 102.0
    - Resulting margin should be exactly 10% (the minimum required)
    """
    inp = OptimizerInput(
        skus=["X"],
        costs={"X": 95.0},
        current_prices={"X": 100.0},
        stocks={"X": 100},
        elasticities={"X": -1.0},
        category_budget=10000.0,  # high budget so cost is not the limiting factor
        min_margin_pct=0.10,
        max_price_change_pct=0.02,
    )
    out = optimize_category_prices(inp)
    price = out.recommended_prices["X"]
    margin = (price - inp.costs["X"]) / price
    # Margin must be >= min_margin_pct; the fixed price is the margin-floor price
    assert margin >= inp.min_margin_pct - 1e-6, (
        f"Margin {margin:.4f} is below min_margin_pct {inp.min_margin_pct}. "
        f"Optimizer must favor margin floor over max-change bound."
    )


def test_optimizer_budget_constraint_binds():
    """Verify that category_budget constraint is actually enforced and influences the solution.

    Use elastic demand (|elasticity| > 1) so the optimizer wants to cut prices to maximize
    revenue. Then show that a tight budget prevents the optimizer from cutting prices as
    much as it would with a generous budget — proving the budget constraint actually changes
    the outcome.
    """
    # Base inputs with elastic demand (-2.0), so cutting price increases revenue
    base_inp = {
        "skus": ["R"],
        "costs": {"R": 40.0},
        "current_prices": {"R": 100.0},
        "stocks": {"R": 100},
        "elasticities": {"R": -2.0},  # Elastic: cutting price boosts revenue
        "min_margin_pct": 0.20,  # 20% margin floor (min price: 40 / 0.80 = 50.0)
        "max_price_change_pct": 0.50,  # Allow up to 50% change (range: [50.0, 150.0])
    }

    # Run with generous budget: optimizer should cut price significantly
    inp_generous = OptimizerInput(category_budget=1000.0, **base_inp)
    out_generous = optimize_category_prices(inp_generous)
    price_generous = out_generous.recommended_prices["R"]

    # Run with tight budget: optimizer should cut price less (price higher)
    inp_tight = OptimizerInput(category_budget=1.0, **base_inp)
    out_tight = optimize_category_prices(inp_tight)
    price_tight = out_tight.recommended_prices["R"]

    # Verify both satisfy margin and change constraints
    for price in [price_generous, price_tight]:
        margin = (price - base_inp["costs"]["R"]) / price
        change_pct = (
            abs(price - base_inp["current_prices"]["R"])
            / base_inp["current_prices"]["R"]
        )
        assert margin >= base_inp["min_margin_pct"] - 1e-6
        assert change_pct <= base_inp["max_price_change_pct"] + 1e-6

    # The tight budget must result in a higher price (less discount) than generous budget.
    # This proves the budget constraint is binding and changes the optimizer's choice.
    assert price_tight > price_generous, (
        f"With elastic demand, a tight budget (1.0) should force a higher price than a generous "
        f"budget (1000.0). Got tight={price_tight}, generous={price_generous}. "
        f"Budget constraint is not binding."
    )
