import pandas as pd
from db.connection import get_connection, init_db
from db.catalog_repo import insert_catalog_rows, get_catalog_df, update_price
from db.competitor_repo import insert_competitor_price_rows, get_rows_for_sku


def test_init_db_creates_tables():
    conn = get_connection(":memory:")
    init_db(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"catalog", "competitor_prices"} <= tables
    conn.close()


def _seeded_conn():
    conn = get_connection(":memory:")
    init_db(conn)
    insert_catalog_rows(
        conn,
        [
            {
                "sku": "LAP-0001",
                "name": "Dell Vortex Pro",
                "brand": "Dell",
                "category": "Laptops",
                "our_price": 999.0,
                "cost": 700.0,
                "stock": 20,
            },
        ],
    )
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
    insert_competitor_price_rows(
        conn,
        [
            {
                "sku": "LAP-0001",
                "competitor": "ByteMart",
                "price": 979.0,
                "in_stock": 1,
                "date": "2026-08-15",
            },
        ],
    )
    rows = get_rows_for_sku(conn, "LAP-0001")
    assert len(rows) == 1
    assert rows[0]["competitor"] == "ByteMart"
    conn.close()


def test_generated_catalog_loads_into_db():
    import os
    import tempfile
    from data.generate_catalog import generate_catalog

    df = generate_catalog(num_skus=5)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = get_connection(db_path)
        init_db(conn)
        insert_catalog_rows(conn, df.to_dict(orient="records"))
        loaded = get_catalog_df(conn)
        assert len(loaded) == 5
        conn.close()
