"""
The interface the Researcher agent calls to get competitor price data.

This file is the deliberate "swap point" in the architecture: today,
get_competitor_prices() reads from our synthetic CSV. If this project
ever needed real data, only this file would change -- researcher.py,
analyst.py, and everything downstream would keep working unmodified,
because they only depend on the CompetitorPriceHistory shape defined
in models.py, not on how that data was produced.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date as date_type, datetime

import pandas as pd

import config
from models import CompetitorPricePoint, CompetitorPriceHistory


def _load_history_csv() -> pd.DataFrame:
    """Loads the raw history CSV, generating it first if it doesn't
    exist yet. Keeps callers from having to remember to run the
    generator script manually before their first pipeline run."""
    if not os.path.exists(config.PRICE_HISTORY_PATH):
        from mock_price_generator import generate_price_history
        catalog = pd.read_csv(config.CATALOG_PATH)
        df = generate_price_history(catalog)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        df.to_csv(config.PRICE_HISTORY_PATH, index=False)
        return df
    return pd.read_csv(config.PRICE_HISTORY_PATH)


def get_competitor_prices(sku: str) -> list[CompetitorPriceHistory]:
    """Returns this SKU's price history, one CompetitorPriceHistory per
    competitor that carries it. This is the only function researcher.py
    should call -- it never touches the CSV or pandas directly, which
    is what keeps the agent layer decoupled from the data layer.
    """
    df = _load_history_csv()
    sku_rows = df[df["sku"] == sku]

    histories = []
    for competitor, group in sku_rows.groupby("competitor"):
        points = [
            CompetitorPricePoint(
                sku=sku,
                competitor=competitor,
                price=row["price"],
                in_stock=bool(row["in_stock"]),
                date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
            )
            for _, row in group.iterrows()
        ]
        histories.append(
            CompetitorPriceHistory(sku=sku, competitor=competitor, points=points)
        )

    return histories


def get_all_skus_with_prices() -> list[str]:
    """Every SKU that has at least some competitor data -- used by the
    Researcher agent to know what to iterate over."""
    df = _load_history_csv()
    return sorted(df["sku"].unique().tolist())
