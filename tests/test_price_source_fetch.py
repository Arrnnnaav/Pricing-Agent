from db.connection import get_connection, init_db
from db.catalog_repo import insert_catalog_rows
from db.competitor_repo import insert_competitor_price_rows
from data.price_source import fetch_batch


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
            {
                "sku": "HEA-0002",
                "name": "Sony Pulse Air",
                "brand": "Sony",
                "category": "Headphones",
                "our_price": 199.0,
                "cost": 100.0,
                "stock": 30,
            },
        ],
    )
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
            {
                "sku": "LAP-0001",
                "competitor": "CircuitHub",
                "price": 1009.0,
                "in_stock": 0,
                "date": "2026-08-15",
            },
            {
                "sku": "HEA-0002",
                "competitor": "ByteMart",
                "price": 189.0,
                "in_stock": 1,
                "date": "2026-08-15",
            },
        ],
    )
    return conn


def test_fetch_batch_returns_only_requested_skus():
    conn = _seeded_conn()
    result = fetch_batch(conn, ["LAP-0001"])
    assert set(result.keys()) == {"LAP-0001"}
    assert len(result["LAP-0001"]) == 2  # ByteMart + CircuitHub
    conn.close()


def test_fetch_batch_multiple_skus_in_one_query():
    conn = _seeded_conn()
    result = fetch_batch(conn, ["LAP-0001", "HEA-0002"])
    assert set(result.keys()) == {"LAP-0001", "HEA-0002"}
    conn.close()


def test_fetch_batch_preserves_in_stock_flag():
    conn = _seeded_conn()
    result = fetch_batch(conn, ["LAP-0001"])
    by_competitor = {h.competitor: h for h in result["LAP-0001"]}
    assert by_competitor["ByteMart"].points[0].in_stock is True
    assert by_competitor["CircuitHub"].points[0].in_stock is False
    conn.close()


def test_fetch_batch_empty_sku_list_returns_empty_dict():
    conn = _seeded_conn()
    assert fetch_batch(conn, []) == {}
    conn.close()
