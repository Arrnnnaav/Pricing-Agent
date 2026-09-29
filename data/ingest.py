"""Ingestion: scraper tool -> fuzzy matcher tool -> competitor_prices.

Listings arrive without SKU codes. Each distinct (competitor, listing
name) is matched once (the same listing repeats every day) and the match
is applied to all of its daily prices. Unmatched listings are kept in
competitor_listings with matched_sku NULL for review instead of being
forced onto the nearest SKU.

Run:  python -m data.ingest
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

import config
from db.catalog_repo import get_catalog_df
from db.connection import get_connection, init_db
from db.competitor_repo import insert_competitor_price_rows
from tools.registry import call_tool, register_all_tools


def matching_quality(rows: list[dict]) -> dict:
    """Precision/recall of the matcher against ground truth, per distinct listing."""
    seen = {}
    for r in rows:
        seen[(r["competitor"], r["listing_name"])] = (r["matched_sku"], r["true_sku"])
    tp = sum(1 for m, t in seen.values() if m and m == t)
    fp = sum(1 for m, t in seen.values() if m and m != t)
    fn = sum(1 for m, t in seen.values() if t and m != t)
    decoys = [m for m, t in seen.values() if t is None]
    return {
        "listings": len(seen),
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "decoys": len(decoys),
        "decoys_wrongly_matched": sum(1 for m in decoys if m),
    }


def ingest(conn, run_id: str = "ingest") -> dict:
    register_all_tools()
    catalog = get_catalog_df(conn)
    t0 = time.perf_counter()
    scraped = call_tool(
        "scraper",
        {
            "catalog": catalog[["sku", "name", "brand", "our_price"]].to_dict(
                orient="records"
            )
        },
        run_id=run_id,
    )
    candidates = dict(zip(catalog["sku"], catalog["name"]))
    matches: dict[tuple[str, str], tuple[str | None, float]] = {}
    for l in scraped.listings:
        key = (l.competitor, l.listing_name)
        if key not in matches:
            out = call_tool(
                "semantic_matcher",
                {"listing_name": l.listing_name, "candidate_skus": candidates},
                run_id=run_id,
            )
            matches[key] = (out.matched_sku, out.score)

    rows, prices = [], []
    for l in scraped.listings:
        sku, score = matches[(l.competitor, l.listing_name)]
        rows.append({**l.model_dump(), "matched_sku": sku, "match_score": score})
        if sku:
            prices.append({"sku": sku, "competitor": l.competitor, "price": l.price,
                           "in_stock": l.in_stock, "date": l.date})  # fmt: skip

    conn.execute("DELETE FROM competitor_listings")
    conn.execute("DELETE FROM competitor_prices")
    conn.executemany(
        """INSERT OR REPLACE INTO competitor_listings
           (competitor, listing_name, price, in_stock, date, matched_sku, match_score, true_sku)
           VALUES (:competitor, :listing_name, :price, :in_stock, :date, :matched_sku,
                   :match_score, :true_sku)""",
        rows,
    )
    insert_competitor_price_rows(conn, prices)
    quality = matching_quality(rows)
    quality.update(price_rows=len(prices), seconds=round(time.perf_counter() - t0, 2))
    return quality


if __name__ == "__main__":
    conn = get_connection(config.DB_PATH)
    init_db(conn)
    print(ingest(conn))
    conn.close()
