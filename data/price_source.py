"""SQL/RAG retrieval for competitor price data.

The Researcher agent asks an LLM to write a SQL SELECT against the
competitor_prices table (see build_batch_sql_prompt / fetch_batch in
Task 6) instead of filtering a DataFrame directly -- that's the "RAG"
half of this file. assert_readonly_sql is the guard that runs before
ANY generated SQL touches the real connection: a generated statement is
untrusted input same as user input would be, and it only ever needs to
read, so we reject anything else outright rather than trying to sandbox
a write.
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


from pydantic import BaseModel
from datetime import datetime

from models import CompetitorPricePoint, CompetitorPriceHistory
from agents.llm_client import generate_structured, RunCostTracker


class GeneratedSql(BaseModel):
    """What we ask the LLM to produce: just the SQL text. Minimal on
    purpose, same reasoning as models.LLMPriceSuggestion -- we validate
    and execute it ourselves rather than trusting the model with
    anything beyond generating the query string."""

    sql: str


_SQL_PROMPT_TEMPLATE = """You write read-only SQLite queries against this schema:

competitor_prices(sku TEXT, competitor TEXT, price REAL, in_stock INTEGER, date TEXT)

Write a single SELECT statement that returns every row from
competitor_prices where sku is one of: {sku_list}

Return only the SQL in the `sql` field. Do not use INSERT, UPDATE,
DELETE, DROP, ALTER, CREATE, PRAGMA, or multiple statements.
"""


def fetch_batch(
    conn, skus: list[str], cost_tracker: RunCostTracker
) -> dict[str, list[CompetitorPriceHistory]]:
    """One LLM call generates a single SQL query covering every SKU in
    `skus`, instead of one call (or one pandas filter) per SKU -- keeps
    this in line with the existing cost-guard philosophy in
    llm_client.RunCostTracker.
    """
    sku_list = ", ".join(f"'{sku}'" for sku in skus)
    prompt = _SQL_PROMPT_TEMPLATE.format(sku_list=sku_list)

    generated = generate_structured(
        prompt=prompt,
        response_model=GeneratedSql,
        cost_tracker=cost_tracker,
    )
    assert_readonly_sql(generated.sql)

    cur = conn.execute(generated.sql)
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
