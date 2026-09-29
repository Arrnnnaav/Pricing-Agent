"""Registers the synthetic competitor feed as a formal tool, so it goes
through the same input/output validation and audit trail as the optimizer
and matcher. It returns what a real scraper would: competitor listings
with free-text names and no SKU codes (see data/listings.py). Still
synthetic -- real scraping is out of scope for ToS reasons."""

import pandas as pd
from pydantic import BaseModel

import config
from data.listings import to_listings
from data.mock_price_generator import generate_price_history


class ScrapeInput(BaseModel):
    catalog: list[dict]  # sku, name, brand, our_price
    days: int = config.PRICE_HISTORY_DAYS
    decoy_rate: float = 0.1


class Listing(BaseModel):
    competitor: str
    listing_name: str
    price: float
    in_stock: int
    date: str
    true_sku: str | None = None  # ground truth, used only to score the matcher


class ScrapeOutput(BaseModel):
    listings: list[Listing]


def scrape_competitor_prices(inp: ScrapeInput) -> ScrapeOutput:
    catalog = pd.DataFrame(inp.catalog)
    history = generate_price_history(catalog, days=inp.days)
    df = to_listings(catalog, history, decoy_rate=inp.decoy_rate)
    df = df.astype(object).where(pd.notna(df), None)  # NaN -> None for decoys
    return ScrapeOutput(listings=[Listing(**r) for r in df.to_dict(orient="records")])
