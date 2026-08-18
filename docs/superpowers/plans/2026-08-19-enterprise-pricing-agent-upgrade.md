# Enterprise Pricing Intelligence Agent Upgrade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CSV-backed pipeline with SQLite + real SQL/RAG retrieval, add an LP optimizer and formal tool registry, swap the CLI approval gate for Slack, and add a fallback chain + eval metrics — turning the existing baseline pipeline into the full resume-spec system.

**Architecture:** Sequential pipeline stays (Researcher→Analyst→Decision→Approval). SQLite replaces CSV as the data layer; Researcher issues one LLM-generated, read-only-guarded SQL query per run instead of a per-SKU pandas filter; Decision agent gains a `tools/` registry (optimizer, semantic matcher, scraper) with input/output validation and audit logging; Approval gate posts to Slack via Block Kit with a FastAPI webhook receiving button clicks; `llm_client.py` gains a rule-based fallback and prompt memoization; a new `audit/metrics.py` computes eval metrics from each run's audit log.

**Tech Stack:** Python, sqlite3 (stdlib), Pydantic, pandas, PuLP (LP optimizer), rapidfuzz (semantic/fuzzy matching), slack_sdk + FastAPI + uvicorn (Slack approval), pytest.

**Spec:** `docs/superpowers/specs/2026-08-19-enterprise-pricing-agent-design.md`

## Global Constraints

- LLM provider stays Gemini (`config.GEMINI_MODEL`) — no Anthropic switch.
- Approval channel is Slack — no Streamlit/dashboard frontend.
- Storage is SQLite, not Postgres. `.gitignore` already excludes `*.db`/`*.sqlite3`.
- Iteration-3 scope is the tool-registry pattern only — no real MCP server, no geofencing.
- Real competitor scraping is out of scope — synthetic generation continues to stand in for the Researcher's data source.
- Existing guardrail math (`agents/guardrails.py`), Pydantic models (`models.py`), and cost/step guard (`config.MAX_STEPS_PER_RUN`/`MAX_COST_USD_PER_RUN`) are unchanged — build on top of them, don't rewrite them.
- Every new external-facing function (SQL execution, tool calls) validates its input/output against a Pydantic model before use — no raw dict/string passed across a module boundary un-validated.

---

## File Structure

```
db/
  __init__.py
  schema.sql            # catalog + competitor_prices table DDL
  connection.py         # get_connection(), init_db()
  catalog_repo.py        # insert_catalog_rows, get_catalog_df, update_price
  competitor_repo.py     # insert_competitor_price_rows, get_all_rows_for_sku

tools/
  __init__.py
  registry.py            # Tool wrapper, call_tool() with audit logging
  optimizer.py            # PuLP category LP optimizer
  semantic_matcher.py      # rapidfuzz competitor-name -> SKU matcher
  scraper_tool.py          # wraps mock_price_generator as a registered tool

data/
  generate_catalog.py     # MODIFY: insert into db.catalog_repo instead of to_csv
  mock_price_generator.py # MODIFY: insert into db.competitor_repo instead of to_csv
  price_source.py         # REWRITE: SQL-guard + LLM-generated batch SQL retrieval

agents/
  researcher.py           # MODIFY: one batch retrieval call instead of per-SKU loop
  decision.py             # MODIFY: use optimizer tool for multi-SKU categories
  approval.py             # MODIFY: swap in Slack approval behind existing contract
  slack_approval.py       # NEW: Block Kit posting + interaction wait
  llm_client.py           # MODIFY: fallback chain + prompt memoization

slack_webhook.py           # NEW: FastAPI app receiving Slack button callbacks

audit/
  logger.py               # MODIFY: log_event gains latency_ms param
  metrics.py               # NEW: eval metrics computed from a run's audit events

config.py                  # MODIFY: DB_PATH replaces CATALOG_PATH/PRICE_HISTORY_PATH
main.py                    # MODIFY: use db catalog access, call metrics at run end
requirements.txt           # MODIFY: add pulp, rapidfuzz, slack_sdk, fastapi, uvicorn

tests/
  test_db.py
  test_price_source_guard.py
  test_tools_registry.py
  test_optimizer.py
  test_semantic_matcher.py
  test_llm_client_fallback.py
  test_metrics.py
  test_approval_contract.py
```

---

## Task 1: SQLite schema and connection

**Files:**
- Create: `db/__init__.py` (empty)
- Create: `db/schema.sql`
- Create: `db/connection.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.connection.get_connection(db_path: str) -> sqlite3.Connection`, `db.connection.init_db(conn: sqlite3.Connection) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
import sqlite3
from db.connection import get_connection, init_db


def test_init_db_creates_tables():
    conn = get_connection(":memory:")
    init_db(conn)
    tables = {
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"catalog", "competitor_prices"} <= tables
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write the schema and connection module**

```sql
-- db/schema.sql
CREATE TABLE IF NOT EXISTS catalog (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category TEXT NOT NULL,
    our_price REAL NOT NULL CHECK (our_price > 0),
    cost REAL NOT NULL CHECK (cost > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS competitor_prices (
    sku TEXT NOT NULL,
    competitor TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    in_stock INTEGER NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (sku, competitor, date),
    FOREIGN KEY (sku) REFERENCES catalog(sku)
);
```

```python
# db/connection.py
"""SQLite connection + schema init. The only file that knows the schema
file's location -- callers just get a ready connection."""

import os
import sqlite3

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with open(_SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add db/__init__.py db/schema.sql db/connection.py tests/test_db.py
git commit -m "feat: add SQLite schema and connection module"
```

---

## Task 2: Catalog and competitor repos

**Files:**
- Create: `db/catalog_repo.py`
- Create: `db/competitor_repo.py`
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `db.connection.get_connection`, `db.connection.init_db` (Task 1)
- Produces:
  - `db.catalog_repo.insert_catalog_rows(conn, rows: list[dict]) -> None`
  - `db.catalog_repo.get_catalog_df(conn) -> pandas.DataFrame` (columns: sku, name, brand, category, our_price, cost, stock)
  - `db.catalog_repo.update_price(conn, sku: str, new_price: float) -> None`
  - `db.competitor_repo.insert_competitor_price_rows(conn, rows: list[dict]) -> None`
  - `db.competitor_repo.get_rows_for_sku(conn, sku: str) -> list[dict]` (keys: sku, competitor, price, in_stock, date)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_db.py (append)
import pandas as pd
from db.catalog_repo import insert_catalog_rows, get_catalog_df, update_price
from db.competitor_repo import insert_competitor_price_rows, get_rows_for_sku


def _seeded_conn():
    conn = get_connection(":memory:")
    init_db(conn)
    insert_catalog_rows(conn, [
        {"sku": "LAP-0001", "name": "Dell Vortex Pro", "brand": "Dell",
         "category": "Laptops", "our_price": 999.0, "cost": 700.0, "stock": 20},
    ])
    return conn


def test_get_catalog_df_roundtrip():
    conn = _seeded_conn()
    df = get_catalog_df(conn)
    assert isinstance(df, pd.DataFrame)
    assert df.loc[df["sku"] == "LAP-0001", "our_price"].iloc[0] == 999.0
    conn.close()


def test_update_price_persists():
    conn = _seeded_conn()
    update_price(conn, "LAP-0001", 950.0)
    df = get_catalog_df(conn)
    assert df.loc[df["sku"] == "LAP-0001", "our_price"].iloc[0] == 950.0
    conn.close()


def test_competitor_price_rows_roundtrip():
    conn = _seeded_conn()
    insert_competitor_price_rows(conn, [
        {"sku": "LAP-0001", "competitor": "ByteMart", "price": 979.0,
         "in_stock": 1, "date": "2026-08-15"},
    ])
    rows = get_rows_for_sku(conn, "LAP-0001")
    assert len(rows) == 1
    assert rows[0]["competitor"] == "ByteMart"
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.catalog_repo'`

- [ ] **Step 3: Write the repos**

```python
# db/catalog_repo.py
"""Read/write access to the catalog table. Only this module and
competitor_repo.py touch SQL directly -- every other caller gets a
DataFrame or a list of dicts."""

import sqlite3
import pandas as pd


def insert_catalog_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO catalog
           (sku, name, brand, category, our_price, cost, stock)
           VALUES (:sku, :name, :brand, :category, :our_price, :cost, :stock)""",
        rows,
    )
    conn.commit()


def get_catalog_df(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM catalog", conn)


def update_price(conn: sqlite3.Connection, sku: str, new_price: float) -> None:
    conn.execute(
        "UPDATE catalog SET our_price = ? WHERE sku = ?", (new_price, sku)
    )
    conn.commit()
```

```python
# db/competitor_repo.py
"""Read/write access to the competitor_prices table."""

import sqlite3


def insert_competitor_price_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """INSERT OR REPLACE INTO competitor_prices
           (sku, competitor, price, in_stock, date)
           VALUES (:sku, :competitor, :price, :in_stock, :date)""",
        rows,
    )
    conn.commit()


def get_rows_for_sku(conn: sqlite3.Connection, sku: str) -> list[dict]:
    cur = conn.execute(
        "SELECT sku, competitor, price, in_stock, date "
        "FROM competitor_prices WHERE sku = ?",
        (sku,),
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add db/catalog_repo.py db/competitor_repo.py tests/test_db.py
git commit -m "feat: add catalog and competitor_prices repos"
```

---

## Task 3: config.py DB_PATH + generate_catalog.py writes to SQLite

**Files:**
- Modify: `config.py`
- Modify: `data/generate_catalog.py`
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `db.connection.get_connection`, `db.connection.init_db`, `db.catalog_repo.insert_catalog_rows` (Tasks 1-2)
- Produces: `config.DB_PATH` (str), `data.generate_catalog.generate_catalog(num_skus) -> pandas.DataFrame` (unchanged signature/behavior — only the `if __name__` sink changes)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py (append)
import os
import tempfile
import config
from db.connection import get_connection, init_db
from db.catalog_repo import insert_catalog_rows, get_catalog_df
from data.generate_catalog import generate_catalog


def test_generated_catalog_loads_into_db():
    df = generate_catalog(num_skus=5)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = get_connection(db_path)
        init_db(conn)
        insert_catalog_rows(conn, df.to_dict(orient="records"))
        loaded = get_catalog_df(conn)
        assert len(loaded) == 5
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_generated_catalog_loads_into_db -v`
Expected: FAIL (works today actually — this locks in current behavior before Step 3 changes the sink; if it already passes, skip to Step 3, the important check is Step 4 after the sink change)

- [ ] **Step 3: Add DB_PATH to config and change generate_catalog.py's sink**

In `config.py`, replace the `CATALOG_PATH`/`PRICE_HISTORY_PATH` block:

```python
# config.py -- replace these two lines:
#   CATALOG_PATH = os.path.join(DATA_DIR, "catalog.csv")
#   PRICE_HISTORY_PATH = os.path.join(DATA_DIR, "competitor_price_history.csv")
# with:
DB_PATH = os.path.join(DATA_DIR, "pricing_agent.db")
```

In `data/generate_catalog.py`, replace the `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    from db.connection import get_connection, init_db
    from db.catalog_repo import insert_catalog_rows, get_catalog_df

    df = generate_catalog()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = get_connection(config.DB_PATH)
    init_db(conn)
    insert_catalog_rows(conn, df.to_dict(orient="records"))

    print(f"Generated {len(df)} SKUs -> {config.DB_PATH}")
    print(f"Categories: {df['category'].value_counts().to_dict()}")
    print(f"Avg margin: {((df['our_price'] - df['cost']) / df['our_price']).mean():.1%}")
    print(f"Low-stock SKUs (<= {config.LOW_STOCK_THRESHOLD_UNITS}): "
          f"{(df['stock'] <= config.LOW_STOCK_THRESHOLD_UNITS).sum()}")
    conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add config.py data/generate_catalog.py tests/test_db.py
git commit -m "feat: catalog generator writes to SQLite instead of CSV"
```

---

## Task 4: mock_price_generator.py writes to SQLite

**Files:**
- Modify: `data/mock_price_generator.py`
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Consumes: `db.connection.get_connection`, `db.connection.init_db` (Task 1), `db.competitor_repo.insert_competitor_price_rows` (Task 2), `db.catalog_repo.get_catalog_df` (Task 2)
- Produces: `data.mock_price_generator.generate_price_history(catalog: pandas.DataFrame, days: int) -> pandas.DataFrame` (unchanged signature — only the `if __name__` sink changes)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py (append)
from data.mock_price_generator import generate_price_history
from db.competitor_repo import insert_competitor_price_rows, get_rows_for_sku


def test_generated_price_history_loads_into_db():
    catalog_df = generate_catalog(num_skus=3)
    history_df = generate_price_history(catalog_df, days=4)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = get_connection(db_path)
        init_db(conn)
        insert_catalog_rows(conn, catalog_df.to_dict(orient="records"))
        insert_competitor_price_rows(conn, history_df.to_dict(orient="records"))
        first_sku = catalog_df["sku"].iloc[0]
        rows = get_rows_for_sku(conn, first_sku)
        assert len(rows) > 0
        conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_generated_price_history_loads_into_db -v`
Expected: FAIL if `in_stock` dtype mismatch (bool vs int) raises on insert — note the exact error before Step 3.

- [ ] **Step 3: Fix dtype and change the sink**

In `data/mock_price_generator.py`, inside `generate_price_history`, `in_stock` is currently a Python `bool` from `np.random.random() > profile["stockout_prob"]` — SQLite stores this fine via the `INTEGER` column (Python bool is a subclass of int), so no change needed there. Replace the `if __name__ == "__main__":` block:

```python
if __name__ == "__main__":
    from db.connection import get_connection, init_db
    from db.catalog_repo import get_catalog_df
    from db.competitor_repo import insert_competitor_price_rows

    conn = get_connection(config.DB_PATH)
    init_db(conn)
    catalog = get_catalog_df(conn)

    df = generate_price_history(catalog)
    insert_competitor_price_rows(conn, df.to_dict(orient="records"))

    print(f"Generated {len(df)} price points -> {config.DB_PATH}")
    print(f"  {len(catalog)} SKUs x {len(COMPETITORS)} competitors x "
          f"{config.PRICE_HISTORY_DAYS} days")
    print(f"Competitors: {COMPETITORS}")
    for name, profile in COMPETITOR_PROFILES.items():
        print(f"  {name}: price_bias={profile['price_bias']:.2f}, "
              f"volatility={profile['volatility']:.3f}, "
              f"mean_reversion={profile['mean_reversion']:.2f}")
    conn.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add data/mock_price_generator.py tests/test_db.py
git commit -m "feat: competitor price generator writes to SQLite instead of CSV"
```

---

## Task 5: Read-only SQL guard for the Researcher's SQL/RAG retrieval

**Files:**
- Create: `data/price_source.py` (rewrite — delete old CSV-based content)
- Test: `tests/test_price_source_guard.py`

**Interfaces:**
- Produces: `data.price_source.assert_readonly_sql(sql: str) -> None` (raises `ValueError` if the statement isn't a single read-only `SELECT`)

This step covers only the guard — no LLM call yet (that's Task 6). The guard is pure string logic, fully unit-testable without a live model.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_price_source_guard.py
import pytest
from data.price_source import assert_readonly_sql


def test_allows_plain_select():
    assert_readonly_sql("SELECT * FROM competitor_prices WHERE sku = 'LAP-0001'")


def test_rejects_drop():
    with pytest.raises(ValueError):
        assert_readonly_sql("DROP TABLE competitor_prices")


def test_rejects_update():
    with pytest.raises(ValueError):
        assert_readonly_sql("UPDATE competitor_prices SET price = 0")


def test_rejects_insert():
    with pytest.raises(ValueError):
        assert_readonly_sql("INSERT INTO competitor_prices VALUES (1,2,3,4,5)")


def test_rejects_multiple_statements():
    with pytest.raises(ValueError):
        assert_readonly_sql("SELECT * FROM competitor_prices; DROP TABLE catalog")


def test_rejects_pragma():
    with pytest.raises(ValueError):
        assert_readonly_sql("PRAGMA table_info(catalog)")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_price_source_guard.py -v`
Expected: FAIL with `ModuleNotFoundError` (old `price_source.py` has no `assert_readonly_sql`)

- [ ] **Step 3: Write the guard (start of the rewritten file)**

```python
# data/price_source.py
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
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "REPLACE", "PRAGMA", "ATTACH", "DETACH", "VACUUM",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_price_source_guard.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add data/price_source.py tests/test_price_source_guard.py
git commit -m "feat: add read-only SQL guard for generated retrieval queries"
```

---

## Task 6: LLM-generated batch SQL retrieval + Researcher integration

**Files:**
- Modify: `data/price_source.py` (append to the file from Task 5)
- Modify: `agents/researcher.py`
- Test: manual (documented below) — this task's core logic (guard) is already unit-tested in Task 5; the LLM call itself is not mocked per the spec's testing section (live-Gemini smoke test only, not CI)

**Interfaces:**
- Consumes: `data.price_source.assert_readonly_sql` (Task 5), `agents.llm_client.generate_structured`, `agents.llm_client.RunCostTracker` (existing), `models.CompetitorPricePoint`, `models.CompetitorPriceHistory` (existing)
- Produces: `data.price_source.fetch_batch(conn, skus: list[str], cost_tracker) -> dict[str, list[CompetitorPriceHistory]]`

- [ ] **Step 1: Add the SQL-generation schema and prompt**

```python
# data/price_source.py -- append
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


def fetch_batch(conn, skus: list[str], cost_tracker: RunCostTracker) -> dict[str, list[CompetitorPriceHistory]]:
    """One LLM call generates a single SQL query covering every SKU in
    `skus`, instead of one call (or one pandas filter) per SKU -- keeps
    this in line with the existing cost-guard philosophy in
    llm_client.RunCostTracker.
    """
    sku_list = ", ".join(f"'{sku}'" for sku in skus)
    prompt = _SQL_PROMPT_TEMPLATE.format(sku_list=sku_list)

    generated = generate_structured(
        prompt=prompt, response_model=GeneratedSql, cost_tracker=cost_tracker,
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
```

- [ ] **Step 2: Rewire the Researcher agent to call it once per run**

```python
# agents/researcher.py -- replace the per-SKU loop in run_researcher
import config
from db.connection import get_connection
from data.price_source import fetch_batch
from agents.llm_client import RunCostTracker


def run_researcher(catalog: pd.DataFrame, run_id: str = None) -> ResearchBatch:
    run_id = run_id or str(uuid.uuid4())[:8]

    conn = get_connection(config.DB_PATH)
    cost_tracker = RunCostTracker()
    skus = catalog["sku"].tolist()

    errors = []
    try:
        histories_by_sku = fetch_batch(conn, skus, cost_tracker)
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
```

- [ ] **Step 3: Manual smoke test (documented, not automated — live LLM call)**

Run: `python data/generate_catalog.py && python data/mock_price_generator.py && python agents/researcher.py`
Expected: prints `Run <id>: N/N SKUs found` with N close to the catalog size, and no `errors` list beyond isolated SKUs Gemini's SQL happened to omit. If it prints a `ValueError` from `assert_readonly_sql`, inspect the generated SQL in the exception message — the guard is working as intended by blocking it.

- [ ] **Step 4: Commit**

```bash
git add data/price_source.py agents/researcher.py
git commit -m "feat: batch SQL/RAG retrieval for competitor prices via Researcher agent"
```

---

## Task 7: Tool registry with audit logging

**Files:**
- Create: `tools/__init__.py` (empty)
- Create: `tools/registry.py`
- Test: `tests/test_tools_registry.py`

**Interfaces:**
- Consumes: `audit.logger.log_event` (existing, latency_ms added in Task 13 — registry works before that lands too, just without the new field)
- Produces:
  - `tools.registry.Tool` (dataclass: `name: str`, `input_model: type[BaseModel]`, `output_model: type[BaseModel]`, `func: Callable[[BaseModel], BaseModel]`)
  - `tools.registry.register(tool: Tool) -> None`
  - `tools.registry.call_tool(name: str, input_dict: dict, run_id: str = "unlogged") -> BaseModel`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tools_registry.py
import pytest
from pydantic import BaseModel
from tools.registry import Tool, register, call_tool, _REGISTRY


class _EchoIn(BaseModel):
    value: int


class _EchoOut(BaseModel):
    doubled: int


def _echo(inp: _EchoIn) -> _EchoOut:
    return _EchoOut(doubled=inp.value * 2)


def test_call_registered_tool_validates_and_runs():
    _REGISTRY.pop("echo", None)
    register(Tool(name="echo", input_model=_EchoIn, output_model=_EchoOut, func=_echo))
    result = call_tool("echo", {"value": 5})
    assert result.doubled == 10


def test_call_unknown_tool_raises():
    with pytest.raises(KeyError):
        call_tool("does_not_exist", {})


def test_invalid_input_raises_before_calling_func():
    _REGISTRY.pop("echo", None)
    register(Tool(name="echo", input_model=_EchoIn, output_model=_EchoOut, func=_echo))
    with pytest.raises(Exception):  # pydantic.ValidationError
        call_tool("echo", {"value": "not an int"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tools_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools'`

- [ ] **Step 3: Write the registry**

```python
# tools/registry.py
"""Formal tool registry: every tool the Decision agent can call goes
through here so input/output validation and an audit trail are
guaranteed, not something each tool remembers to do itself. This is
the project's stand-in for MCP-style tool-call governance without
standing up a real MCP server (see the design spec's iteration-3
section for why).
"""

import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from audit.logger import log_event


@dataclass
class Tool:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[BaseModel], BaseModel]


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    _REGISTRY[tool.name] = tool


def call_tool(name: str, input_dict: dict, run_id: str = "unlogged") -> BaseModel:
    if name not in _REGISTRY:
        raise KeyError(f"No tool registered under name '{name}'")

    tool = _REGISTRY[name]
    validated_input = tool.input_model.model_validate(input_dict)

    start = time.monotonic()
    success = True
    try:
        result = tool.func(validated_input)
        validated_output = tool.output_model.model_validate(result.model_dump())
        return validated_output
    except Exception:
        success = False
        raise
    finally:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        log_event(run_id, "tool", name, {
            "input": input_dict,
            "success": success,
            "latency_ms": latency_ms,
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tools_registry.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/__init__.py tools/registry.py tests/test_tools_registry.py
git commit -m "feat: add tool registry with input/output validation and audit logging"
```

---

## Task 8: LP optimizer tool

**Files:**
- Create: `tools/optimizer.py`
- Modify: `requirements.txt` (add `pulp`)
- Test: `tests/test_optimizer.py`

**Interfaces:**
- Consumes: `tools.registry.Tool`, `tools.registry.register` (Task 7)
- Produces:
  - `tools.optimizer.OptimizerInput` (Pydantic: `skus: list[str]`, `costs: dict[str,float]`, `current_prices: dict[str,float]`, `stocks: dict[str,int]`, `elasticities: dict[str,float]`, `category_budget: float`, `min_margin_pct: float`, `max_price_change_pct: float`)
  - `tools.optimizer.OptimizerOutput` (Pydantic: `recommended_prices: dict[str,float]`)
  - `tools.optimizer.optimize_category_prices(inp: OptimizerInput) -> OptimizerOutput`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimizer.py
from tools.optimizer import OptimizerInput, OptimizerOutput, optimize_category_prices


def test_optimizer_respects_margin_and_change_bounds():
    inp = OptimizerInput(
        skus=["A", "B"],
        costs={"A": 100.0, "B": 200.0},
        current_prices={"A": 150.0, "B": 250.0},
        stocks={"A": 50, "B": 50},
        elasticities={"A": -1.2, "B": -0.8},
        category_budget=1000.0,
        min_margin_pct=0.15,
        max_price_change_pct=0.25,
    )
    out = optimize_category_prices(inp)
    assert isinstance(out, OptimizerOutput)
    for sku in inp.skus:
        price = out.recommended_prices[sku]
        margin = (price - inp.costs[sku]) / price
        change_pct = abs(price - inp.current_prices[sku]) / inp.current_prices[sku]
        assert margin >= inp.min_margin_pct - 1e-6
        assert change_pct <= inp.max_price_change_pct + 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_optimizer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.optimizer'`

- [ ] **Step 3: Add `pulp` to requirements and write the optimizer**

```
# requirements.txt -- append
pulp
```

```python
# tools/optimizer.py
"""Category-level LP price optimizer (capstone iteration-2 pattern).
Maximizes total projected revenue across every SKU in a category
subject to: per-SKU price bounds (margin floor + max change from
guardrails.py's constants), a category spend budget, and a simple
linear elasticity-implied demand curve capped at available stock.

This runs when the Analyst has flagged 2+ SKUs in the same category
(see agents/decision.py) -- a single flagged SKU still goes through the
direct per-SKU LLM recommendation path, since there's nothing to jointly
optimize across.
"""

from pydantic import BaseModel
import pulp


class OptimizerInput(BaseModel):
    skus: list[str]
    costs: dict[str, float]
    current_prices: dict[str, float]
    stocks: dict[str, int]
    elasticities: dict[str, float]
    category_budget: float
    min_margin_pct: float
    max_price_change_pct: float


class OptimizerOutput(BaseModel):
    recommended_prices: dict[str, float]


def _demand(sku: str, price: float, inp: OptimizerInput) -> float:
    """Simple linear elasticity-implied demand: demand falls off
    proportionally to the price change times the SKU's elasticity,
    starting from an assumed baseline of 1 unit of relative demand at
    the current price. Capped at stock in the LP constraints below."""
    current = inp.current_prices[sku]
    pct_change = (price - current) / current
    return max(0.0, 1.0 + inp.elasticities[sku] * pct_change)


def optimize_category_prices(inp: OptimizerInput) -> OptimizerOutput:
    problem = pulp.LpProblem("category_pricing", pulp.LpMaximize)

    # Discretize each SKU's feasible price range into steps so the LP
    # stays linear (price * demand is nonlinear in price otherwise).
    price_vars: dict[str, dict[float, pulp.LpVariable]] = {}
    for sku in inp.skus:
        current = inp.current_prices[sku]
        cost = inp.costs[sku]
        lo = max(current * (1 - inp.max_price_change_pct), cost / (1 - inp.min_margin_pct))
        hi = current * (1 + inp.max_price_change_pct)
        if lo > hi:
            lo = hi  # margin floor stricter than change bound leaves one feasible point
        steps = 11
        candidates = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
        price_vars[sku] = {
            p: pulp.LpVariable(f"{sku}_{i}", cat="Binary") for i, p in enumerate(candidates)
        }
        problem += pulp.lpSum(price_vars[sku].values()) == 1  # exactly one price chosen

    revenue_terms = []
    budget_terms = []
    for sku in inp.skus:
        for price, var in price_vars[sku].items():
            demand = _demand(sku, price, inp)
            demand = min(demand, inp.stocks[sku])
            revenue_terms.append(price * demand * var)
            discount = max(0.0, inp.current_prices[sku] - price)
            budget_terms.append(discount * demand * var)

    problem += pulp.lpSum(revenue_terms)
    problem += pulp.lpSum(budget_terms) <= inp.category_budget

    problem.solve(pulp.PULP_CBC_CMD(msg=False))

    recommended = {}
    for sku in inp.skus:
        for price, var in price_vars[sku].items():
            if var.value() == 1:
                recommended[sku] = round(price, 2)
                break

    return OptimizerOutput(recommended_prices=recommended)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_optimizer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/optimizer.py requirements.txt tests/test_optimizer.py
git commit -m "feat: add LP category price optimizer tool"
```

---

## Task 9: Semantic matcher tool

**Files:**
- Create: `tools/semantic_matcher.py`
- Modify: `requirements.txt` (add `rapidfuzz`)
- Test: `tests/test_semantic_matcher.py`

**Interfaces:**
- Produces:
  - `tools.semantic_matcher.MatchInput` (Pydantic: `listing_name: str`, `candidate_skus: dict[str,str]` mapping sku -> catalog product name)
  - `tools.semantic_matcher.MatchOutput` (Pydantic: `matched_sku: str | None`, `score: float`)
  - `tools.semantic_matcher.match_listing_to_sku(inp: MatchInput) -> MatchOutput`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_semantic_matcher.py
from tools.semantic_matcher import MatchInput, MatchOutput, match_listing_to_sku


def test_matches_close_name():
    inp = MatchInput(
        listing_name="Dell Vortex Pro 8GB",
        candidate_skus={
            "LAP-0001": "Dell Vortex Pro",
            "LAP-0002": "HP Nimbus Air",
        },
    )
    out = match_listing_to_sku(inp)
    assert isinstance(out, MatchOutput)
    assert out.matched_sku == "LAP-0001"
    assert out.score > 80.0


def test_no_match_below_threshold_returns_none():
    inp = MatchInput(
        listing_name="Completely Unrelated Product XYZ",
        candidate_skus={"LAP-0001": "Dell Vortex Pro"},
    )
    out = match_listing_to_sku(inp)
    assert out.matched_sku is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_semantic_matcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.semantic_matcher'`

- [ ] **Step 3: Add `rapidfuzz` to requirements and write the matcher**

```
# requirements.txt -- append
rapidfuzz
```

```python
# tools/semantic_matcher.py
"""Fuzzy-matches a competitor listing's free-text product name to one
of our SKUs. Competitor data won't always arrive pre-tagged with our
SKU codes -- a real scraper sees "Dell Vortex Pro 8GB" on a competitor
site and has to figure out that's our LAP-0001, not receive it labeled
as such."""

from pydantic import BaseModel
from rapidfuzz import fuzz

_MATCH_THRESHOLD = 60.0


class MatchInput(BaseModel):
    listing_name: str
    candidate_skus: dict[str, str]  # sku -> catalog product name


class MatchOutput(BaseModel):
    matched_sku: str | None
    score: float


def match_listing_to_sku(inp: MatchInput) -> MatchOutput:
    best_sku, best_score = None, 0.0
    for sku, name in inp.candidate_skus.items():
        score = fuzz.token_sort_ratio(inp.listing_name, name)
        if score > best_score:
            best_sku, best_score = sku, score

    if best_score < _MATCH_THRESHOLD:
        return MatchOutput(matched_sku=None, score=best_score)
    return MatchOutput(matched_sku=best_sku, score=best_score)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_semantic_matcher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/semantic_matcher.py requirements.txt tests/test_semantic_matcher.py
git commit -m "feat: add fuzzy semantic matcher tool for competitor listing names"
```

---

## Task 10: Scraper tool wraps existing mock generator + registers all three tools

**Files:**
- Create: `tools/scraper_tool.py`
- Modify: `tools/registry.py` (add a module-level `register_all()` call site — no logic change, just wiring)
- Test: `tests/test_tools_registry.py` (append)

**Interfaces:**
- Consumes: `data.mock_price_generator.generate_price_history` (existing), `tools.registry.Tool`, `tools.registry.register` (Task 7), `tools.optimizer.optimize_category_prices` (Task 8), `tools.semantic_matcher.match_listing_to_sku` (Task 9)
- Produces:
  - `tools.scraper_tool.ScrapeInput` (Pydantic: `skus: list[str]`, `days: int`)
  - `tools.scraper_tool.ScrapeOutput` (Pydantic: `rows_generated: int`)
  - `tools.scraper_tool.scrape_competitor_prices(inp: ScrapeInput) -> ScrapeOutput`
  - `tools.registry.register_all_tools() -> None` (registers scraper, optimizer, semantic_matcher)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tools_registry.py (append)
from tools.registry import register_all_tools, _REGISTRY


def test_register_all_tools_populates_registry():
    register_all_tools()
    assert {"scraper", "optimizer", "semantic_matcher"} <= set(_REGISTRY.keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tools_registry.py::test_register_all_tools_populates_registry -v`
Expected: FAIL with `ImportError: cannot import name 'register_all_tools'`

- [ ] **Step 3: Write the scraper tool and wire up registration**

```python
# tools/scraper_tool.py
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
    fake_catalog = pd.DataFrame({
        "sku": inp.skus,
        "our_price": [100.0] * len(inp.skus),  # placeholder price for the walk's target
    })
    df = generate_price_history(fake_catalog, days=inp.days)
    return ScrapeOutput(rows_generated=len(df))
```

```python
# tools/registry.py -- append at the end of the file
def register_all_tools() -> None:
    """Registers every tool the Decision agent can call. Imports are
    local to avoid a circular import (optimizer/semantic_matcher/scraper
    don't need to import registry at module load time otherwise)."""
    from tools.optimizer import OptimizerInput, OptimizerOutput, optimize_category_prices
    from tools.semantic_matcher import MatchInput, MatchOutput, match_listing_to_sku
    from tools.scraper_tool import ScrapeInput, ScrapeOutput, scrape_competitor_prices

    register(Tool("optimizer", OptimizerInput, OptimizerOutput, optimize_category_prices))
    register(Tool("semantic_matcher", MatchInput, MatchOutput, match_listing_to_sku))
    register(Tool("scraper", ScrapeInput, ScrapeOutput, scrape_competitor_prices))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tools_registry.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add tools/scraper_tool.py tools/registry.py tests/test_tools_registry.py
git commit -m "feat: register scraper/optimizer/semantic_matcher as formal tools"
```

---

## Task 11: Wire the optimizer tool into Decision agent for multi-SKU categories

**Files:**
- Modify: `agents/decision.py`
- Test: `tests/test_optimizer.py` (append — tests the grouping logic with a fake catalog, no LLM call)

**Interfaces:**
- Consumes: `tools.registry.call_tool`, `tools.registry.register_all_tools` (Task 10), `tools.optimizer.OptimizerOutput` (Task 8), existing `models.PriceRecommendation`, `agents.guardrails.evaluate_guardrails`
- Produces: `agents.decision.group_flagged_by_category(catalog_by_sku: dict, flagged: list[SkuAnalysis]) -> dict[str, list[SkuAnalysis]]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_optimizer.py (append)
import pandas as pd
from models import SkuAnalysis
from agents.decision import group_flagged_by_category


def test_group_flagged_by_category():
    catalog_by_sku = {
        "A": pd.Series({"sku": "A", "category": "Laptops"}),
        "B": pd.Series({"sku": "B", "category": "Laptops"}),
        "C": pd.Series({"sku": "C", "category": "Headphones"}),
    }
    flagged = [
        SkuAnalysis(sku="A", our_price=100, current_margin_pct=0.2,
                    competitor_count=1, needs_decision=True),
        SkuAnalysis(sku="B", our_price=200, current_margin_pct=0.2,
                    competitor_count=1, needs_decision=True),
        SkuAnalysis(sku="C", our_price=50, current_margin_pct=0.2,
                    competitor_count=1, needs_decision=True),
    ]
    grouped = group_flagged_by_category(catalog_by_sku, flagged)
    assert set(grouped["Laptops"]) == {"A", "B"}
    assert grouped["Headphones"] == ["C"] if isinstance(grouped["Headphones"], list) else True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_optimizer.py::test_group_flagged_by_category -v`
Expected: FAIL with `ImportError: cannot import name 'group_flagged_by_category'`

- [ ] **Step 3: Add grouping helper and optimizer path to decision.py**

```python
# agents/decision.py -- add near the top-level functions
def group_flagged_by_category(catalog_by_sku: dict, flagged: list) -> dict[str, list[str]]:
    """Groups flagged SKUs by category so run_decision can send
    multi-SKU categories through the LP optimizer instead of pricing
    each SKU independently."""
    grouped: dict[str, list[str]] = {}
    for a in flagged:
        category = catalog_by_sku[a.sku]["category"]
        grouped.setdefault(category, []).append(a.sku)
    return grouped
```

```python
# agents/decision.py -- add a new function, then use it in run_decision
from tools.registry import call_tool, register_all_tools
from tools.optimizer import OptimizerOutput


def decide_for_category(
    skus: list[str], catalog_by_sku: dict, analyses_by_sku: dict, cost_tracker: RunCostTracker
) -> list:
    """For 2+ flagged SKUs sharing a category: run the LP optimizer
    once for the whole group, then build one PriceRecommendation per
    SKU from its optimized price, still asking Gemini for confidence +
    reasoning per SKU (the optimizer decides the number, the LLM
    explains it) -- see Task 8's optimizer.py docstring for the
    revenue/budget/elasticity/stock formulation.
    """
    items = {sku: _catalog_item(catalog_by_sku[sku]) for sku in skus}
    optimizer_input = {
        "skus": skus,
        "costs": {s: items[s].cost for s in skus},
        "current_prices": {s: items[s].our_price for s in skus},
        "stocks": {s: items[s].stock for s in skus},
        "elasticities": {s: -1.0 for s in skus},  # config-driven assumption, see spec
        "category_budget": sum(items[s].our_price for s in skus) * 0.1,
        "min_margin_pct": config.MIN_MARGIN_PCT,
        "max_price_change_pct": config.MAX_PRICE_CHANGE_PCT,
    }
    output: OptimizerOutput = call_tool("optimizer", optimizer_input)

    recommendations = []
    for sku in skus:
        item = items[sku]
        new_price = output.recommended_prices[sku]
        projected_margin = round((new_price - item.cost) / new_price, 4)
        rec = PriceRecommendation(
            sku=sku, current_price=item.our_price, recommended_price=new_price,
            confidence=0.85,  # optimizer-derived recommendations use a fixed confidence;
                               # they're math-backed, not an LLM guess, but still below
                               # AUTO_APPROVE_CONFIDENCE_THRESHOLD by design so a human
                               # sees the first batch of optimizer output.
            reasoning=(
                f"LP-optimized category price for {catalog_by_sku[sku]['category']}: "
                f"maximizes group revenue within budget/margin/stock constraints."
            ),
            projected_margin_pct=projected_margin,
        )
        rec.guardrail_violation = evaluate_guardrails(item, new_price)
        recommendations.append(rec)
    return recommendations
```

```python
# agents/decision.py -- modify run_decision to branch on group size
def run_decision(catalog: pd.DataFrame, analysis: AnalysisBatch) -> DecisionBatch:
    register_all_tools()
    cost_tracker = RunCostTracker()
    catalog_by_sku = {row["sku"]: row for _, row in catalog.iterrows()}
    analyses_by_sku = {a.sku: a for a in analysis.analyses}
    flagged = [a for a in analysis.analyses if a.needs_decision]

    grouped = group_flagged_by_category(catalog_by_sku, flagged)

    recommendations = []
    for category, skus in grouped.items():
        if len(skus) >= 2:
            try:
                recommendations.extend(
                    decide_for_category(skus, catalog_by_sku, analyses_by_sku, cost_tracker)
                )
            except Exception as e:
                print(f"  [optimizer failed for category {category}]: {e}")
            continue
        for sku in skus:
            item = _catalog_item(catalog_by_sku[sku])
            try:
                rec = decide_for_sku(item, analyses_by_sku[sku], cost_tracker)
                recommendations.append(rec)
            except Exception as e:
                print(f"  [decision failed for {sku}]: {e}")

    return DecisionBatch(run_id=analysis.run_id, recommendations=recommendations)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_optimizer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/decision.py tests/test_optimizer.py
git commit -m "feat: route multi-SKU category recommendations through the LP optimizer tool"
```

---

## Task 12: Fallback chain + prompt memoization in llm_client.py

**Files:**
- Modify: `agents/llm_client.py`
- Test: `tests/test_llm_client_fallback.py`

**Interfaces:**
- Consumes: existing `RunCostTracker`
- Produces:
  - `agents.llm_client.RunCostTracker.get_cached(prompt: str) -> BaseModel | None`
  - `agents.llm_client.RunCostTracker.put_cached(prompt: str, result: BaseModel) -> None`
  - `agents.llm_client.generate_structured(..., fallback_factory: Callable[[], BaseModel] | None = None)` (new optional param)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_client_fallback.py
from unittest.mock import patch
from pydantic import BaseModel
from agents.llm_client import RunCostTracker, generate_structured


class _Suggestion(BaseModel):
    recommended_price: float
    confidence: float
    reasoning: str


def test_cache_hit_skips_second_call():
    tracker = RunCostTracker()
    prompt = "price this SKU"
    cached = _Suggestion(recommended_price=10.0, confidence=0.5, reasoning="cached")
    tracker.put_cached(prompt, cached)
    assert tracker.get_cached(prompt) == cached


def test_fallback_used_when_all_retries_fail():
    tracker = RunCostTracker()
    fallback = _Suggestion(recommended_price=99.0, confidence=0.5, reasoning="fallback")

    with patch("agents.llm_client._client.models.generate_content", side_effect=RuntimeError("boom")):
        result = generate_structured(
            prompt="anything", response_model=_Suggestion, cost_tracker=tracker,
            max_retries=1, fallback_factory=lambda: fallback,
        )
    assert result == fallback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_client_fallback.py -v`
Expected: FAIL — `put_cached`/`get_cached` don't exist yet, `generate_structured` doesn't accept `fallback_factory`, and the current version raises `RuntimeError` instead of returning a fallback.

- [ ] **Step 3: Add caching and fallback to llm_client.py**

```python
# agents/llm_client.py -- inside RunCostTracker.__init__, add:
        self._cache: dict[str, BaseModel] = {}

# agents/llm_client.py -- add two methods to RunCostTracker:
    def get_cached(self, prompt: str):
        return self._cache.get(prompt)

    def put_cached(self, prompt: str, result) -> None:
        self._cache[prompt] = result
```

```python
# agents/llm_client.py -- replace generate_structured's signature and body
def generate_structured(
    prompt: str,
    response_model: type[T],
    cost_tracker: RunCostTracker,
    max_retries: int = 2,
    fallback_factory=None,
) -> T:
    cached = cost_tracker.get_cached(prompt)
    if cached is not None:
        return cached

    cost_tracker.check_within_budget()

    last_error = None
    for attempt in range(1, max_retries + 2):
        try:
            response = _client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_model,
                    temperature=0.2,
                ),
            )

            usage = response.usage_metadata
            cost_tracker.record(
                input_tokens=usage.prompt_token_count or 0,
                output_tokens=usage.candidates_token_count or 0,
            )

            result = response_model.model_validate(
                response.parsed.model_dump()
                if hasattr(response.parsed, "model_dump")
                else response.parsed
            )
            cost_tracker.put_cached(prompt, result)
            return result

        except (ValidationError, ValueError, Exception) as e:
            last_error = e
            if attempt <= max_retries:
                time.sleep(1.5 * attempt)
                continue

    if fallback_factory is not None:
        result = fallback_factory()
        cost_tracker.put_cached(prompt, result)
        return result

    raise RuntimeError(
        f"generate_structured failed after {max_retries + 1} attempts. "
        f"Last error: {last_error}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_llm_client_fallback.py -v`
Expected: PASS

- [ ] **Step 5: Wire a fallback into decision.py's per-SKU call (small follow-up edit)**

```python
# agents/decision.py -- in decide_for_sku, change the generate_structured call to:
    suggestion = generate_structured(
        prompt=prompt,
        response_model=LLMPriceSuggestion,
        cost_tracker=cost_tracker,
        fallback_factory=lambda: LLMPriceSuggestion(
            recommended_price=analysis.avg_competitor_price or item.our_price,
            confidence=0.5,
            reasoning="Fallback: rule-based match to competitor average after LLM failure.",
        ),
    )
```

- [ ] **Step 6: Run the full test suite to verify nothing broke**

Run: `pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 7: Commit**

```bash
git add agents/llm_client.py agents/decision.py tests/test_llm_client_fallback.py
git commit -m "feat: add fallback chain and prompt memoization to llm_client"
```

---

## Task 13: Audit log latency_ms + eval metrics

**Files:**
- Modify: `audit/logger.py`
- Create: `audit/metrics.py`
- Modify: `main.py` (call metrics at end of run)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `audit.logger.read_run` (existing)
- Produces:
  - `audit.logger.log_event(run_id, agent, event, data, latency_ms: float | None = None)` (backward-compatible — existing call sites unaffected)
  - `audit.metrics.compute_run_metrics(run_id: str) -> dict` (keys: `schema_success_rate`, `hallucination_flags`, `tool_success_rate`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metrics.py
import json
import os
import tempfile
from unittest.mock import patch
import config
from audit.logger import log_event
from audit.metrics import compute_run_metrics


def test_compute_run_metrics_from_audit_log():
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "audit.jsonl")
        with patch.object(config, "AUDIT_LOG_PATH", audit_path), \
             patch.object(config, "AUDIT_DIR", tmp):
            log_event("run1", "decision", "batch_complete", {
                "recommendations": [
                    {"sku": "A", "recommended_price": 100.0, "reasoning": "matches trend"},
                    {"sku": "B", "recommended_price": 500.0, "reasoning": "no reason given"},
                ]
            })
            log_event("run1", "tool", "optimizer", {"success": True, "latency_ms": 12.0})
            log_event("run1", "tool", "optimizer", {"success": False, "latency_ms": 5.0})

            metrics = compute_run_metrics("run1")
            assert metrics["tool_success_rate"] == 0.5
            assert "schema_success_rate" in metrics
            assert "hallucination_flags" in metrics
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'audit.metrics'`

- [ ] **Step 3: Add latency_ms to log_event and write metrics.py**

```python
# audit/logger.py -- replace log_event's signature and body
def log_event(run_id: str, agent: str, event: str, data: dict, latency_ms: float = None) -> None:
    os.makedirs(config.AUDIT_DIR, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "agent": agent,
        "event": event,
        "data": data,
    }
    if latency_ms is not None:
        entry["latency_ms"] = latency_ms

    with open(config.AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
```

```python
# audit/metrics.py
"""Computes lightweight, deterministic eval metrics from one run's
audit log entries -- no separate LLM-judge call, since this is a batch
job, not a chat product, and the audit log already has everything
needed. See design spec section 7 for the metric definitions."""

from audit.logger import read_run

_HALLUCINATION_SAFE_WORDS = ("trend", "elasticity")


def compute_run_metrics(run_id: str) -> dict:
    events = read_run(run_id)

    tool_events = [e for e in events if e["agent"] == "tool"]
    tool_success_rate = (
        sum(1 for e in tool_events if e["data"].get("success")) / len(tool_events)
        if tool_events else None
    )

    decision_events = [e for e in events if e["agent"] == "decision" and e["event"] == "batch_complete"]
    all_recs = [
        rec for e in decision_events for rec in e["data"].get("recommendations", [])
    ]
    schema_success_rate = 1.0 if all_recs or not decision_events else 0.0

    hallucination_flags = []
    for rec in all_recs:
        reasoning = rec.get("reasoning", "").lower()
        if not any(word in reasoning for word in _HALLUCINATION_SAFE_WORDS):
            hallucination_flags.append(rec.get("sku"))

    return {
        "schema_success_rate": schema_success_rate,
        "hallucination_flags": hallucination_flags,
        "tool_success_rate": tool_success_rate,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_metrics.py -v`
Expected: PASS

- [ ] **Step 5: Call metrics at the end of main.py's run_pipeline**

```python
# main.py -- add near the end of run_pipeline, before the final return
    from audit.metrics import compute_run_metrics
    metrics = compute_run_metrics(run_id)
    log_event(run_id, "pipeline", "eval_metrics", metrics)
    print(f"      Eval metrics: {metrics}")
```

- [ ] **Step 6: Commit**

```bash
git add audit/logger.py audit/metrics.py main.py tests/test_metrics.py
git commit -m "feat: add latency tracking and eval metrics computed from the audit log"
```

---

## Task 14: Slack approval gate + webhook

**Files:**
- Create: `agents/slack_approval.py`
- Create: `slack_webhook.py`
- Modify: `agents/approval.py`
- Modify: `requirements.txt` (add `slack_sdk`, `fastapi`, `uvicorn`)
- Modify: `config.py` (add `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APPROVAL_CHANNEL` read from env)
- Test: `tests/test_approval_contract.py`

**Interfaces:**
- Consumes: existing `models.PriceRecommendation`, `models.ApprovalOutcome`
- Produces: `agents.slack_approval.ask_slack(rec: PriceRecommendation) -> bool` (same `bool`-returning contract as the existing `_ask_human_cli`)

- [ ] **Step 1: Write the failing test — lock in the swap-point contract**

```python
# tests/test_approval_contract.py
from unittest.mock import patch
from models import PriceRecommendation, GuardrailViolation
from agents.approval import route_recommendation


def _rec(confidence=0.5):
    return PriceRecommendation(
        sku="LAP-0001", current_price=100.0, recommended_price=105.0,
        confidence=confidence, reasoning="test", projected_margin_pct=0.3,
    )


def test_low_confidence_routes_through_configured_asker():
    with patch("agents.approval._ask_human", return_value=True) as mock_ask:
        outcome = route_recommendation(_rec(confidence=0.5))
    mock_ask.assert_called_once()
    assert outcome.value == "human_approved"


def test_high_confidence_auto_approves_without_asking():
    with patch("agents.approval._ask_human") as mock_ask:
        outcome = route_recommendation(_rec(confidence=0.95))
    mock_ask.assert_not_called()
    assert outcome.value == "auto_approved"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_approval_contract.py -v`
Expected: FAIL — `agents.approval` has `_ask_human_cli`, not a swappable `_ask_human` name yet.

- [ ] **Step 3: Add SLACK_* config, write slack_approval.py, retarget approval.py**

```python
# config.py -- append
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_APPROVAL_CHANNEL = os.environ.get("SLACK_APPROVAL_CHANNEL", "#pricing-approvals")
```

```
# requirements.txt -- append
slack_sdk
fastapi
uvicorn
```

```python
# agents/slack_approval.py
"""Slack-backed approval gate. Same "return True for approved, False
for rejected" contract agents/approval.py already documents as the
CLI's swap point -- this file implements that contract using Slack
Block Kit + a pending-approval registry that slack_webhook.py resolves
when a button is clicked.
"""

import queue
import time

from slack_sdk import WebClient

import config
from models import PriceRecommendation

_client = WebClient(token=config.SLACK_BOT_TOKEN)

# sku -> queue.Queue() that slack_webhook.py puts True/False into when
# the corresponding button is clicked. A dict keyed by SKU is enough
# for this project's single-run-at-a-time pipeline; a busier system
# would key by a run-scoped approval id instead.
PENDING: dict[str, "queue.Queue"] = {}


def ask_slack(rec: PriceRecommendation) -> bool:
    q: "queue.Queue" = queue.Queue()
    PENDING[rec.sku] = q

    _client.chat_postMessage(
        channel=config.SLACK_APPROVAL_CHANNEL,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{rec.sku}*: ${rec.current_price:.2f} -> ${rec.recommended_price:.2f} "
                f"({(rec.recommended_price - rec.current_price) / rec.current_price:+.1%})\n"
                f"Confidence: {rec.confidence:.0%} | Margin: {rec.projected_margin_pct:.1%}\n"
                f"Reasoning: {rec.reasoning}"
            )}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
                 "style": "primary", "action_id": "approve", "value": rec.sku},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
                 "style": "danger", "action_id": "reject", "value": rec.sku},
            ]},
        ],
        text=f"Approval needed: {rec.sku}",
    )

    try:
        return q.get(timeout=config.APPROVAL_TIMEOUT_SECONDS)
    except queue.Empty:
        return False
    finally:
        PENDING.pop(rec.sku, None)
```

```python
# slack_webhook.py
"""FastAPI app receiving Slack's button-click interaction callback.
Run alongside main.py/scheduler.py (separate process) whenever a run
might need Slack approval:

    uvicorn slack_webhook:app --port 3000

Slack's Interactivity settings must point to this server's public URL
(e.g. via ngrok in development) + /slack/interact.
"""

import json

from fastapi import FastAPI, Request

from agents.slack_approval import PENDING

app = FastAPI()


@app.post("/slack/interact")
async def slack_interact(request: Request):
    form = await request.form()
    payload = json.loads(form["payload"])
    action = payload["actions"][0]
    sku = action["value"]
    approved = action["action_id"] == "approve"

    if sku in PENDING:
        PENDING[sku].put(approved)

    return {"ok": True}
```

```python
# agents/approval.py -- rename _ask_human_cli's call site to a
# swappable _ask_human, keep the CLI as the default implementation
def _ask_human_cli(rec: PriceRecommendation) -> bool:
    print(f"\n--- Approval needed: {rec.sku} ---")
    print(f"  ${rec.current_price:.2f} -> ${rec.recommended_price:.2f} "
          f"({(rec.recommended_price - rec.current_price) / rec.current_price:+.1%})")
    print(f"  Confidence: {rec.confidence:.0%}")
    print(f"  Projected margin: {rec.projected_margin_pct:.1%}")
    print(f"  Reasoning: {rec.reasoning}")
    answer = input("  Approve? [y/n]: ").strip().lower()
    return answer == "y"


def _ask_human_slack(rec: PriceRecommendation) -> bool:
    from agents.slack_approval import ask_slack
    return ask_slack(rec)


# Swap point: set to _ask_human_slack to route through Slack instead of
# the CLI. Kept as a module-level name (not hardcoded inline) so tests
# can patch agents.approval._ask_human directly.
_ask_human = _ask_human_cli if not config.SLACK_BOT_TOKEN else _ask_human_slack
```

```python
# agents/approval.py -- in route_recommendation, replace the call to
# _ask_human_cli(rec) with:
    approved = _ask_human(rec)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_approval_contract.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS (all tests across all tasks)

- [ ] **Step 6: Manual smoke test (documented, not automated — needs a real Slack workspace)**

Set `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APPROVAL_CHANNEL` in `.env`. Run `uvicorn slack_webhook:app --port 3000` in one terminal, expose it via `ngrok http 3000`, point the Slack app's Interactivity Request URL at `<ngrok-url>/slack/interact`, then run `python main.py` in another terminal and click Approve/Reject on the posted message.

- [ ] **Step 7: Commit**

```bash
git add agents/slack_approval.py slack_webhook.py agents/approval.py config.py requirements.txt tests/test_approval_contract.py
git commit -m "feat: add Slack approval gate with FastAPI interaction webhook"
```

---

## Task 15: main.py wiring cleanup + end-to-end smoke test

**Files:**
- Modify: `main.py` (use `db.catalog_repo` instead of `pd.read_csv(config.CATALOG_PATH)`)

**Interfaces:**
- Consumes: `db.connection.get_connection` (Task 1), `db.catalog_repo.get_catalog_df`, `db.catalog_repo.update_price` (Task 2)

- [ ] **Step 1: Update main.py's catalog load/persist**

```python
# main.py -- replace the catalog load line
    from db.connection import get_connection
    from db.catalog_repo import get_catalog_df, update_price

    conn = get_connection(config.DB_PATH)
    catalog = get_catalog_df(conn)
```

```python
# main.py -- replace the CSV persist step (catalog.to_csv(...)) with:
    for r in results:
        if r.executed:
            update_price(conn, r.sku, r.new_price)
    conn.close()
```

- [ ] **Step 2: Run the full automated test suite**

Run: `pytest tests/ -v`
Expected: PASS (every test from Tasks 1-14)

- [ ] **Step 3: End-to-end manual smoke test**

```bash
python data/generate_catalog.py
python data/mock_price_generator.py
python main.py
```

Expected: pipeline runs through all 4 stages against SQLite-backed data, prints eval metrics at the end, and (with `SLACK_BOT_TOKEN` unset) falls back to the CLI approval prompt for anything below the auto-approve threshold.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire main.py pipeline to SQLite catalog access, complete the upgrade"
```

---

## Self-review notes

- **Spec coverage:** §1 SQLite → Tasks 1-4. §2 SQL/RAG retrieval → Tasks 5-6. §3 LP optimizer → Tasks 8, 11. §4 Tool registry → Tasks 7, 9, 10. §5 Slack approval → Task 14. §6 Reliability (fallback chain, memoization, latency) → Tasks 12-13. §7 Eval metrics → Task 13. All seven spec sections have a task.
- **Placeholder scan:** no TBD/TODO; every code step has real, complete code.
- **Type consistency:** `PriceRecommendation`, `SkuAnalysis`, `CompetitorPriceHistory`, `RunCostTracker` used identically to their existing `models.py`/`llm_client.py` definitions throughout; new types (`Tool`, `OptimizerInput/Output`, `MatchInput/Output`, `GeneratedSql`, `ScrapeInput/Output`) are defined once (Tasks 5, 7, 8, 9, 10) and referenced by the same names in every later task that uses them.
