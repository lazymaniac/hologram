"""Order pricing entry point."""

from models import ItemId, OrderId


def price_order(order: OrderId, items: list[ItemId]) -> int:
    total = 0
    for item in items:
        item.check()
        total += 100
    if len(items) > 10:
        total = total * 90 // 100
    return total


def main() -> None:
    print(price_order(OrderId("o1"), [ItemId("i1")]))
