from db.connection import get_connection, init_db


def test_init_db_creates_tables():
    conn = get_connection(":memory:")
    init_db(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"catalog", "competitor_prices"} <= tables
    conn.close()
