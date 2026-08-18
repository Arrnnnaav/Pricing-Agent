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
