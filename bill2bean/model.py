from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
from typing import Any


class Direction(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    NEUTRAL = "neutral"


class ReviewLevel(str, Enum):
    OK = "ok"
    CHECK = "check"
    MANUAL = "manual"


@dataclass
class ReviewReasons:
    items: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, value: str | "ReviewReasons") -> "ReviewReasons":
        if isinstance(value, ReviewReasons):
            return cls(list(value.items))
        return cls([part for part in value.split(";") if part])

    def __bool__(self) -> bool:
        return bool(self.items)

    def __str__(self) -> str:
        return ";".join(self.items)

    def has(self, reason: str) -> bool:
        return reason in self.items

    def add(self, reason: str) -> "ReviewReasons":
        if reason not in self.items:
            self.items.append(reason)
        return self

    def remove(self, reason: str) -> "ReviewReasons":
        self.items = [part for part in self.items if part != reason]
        return self

    def replace(self, old: str, new: str) -> "ReviewReasons":
        self.items = [new if part == old else part for part in self.items]
        if new not in self.items:
            self.items.append(new)
        return self

    def value_after_prefix(self, prefixes: tuple[str, ...]) -> str:
        for reason in self.items:
            for prefix in prefixes:
                if reason.startswith(prefix):
                    return reason.removeprefix(prefix)
        return ""

    def duplicate_parent_uid(self) -> str:
        return self.value_after_prefix(
            (
                "duplicate_of:",
                "duplicate_family_card:",
                "duplicate_credit_card_repayment:",
            )
        )

    def cashback_parent_uid(self) -> str:
        return self.value_after_prefix(("cashback_for:",))


@dataclass
class BillTransaction:
    source: str
    source_id: str
    time: datetime
    payee: str
    narration: str
    amount: Decimal
    currency: str = "CNY"
    direction: Direction = Direction.EXPENSE
    source_account_hint: str = ""
    expense_account: str = ""
    income_account: str = ""
    receivable_account: str = ""
    action: str = "post"
    review_level: ReviewLevel = ReviewLevel.OK
    review_reason: ReviewReasons = field(default_factory=ReviewReasons)
    tags: str = ""
    links: str = ""
    notes: str = ""
    aa_account: str = ""
    aa_amount: str = ""
    share: str = ""
    share_account: str = ""
    share_amount: str = ""
    discount_account: str = ""
    discount_amount: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.review_reason, str):
            self.review_reason = ReviewReasons.parse(self.review_reason)

    @property
    def date(self) -> str:
        return self.time.date().isoformat()

    @property
    def uid(self) -> str:
        if self.source_id:
            raw = "|".join([self.source, self.source_id])
        else:
            raw = "|".join(
                [
                    self.source,
                    self.time.isoformat(),
                    self.payee,
                    self.narration,
                    str(self.amount),
                    self.direction.value,
                ]
            )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def same_money_day_key(self) -> tuple[str, Decimal, str]:
        return (self.date, self.amount.copy_abs(), self.currency)
