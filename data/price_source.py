"""Competitor price data retrieval.

fetch_batch runs one parameterized SQL query covering every SKU in a
batch, instead of one call per SKU. An earlier version generated this
SQL via an LLM call, but the query's shape never actually varied --
every run needs the same thing (full 7-day history, all competitors,
in-stock and out-of-stock both, since out-of-stock rows are what keep
trend_pct honest) -- so the LLM call added hallucination risk, latency,
and cost with no real judgment behind it. A parameterized query does the
identical job with none of that, and removes a single point of failure
from the first pipeline stage everything else depends on.

assert_readonly_sql is kept as a guard for any future retrieval path
that DOES need to vary its query shape (e.g. an LLM deciding which
columns/filters to pull for a genuinely different request) -- it's cheap
insurance to keep in place even though fetch_batch itself no longer
generates SQL.
"""

import re

_DISALLOWED_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "PRAGMA",
    "ATTACH",
    "DETACH",
    "VACUUM",
)


def assert_readonly_sql(sql: str) -> None:
    """Raises ValueError unless `sql` is a single read-only SELECT
    statement. Rejects multiple statements (semicolon-separated) and
    any DML/DDL/PRAGMA keyword appearing anywhere in the text."""
    stripped = sql.strip().rstrip(";")

    if ";" in stripped:
        raise ValueError("Multiple SQL statements are not allowed")

    if not re.match(r"^\s*SELECT\b", stripped, re.IGNORECASE):
        raise ValueError("Only SELECT statements are allowed")

    upper = stripped.upper()
    for keyword in _DISALLOWED_KEYWORDS:
        if re.search(rf"\b{keyword}\b", upper):
            raise ValueError(f"Disallowed keyword in generated SQL: {keyword}")


from datetime import datetime

from models import CompetitorPricePoint, CompetitorPriceHistory


def fetch_batch(conn, skus: list[str]) -> dict[str, list[CompetitorPriceHistory]]:
    """Pulls every competitor_prices row for the given SKUs in one
    parameterized query. Returns a dict of sku -> list of
    CompetitorPriceHistory (one per competitor that carries it).
    """
    if not skus:
        return {}

    placeholders = ",".join("?" * len(skus))
    sql = f"SELECT * FROM competitor_prices WHERE sku IN ({placeholders})"

    cur = conn.execute(sql, skus)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    by_sku_competitor: dict[tuple[str, str], list[CompetitorPricePoint]] = {}
    for row in rows:
        key = (row["sku"], row["competitor"])
        by_sku_competitor.setdefault(key, []).append(
            CompetitorPricePoint(
                sku=row["sku"],
                competitor=row["competitor"],
                price=row["price"],
                in_stock=bool(row["in_stock"]),
                date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
            )
        )

    result: dict[str, list[CompetitorPriceHistory]] = {}
    for (sku, competitor), points in by_sku_competitor.items():
        result.setdefault(sku, []).append(
            CompetitorPriceHistory(sku=sku, competitor=competitor, points=points)
        )
    return result
