"""Tests for order pricing."""

from app import price_order
from models import ItemId, OrderId


def test_bulk_orders_get_discount():
    items = [ItemId(f"i{n}") for n in range(11)]
    assert price_order(OrderId("o1"), items) == 990


def test_blank_item_id_rejected():
    try:
        ItemId("").check()
    except ValueError:
        return
    raise AssertionError("expected ValueError")
