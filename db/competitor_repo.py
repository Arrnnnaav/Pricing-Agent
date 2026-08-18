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
