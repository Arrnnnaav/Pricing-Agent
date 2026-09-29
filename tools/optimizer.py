"""Category-level LP price optimizer (capstone iteration-2 pattern).
Maximizes total projected revenue across every SKU in a category
subject to: per-SKU price bounds (margin floor + max change from
guardrails.py's constants), a category spend budget, and a simple
linear elasticity-implied demand curve capped at available stock.

This runs when the Analyst has flagged 2+ SKUs in the same category
(see agents/decision.py) -- a single flagged SKU still goes through the
direct per-SKU LLM recommendation path, since there's nothing to jointly
optimize across.
"""

from pydantic import BaseModel
import math

import pulp


class OptimizerInput(BaseModel):
    skus: list[str]
    costs: dict[str, float]
    current_prices: dict[str, float]
    stocks: dict[str, int]
    elasticities: dict[str, float]
    category_budget: float
    min_margin_pct: float
    max_price_change_pct: float
    # Optional, competitor-aware demand model. With reference_prices set,
    # demand responds to our price relative to the market (the competitor
    # average) instead of relative to our own current price -- a price
    # that is 10% above the market loses demand even if it is unchanged.
    reference_prices: dict[str, float] | None = None
    base_demand: dict[str, float] | None = None  # units/day at the reference price
    objective: str = "revenue"  # "revenue" | "profit"
    price_steps: int = 11
    # SKUs at or below this stock may not be priced up (mirrors the
    # LOW_STOCK_BLOCKS_RAISE guardrail); None disables the rule.
    low_stock_threshold: int | None = None


class OptimizerOutput(BaseModel):
    recommended_prices: dict[str, float]


def _demand(sku: str, price: float, inp: OptimizerInput) -> float:
    """Simple linear elasticity-implied demand: demand falls off
    proportionally to the price change times the SKU's elasticity,
    starting from an assumed baseline of 1 unit of relative demand at
    the current price. Capped at stock in the LP constraints below."""
    ref = (inp.reference_prices or {}).get(sku) or inp.current_prices[sku]
    base = (inp.base_demand or {}).get(sku, 1.0)
    pct_change = (price - ref) / ref
    return base * max(0.0, 1.0 + inp.elasticities[sku] * pct_change)


def optimize_category_prices(inp: OptimizerInput) -> OptimizerOutput:
    problem = pulp.LpProblem("category_pricing", pulp.LpMaximize)

    # Discretize each SKU's feasible price range into steps so the LP
    # stays linear (price * demand is nonlinear in price otherwise).
    price_vars: dict[str, dict[float, pulp.LpVariable]] = {}
    for sku in inp.skus:
        current = inp.current_prices[sku]
        cost = inp.costs[sku]
        margin_floor_price = cost / (1 - inp.min_margin_pct)
        max_change_hi = current * (1 + inp.max_price_change_pct)
        lo = max(current * (1 - inp.max_price_change_pct), margin_floor_price)
        hi = max_change_hi
        if (
            inp.low_stock_threshold is not None
            and inp.stocks[sku] <= inp.low_stock_threshold
        ):
            hi = min(hi, current)  # guardrail: no raises on low stock
        # Snap bounds inward to whole cents, then step them in until they pass
        # the same checks the guardrails apply: 1.25 * 1665.32 = 2081.65 is
        # exactly +25% on paper but 0.25000000000000006 in floating point,
        # which the strict max-change guardrail rejected in a live run.
        lo, hi = math.ceil(lo * 100) / 100, math.floor(hi * 100) / 100
        while hi > lo and abs(hi - current) / current > inp.max_price_change_pct:
            hi = round(hi - 0.01, 2)
        while lo < hi and (
            abs(lo - current) / current > inp.max_price_change_pct
            or (lo - cost) / lo < inp.min_margin_pct
        ):
            lo = round(lo + 0.01, 2)
        if lo > hi:
            # Margin floor is the business-critical constraint; when it conflicts
            # with the max-change bound, use the margin floor price as the single
            # feasible point, not the max-change bound.
            lo = hi = margin_floor_price
        steps = inp.price_steps
        candidates = sorted(
            {
                min(hi, max(lo, round(lo + (hi - lo) * i / (steps - 1), 2)))
                for i in range(steps)
            }
        )
        price_vars[sku] = {
            p: pulp.LpVariable(f"{sku}_{i}", cat="Binary")
            for i, p in enumerate(candidates)
        }
        problem += pulp.lpSum(price_vars[sku].values()) == 1  # exactly one price chosen

    revenue_terms = []
    budget_terms = []
    for sku in inp.skus:
        for price, var in price_vars[sku].items():
            demand = _demand(sku, price, inp)
            demand = min(demand, inp.stocks[sku])
            unit_value = price - inp.costs[sku] if inp.objective == "profit" else price
            revenue_terms.append(unit_value * demand * var)
            discount = max(0.0, inp.current_prices[sku] - price)
            budget_terms.append(discount * demand * var)

    problem += pulp.lpSum(revenue_terms)
    problem += pulp.lpSum(budget_terms) <= inp.category_budget

    problem.solve(pulp.PULP_CBC_CMD(msg=False))

    # Check solver status; if not optimal, raise clear error for caller diagnosis.
    status_str = pulp.LpStatus[problem.status]
    if status_str != "Optimal":
        raise RuntimeError(
            f"LP solver returned status '{status_str}' (not Optimal). "
            f"Problem may be infeasible or unbounded; check budget and price bounds."
        )

    recommended = {}
    for sku in inp.skus:
        for price, var in price_vars[sku].items():
            if var.value() == 1:
                recommended[sku] = round(price, 2)
                break

    return OptimizerOutput(recommended_prices=recommended)
