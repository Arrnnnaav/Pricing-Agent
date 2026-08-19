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
from db.connection import get_connection
from data.price_source import fetch_batch


def run_researcher(catalog: pd.DataFrame, run_id: str = None) -> ResearchBatch:
    """Pulls competitor price history for every SKU in the given
    catalog DataFrame via one parameterized SQL query covering every
    SKU, instead of one query per SKU.
    """
    run_id = run_id or str(uuid.uuid4())[:8]

    conn = get_connection(config.DB_PATH)
    skus = catalog["sku"].tolist()

    errors = []
    try:
        histories_by_sku = fetch_batch(conn, skus)
    except Exception as e:
        histories_by_sku = {}
        errors.append(f"batch retrieval failed: {e}")
    finally:
        conn.close()

    histories = [h for hs in histories_by_sku.values() for h in hs]
    found_skus = set(histories_by_sku.keys())
    for sku in skus:
        if sku not in found_skus:
            errors.append(f"{sku}: no competitor data found")

    return ResearchBatch(
        run_id=run_id,
        histories=histories,
        skus_requested=len(catalog),
        skus_found=len(found_skus),
        errors=errors,
    )


if __name__ == "__main__":
    from db.catalog_repo import get_catalog_df

    conn = get_connection(config.DB_PATH)
    catalog = get_catalog_df(conn)
    conn.close()

    batch = run_researcher(catalog)
    print(f"Run {batch.run_id}: {batch.skus_found}/{batch.skus_requested} SKUs found")
    print(f"Total competitor histories: {len(batch.histories)}")
    if batch.errors:
        print(f"Errors: {batch.errors}")
