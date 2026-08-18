"""Registers the existing synthetic competitor-price generator as a
formal tool, so it goes through the same input/output validation and
audit trail as optimizer/semantic_matcher, instead of being called as a
bare function. Still synthetic data -- see the design spec's "out of
scope" section for why real scraping isn't implemented."""

import pandas as pd
from pydantic import BaseModel

from data.mock_price_generator import generate_price_history


class ScrapeInput(BaseModel):
    skus: list[str]
    days: int = 7


class ScrapeOutput(BaseModel):
    rows_generated: int


def scrape_competitor_prices(inp: ScrapeInput) -> ScrapeOutput:
    fake_catalog = pd.DataFrame(
        {
            "sku": inp.skus,
            "our_price": [100.0]
            * len(inp.skus),  # placeholder price for the walk's target
        }
    )
    df = generate_price_history(fake_catalog, days=inp.days)
    return ScrapeOutput(rows_generated=len(df))
