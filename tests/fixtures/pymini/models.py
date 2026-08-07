"""Domain models for the mini shop."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UserId:
    value: str

    def check(self) -> None:
        if not self.value:
            raise ValueError("blank id")


@dataclass(frozen=True)
class OrderId:
    value: str

    def check(self) -> None:
        if not self.value:
            raise ValueError("blank id")


@dataclass(frozen=True)
class ItemId:
    value: str

    def check(self) -> None:
        if not self.value:
            raise ValueError("blank id")
