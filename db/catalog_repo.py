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
    conn.execute("UPDATE catalog SET our_price = ? WHERE sku = ?", (new_price, sku))
    conn.commit()
