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
