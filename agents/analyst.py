"""
Analyst agent: takes the Researcher's raw price histories plus our own
catalog, and for every SKU computes how we compare to the competitive
market -- min/max/avg competitor price, our delta from that average,
and the average trend direction. It then decides which SKUs are worth
sending to the (expensive, LLM-backed) Decision agent, using pure
guardrail math from guardrails.py -- no LLM call happens in this file.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statistics

import pandas as pd

import config
from models import CatalogItem, ResearchBatch, SkuAnalysis, AnalysisBatch
from agents.guardrails import check_margin_floor

# A SKU is only worth sending to the Decision agent if our price gap
# from the competitive average is at least this large. Below this, the
# gap is noise-level and not worth an LLM call (and a real cost-guard
# reason to not analyze every SKU on every run).
MIN_DELTA_PCT_TO_FLAG = 0.03


def _catalog_item(row: pd.Series) -> CatalogItem:
    return CatalogItem(
        sku=row["sku"], name=row["name"], brand=row["brand"],
        category=row["category"], our_price=row["our_price"],
        cost=row["cost"], stock=row["stock"],
    )


def analyze_sku(item: CatalogItem, histories: list) -> SkuAnalysis:
    """Builds one SkuAnalysis from a catalog item and its list of
    CompetitorPriceHistory objects (one per competitor that carries it).
    Only in-stock competitor prices count toward min/max/avg -- an
    out-of-stock listing isn't a price you could actually be undercut by.
    """
    in_stock_latest = [
        h.latest.price for h in histories if h.latest.in_stock
    ]

    if not in_stock_latest:
        return SkuAnalysis(
            sku=item.sku,
            our_price=item.our_price,
            current_margin_pct=item.current_margin_pct,
            competitor_count=len(histories),
            needs_decision=False,
            notes="No in-stock competitor prices available",
        )

    avg_price = statistics.mean(in_stock_latest)
    delta_pct = (item.our_price - avg_price) / item.our_price
    avg_trend = statistics.mean(h.trend_pct for h in histories)

    # Flag for a decision if: the price gap is meaningful in either
    # direction, AND acting on it wouldn't already be blocked by the
    # margin guardrail (no point sending a SKU to the Decision agent
    # for a price cut that guardrails.py would reject anyway).
    meaningful_gap = abs(delta_pct) >= MIN_DELTA_PCT_TO_FLAG
    room_to_move = check_margin_floor(item, avg_price) if delta_pct > 0 else True

    return SkuAnalysis(
        sku=item.sku,
        our_price=item.our_price,
        min_competitor_price=min(in_stock_latest),
        max_competitor_price=max(in_stock_latest),
        avg_competitor_price=round(avg_price, 2),
        price_delta_pct=round(delta_pct, 4),
        avg_competitor_trend_pct=round(avg_trend, 4),
        current_margin_pct=item.current_margin_pct,
        competitor_count=len(histories),
        needs_decision=meaningful_gap and room_to_move,
        notes=(
            f"{'Overpriced' if delta_pct > 0 else 'Underpriced'} vs market by "
            f"{abs(delta_pct):.1%}, trend {avg_trend:+.1%}"
        ),
    )


def run_analyst(catalog: pd.DataFrame, research: ResearchBatch) -> AnalysisBatch:
    """Runs analyze_sku for every SKU in the catalog, grouping the
    research batch's flat history list back by SKU first."""
    histories_by_sku: dict[str, list] = {}
    for h in research.histories:
        histories_by_sku.setdefault(h.sku, []).append(h)

    analyses = []
    for _, row in catalog.iterrows():
        item = _catalog_item(row)
        sku_histories = histories_by_sku.get(item.sku, [])
        analyses.append(analyze_sku(item, sku_histories))

    flagged = sum(1 for a in analyses if a.needs_decision)

    return AnalysisBatch(
        run_id=research.run_id,
        analyses=analyses,
        flagged_count=flagged,
    )


if __name__ == "__main__":
    from agents.researcher import run_researcher

    catalog = pd.read_csv(config.CATALOG_PATH)
    research = run_researcher(catalog)
    analysis = run_analyst(catalog, research)

    print(f"Run {analysis.run_id}: {analysis.flagged_count}/{len(analysis.analyses)} SKUs flagged")
    for a in analysis.analyses:
        if a.needs_decision:
            print(f"  {a.sku}: our=${a.our_price:.2f} avg_competitor=${a.avg_competitor_price:.2f} "
                  f"delta={a.price_delta_pct:+.1%} trend={a.avg_competitor_trend_pct:+.1%}")
