"""
Researcher agent: the first stage of the pipeline. Its only job is to
gather competitor price history for every SKU in our catalog and hand
back a clean ResearchBatch. It does not analyze, judge, or decide
anything -- that separation is deliberate, so each agent has one job
and one kind of failure to reason about.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uuid

import pandas as pd

import config
from models import ResearchBatch
from data.price_source import get_competitor_prices


def run_researcher(catalog: pd.DataFrame, run_id: str = None) -> ResearchBatch:
    """Pulls competitor price history for every SKU in the given
    catalog DataFrame. Errors on individual SKUs (e.g. no competitor
    data available) are collected rather than raised, so one bad SKU
    doesn't take down the whole batch -- this mirrors how a real
    scraper would behave: some sites time out, most don't.
    """
    run_id = run_id or str(uuid.uuid4())[:8]

    histories = []
    errors = []

    for sku in catalog["sku"]:
        try:
            sku_histories = get_competitor_prices(sku)
            if not sku_histories:
                errors.append(f"{sku}: no competitor data found")
                continue
            histories.extend(sku_histories)
        except Exception as e:
            errors.append(f"{sku}: {e}")

    skus_found = len({h.sku for h in histories})

    return ResearchBatch(
        run_id=run_id,
        histories=histories,
        skus_requested=len(catalog),
        skus_found=skus_found,
        errors=errors,
    )


if __name__ == "__main__":
    catalog = pd.read_csv(config.CATALOG_PATH)
    batch = run_researcher(catalog)
    print(f"Run {batch.run_id}: {batch.skus_found}/{batch.skus_requested} SKUs found")
    print(f"Total competitor histories: {len(batch.histories)}")
    if batch.errors:
        print(f"Errors: {batch.errors}")
