class GoldPythonBase:
    pass


class GoldPythonDerived(GoldPythonBase):
    pass


def gold_first(value: int) -> int:
    return value + 1


def gold_second(value: int) -> int:
    return value * 2
