from .types import gold_first, gold_second


def gold_ordered_caller(value: int) -> int:
    gold_first(value)
    gold_second(value)
    return gold_first(value)
