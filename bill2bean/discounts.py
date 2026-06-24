from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class DiscountAmount:
    amount: Decimal
    is_cashback: bool = False

    @classmethod
    def parse(cls, value: str) -> "DiscountAmount":
        text = (value or "").strip()
        if not text:
            return cls(Decimal("0"))

        is_cashback = False
        lowered = text.lower()
        for prefix in ("cb:", "cashback:"):
            if lowered.startswith(prefix):
                text = text[len(prefix) :].strip()
                is_cashback = True
                break

        try:
            amount = Decimal(text.replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"invalid discount_amount: {value!r}") from exc
        return cls(amount, is_cashback)

    @property
    def fee_deduction(self) -> Decimal:
        if self.is_cashback:
            return Decimal("0")
        return self.amount


def parse_deduction_discount_amount(value: str, uid: str = "") -> Decimal:
    discount = DiscountAmount.parse(value)
    if discount.is_cashback:
        suffix = f" for row {uid}" if uid else ""
        raise ValueError(
            f"cashback discount_amount syntax is only supported for investment rows{suffix}"
        )
    return discount.amount
