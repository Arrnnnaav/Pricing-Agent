"""
Decision agent: for every SKU the Analyst flagged, asks the configured LLM (local Ollama by default) to
recommend a new price with a confidence score and reasoning, returned
as a schema-validated PriceRecommendation. After the LLM responds, we
re-run guardrails.py against its ACTUAL recommended price (not just the
average competitor price the Analyst used to decide whether to flag
the SKU) -- the model might recommend something more aggressive than
the average, so this is a second, independent check before anything
reaches the approval gate.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import config
from models import (
    CatalogItem,
    AnalysisBatch,
    SkuAnalysis,
    LLMPriceSuggestion,
    PriceRecommendation,
    DecisionBatch,
)
from agents.guardrails import evaluate_guardrails
from agents.llm_client import generate_structured, RunCostTracker
from audit.logger import log_event
from tools.registry import call_tool, register_all_tools
from tools.optimizer import OptimizerOutput

PROMPT_TEMPLATE = """You are a pricing analyst for an electronics retailer. \
Recommend a new price for this product based on competitor data.

Product: {sku}
Our current price: ${our_price:.2f}
Our cost: ${cost:.2f} (minimum allowed margin: {min_margin:.0%})
Competitor prices (in stock, most recent): min=${min_comp:.2f}, \
max=${max_comp:.2f}, avg=${avg_comp:.2f}
Competitor price trend over the last week: {trend:+.1%} \
({trend_desc})
Number of competitors tracked: {competitor_count}

Rules:
- Never recommend a price that drops margin below {min_margin:.0%}.
- Do not recommend a change larger than {max_change:.0%} of the current price.
- Prefer matching the competitive average unless the trend suggests \
otherwise (e.g. a strong downward trend may warrant pricing slightly \
below the current average to stay competitive as the market moves).
- confidence should reflect how clear-cut this decision is: a stable, \
consistent trend across competitors deserves higher confidence than a \
single-day gap or a noisy/inconsistent trend.

Respond with the new recommended_price, your confidence (0 to 1), and \
your reasoning.
"""


def _catalog_item(row: pd.Series) -> CatalogItem:
    return CatalogItem(
        sku=row["sku"],
        name=row["name"],
        brand=row["brand"],
        category=row["category"],
        our_price=row["our_price"],
        cost=row["cost"],
        stock=row["stock"],
    )


def group_flagged_by_category(
    catalog_by_sku: dict, flagged: list
) -> dict[str, list[str]]:
    """Groups flagged SKUs by category so run_decision can send
    multi-SKU categories through the LP optimizer instead of pricing
    each SKU independently."""
    grouped: dict[str, list[str]] = {}
    for a in flagged:
        category = catalog_by_sku[a.sku]["category"]
        grouped.setdefault(category, []).append(a.sku)
    return grouped


def build_prompt(item: CatalogItem, a: SkuAnalysis) -> str:
    trend = a.avg_competitor_trend_pct or 0.0
    trend_desc = "rising" if trend > 0.01 else "falling" if trend < -0.01 else "flat"
    return PROMPT_TEMPLATE.format(
        sku=item.sku,
        our_price=item.our_price,
        cost=item.cost,
        min_margin=config.MIN_MARGIN_PCT,
        min_comp=a.min_competitor_price,
        max_comp=a.max_competitor_price,
        avg_comp=a.avg_competitor_price,
        trend=trend,
        trend_desc=trend_desc,
        competitor_count=a.competitor_count,
        max_change=config.MAX_PRICE_CHANGE_PCT,
    )


def decide_for_sku(
    item: CatalogItem, analysis: SkuAnalysis, cost_tracker: RunCostTracker
) -> PriceRecommendation:
    prompt = build_prompt(item, analysis)

    # Ask Gemini for only the minimal schema -- see LLMPriceSuggestion's
    # docstring in models.py for why we don't hand it our full internal
    # PriceRecommendation model directly.
    suggestion = generate_structured(
        prompt=prompt,
        response_model=LLMPriceSuggestion,
        cost_tracker=cost_tracker,
        fallback_factory=lambda: LLMPriceSuggestion(
            recommended_price=analysis.avg_competitor_price or item.our_price,
            confidence=0.5,
            reasoning="Fallback: rule-based match to competitor average after LLM failure.",
        ),
    )

    projected_margin = round(
        (suggestion.recommended_price - item.cost) / suggestion.recommended_price, 4
    )

    # Build the full internal model ourselves. This is also where
    # PriceRecommendation's own field_validator (sane_change, in
    # models.py) runs -- if Gemini suggested something wildly off
    # despite the prompt's rules, construction fails here and the
    # caller (run_decision) catches it as a per-SKU failure.
    rec = PriceRecommendation(
        sku=item.sku,
        current_price=item.our_price,
        recommended_price=suggestion.recommended_price,
        confidence=suggestion.confidence,
        reasoning=suggestion.reasoning,
        projected_margin_pct=projected_margin,
    )

    # Second, independent guardrail check against what the model
    # actually recommended -- the Analyst's earlier check only looked
    # at the competitive average, not this specific number.
    rec.guardrail_violation = evaluate_guardrails(item, rec.recommended_price)

    # A price increase above half the max allowed change is treated as
    # not reversible -- forces human review at the approval gate even
    # if confidence came back high, since raising customer-facing prices
    # is a harder action to walk back than a cut.
    is_raise = rec.recommended_price > item.our_price
    raise_pct = (
        (rec.recommended_price - item.our_price) / item.our_price if is_raise else 0
    )
    rec.reversible = not (is_raise and raise_pct > config.MAX_PRICE_CHANGE_PCT / 2)

    return rec


def decide_for_category(
    skus: list[str],
    catalog_by_sku: dict,
    analyses_by_sku: dict,
    run_id: str,
) -> list:
    """For 2+ flagged SKUs sharing a category: run the LP optimizer
    once for the whole group, then build one PriceRecommendation per
    SKU from its optimized price. Optimizer decides the price; confidence
    is fixed at 0.85 (math-backed, not an LLM guess, but below
    AUTO_APPROVE_CONFIDENCE_THRESHOLD to force human review of the first
    batch of optimizer output), and reasoning is templated. See Task 8's
    optimizer.py docstring for the revenue/budget/elasticity/stock
    formulation.
    """
    items = {sku: _catalog_item(catalog_by_sku[sku]) for sku in skus}
    optimizer_input = {
        "skus": skus,
        "costs": {s: items[s].cost for s in skus},
        "current_prices": {s: items[s].our_price for s in skus},
        "stocks": {s: items[s].stock for s in skus},
        # Per-category elasticity, estimated from sales history by
        # simulation/elasticity.py when available (config default otherwise).
        "elasticities": {s: config.category_elasticity(catalog_by_sku[s]["category"]) for s in skus},
        "category_budget": sum(items[s].our_price for s in skus) * 0.1,
        "min_margin_pct": config.MIN_MARGIN_PCT,
        "max_price_change_pct": config.MAX_PRICE_CHANGE_PCT,
        # Competitor-aware demand: price relative to the market average.
        "reference_prices": {
            s: analyses_by_sku[s].avg_competitor_price or items[s].our_price for s in skus
        },
        "objective": "profit",
        "low_stock_threshold": config.LOW_STOCK_THRESHOLD_UNITS,
        "price_steps": 21,
    }
    output: OptimizerOutput = call_tool("optimizer", optimizer_input, run_id=run_id)

    recommendations = []
    for sku in skus:
        try:
            item = items[sku]
            new_price = output.recommended_prices[sku]
            projected_margin = round((new_price - item.cost) / new_price, 4)
            rec = PriceRecommendation(
                sku=sku,
                current_price=item.our_price,
                recommended_price=new_price,
                # Optimizer output is math-backed, so confidence encodes the
                # auto-approval policy rather than a model's self-report:
                # small moves auto-execute, larger ones go to a human.
                confidence=(
                    config.OPTIMIZER_SMALL_CHANGE_CONFIDENCE
                    if abs(new_price - item.our_price) / item.our_price
                    <= config.OPTIMIZER_AUTO_MAX_CHANGE_PCT
                    else config.OPTIMIZER_LARGE_CHANGE_CONFIDENCE
                ),
                reasoning=(
                    f"LP-optimized category price for {catalog_by_sku[sku]['category']}: "
                    f"maximizes group revenue within budget/margin/stock constraints."
                ),
                projected_margin_pct=projected_margin,
            )
            rec.guardrail_violation = evaluate_guardrails(item, new_price)
            rec.source = "optimizer"
            recommendations.append(rec)
        except Exception as e:
            print(f"  [optimizer recommendation failed for {sku}]: {e}")
    return recommendations


def run_decision(catalog: pd.DataFrame, analysis: AnalysisBatch) -> DecisionBatch:
    register_all_tools()
    cost_tracker = RunCostTracker()
    catalog_by_sku = {row["sku"]: row for _, row in catalog.iterrows()}
    analyses_by_sku = {a.sku: a for a in analysis.analyses}
    flagged = [a for a in analysis.analyses if a.needs_decision]

    grouped = group_flagged_by_category(catalog_by_sku, flagged)

    recommendations = []
    for category, skus in grouped.items():
        if len(skus) >= 2:
            try:
                recommendations.extend(
                    decide_for_category(
                        skus, catalog_by_sku, analyses_by_sku, analysis.run_id
                    )
                )
            except Exception as e:
                print(f"  [optimizer failed for category {category}]: {e}")
            continue
        for sku in skus:
            item = _catalog_item(catalog_by_sku[sku])
            try:
                rec = decide_for_sku(item, analyses_by_sku[sku], cost_tracker)
                recommendations.append(rec)
            except Exception as e:
                print(f"  [decision failed for {sku}]: {e}")

    # Records how many generate_structured calls were attempted vs.
    # succeeded (including via the fallback path) for this run, so
    # audit.metrics.compute_run_metrics can compute a real
    # schema_success_rate instead of inferring it from output presence.
    log_event(
        analysis.run_id,
        "decision",
        "llm_stats",
        cost_tracker.stats(),
    )

    return DecisionBatch(run_id=analysis.run_id, recommendations=recommendations)


if __name__ == "__main__":
    from db.connection import get_connection
    from db.catalog_repo import get_catalog_df
    from agents.researcher import run_researcher
    from agents.analyst import run_analyst

    conn = get_connection(config.DB_PATH)
    catalog = get_catalog_df(conn)
    conn.close()

    research = run_researcher(catalog)
    analysis = run_analyst(catalog, research)
    decisions = run_decision(catalog, analysis)

    print(f"Run {decisions.run_id}: {len(decisions.recommendations)} recommendations")
    for rec in decisions.recommendations:
        print(
            f"  {rec.sku}: ${rec.current_price:.2f} -> ${rec.recommended_price:.2f} "
            f"(confidence={rec.confidence:.0%}, violation={rec.guardrail_violation.value}, "
            f"reversible={rec.reversible})"
        )
        print(f"    reasoning: {rec.reasoning}")
