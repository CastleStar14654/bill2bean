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
    review_reason: str = ""
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

    @property
    def date(self) -> str:
        return self.time.date().isoformat()

    @property
    def uid(self) -> str:
        raw = "|".join(
            [
                self.source,
                self.source_id,
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
