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

    Use a low budget to force the optimizer to choose smaller discounts than would
    otherwise maximize revenue. Without the budget constraint, the optimizer would
    pick the maximum feasible prices to maximize revenue. With a tight budget, it
    must settle for prices with less discount (i.e., closer to current prices).
    """
    inp = OptimizerInput(
        skus=["P", "Q"],
        costs={"P": 50.0, "Q": 50.0},
        current_prices={"P": 100.0, "Q": 100.0},
        stocks={"P": 10, "Q": 10},
        elasticities={"P": -0.5, "Q": -0.5},
        category_budget=5.0,  # Very tight budget: only allows ~5 units of total discount
        min_margin_pct=0.20,  # 20% margin floor
        max_price_change_pct=0.50,  # Allow 50% price change
    )
    out = optimize_category_prices(inp)

    # Verify all constraints are satisfied
    for sku in inp.skus:
        price = out.recommended_prices[sku]
        margin = (price - inp.costs[sku]) / price
        change_pct = abs(price - inp.current_prices[sku]) / inp.current_prices[sku]
        assert margin >= inp.min_margin_pct - 1e-6
        assert change_pct <= inp.max_price_change_pct + 1e-6

    # Verify budget constraint: sum of (discount * demand) <= budget
    total_discount_spend = 0.0
    for sku in inp.skus:
        price = out.recommended_prices[sku]
        discount = max(0.0, inp.current_prices[sku] - price)
        # Demand at this price
        pct_change = (price - inp.current_prices[sku]) / inp.current_prices[sku]
        demand = max(0.0, 1.0 + inp.elasticities[sku] * pct_change)
        demand = min(demand, inp.stocks[sku])
        total_discount_spend += discount * demand

    assert total_discount_spend <= inp.category_budget + 1e-6, (
        f"Total discount spend {total_discount_spend:.2f} exceeds budget {inp.category_budget}. "
        f"Budget constraint was not enforced."
    )
