from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from functools import total_ordering
import hashlib
from typing import Any


class Direction(str, Enum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    NEUTRAL = "neutral"


@total_ordering
class ReviewLevel(Enum):
    OK = ("ok", 0)
    CHECK = ("check", 1)
    MANUAL = ("manual", 2)

    def __new__(cls, value: str, rank: int) -> "ReviewLevel":
        obj = object.__new__(cls)
        obj._value_ = value
        obj._rank = rank
        return obj

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReviewLevel):
            return NotImplemented
        return self._rank < other._rank


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

    def flagged_accounts(self) -> set[str]:
        return {
            reason.removeprefix("flagged_account:")
            for reason in self.items
            if reason.startswith("flagged_account:")
        }


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
    investment_units: str = ""
    investment_price: str = ""
    investment_price_date: str = ""
    commission_amount: str = ""
    commission_account: str = ""
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

    def text(self) -> str:
        return " ".join([self.payee, self.narration, str(self.metadata)])

    def is_family_card(self) -> bool:
        return self.source == "wechat" and self.metadata.get("交易类型") == "亲属卡交易"

    def is_payment_platform_credit_card_repayment(self) -> bool:
        return self.source in {"alipay", "wechat"} and "信用卡还款" in self.text()

    def is_credit_card_repayment_credit(self) -> bool:
        return self.source.endswith("_credit") and (
            self.metadata.get("txn_type") == "信用卡还款"
            or self.metadata.get("is_credit_card_repayment") == "true"
        )

    def is_refund(self) -> bool:
        if self.action == "merge_cashback" or self.metadata.get("is_credit_card_cashback") == "true":
            return False
        if self.source == "icbc_credit" and self.metadata.get("txn_type") in {
            "退款",
            "退货",
            "境外退货",
        }:
            return True
        if self.source == "wechat":
            return "退款" in self.metadata.get("交易类型", "")
        if self.source == "alipay":
            narration = self.metadata.get("商品说明", "").strip()
            return (
                self.metadata.get("交易状态", "").strip() == "退款成功"
                and (
                    self.metadata.get("交易分类", "").strip() == "退款"
                    or narration.startswith("退款-")
                )
            )
        return False

    def is_platform_account_transfer(self) -> bool:
        return self.direction == Direction.NEUTRAL and bool(self.metadata.get("target_account_hint"))

    def mark_manual(self, reason: str) -> None:
        self.review_level = ReviewLevel.MANUAL
        self.review_reason.add(reason)

    def flag_account(self, account: str) -> None:
        self.mark_manual(f"flagged_account:{account}")

    def normalize_reimburse_action(self) -> None:
        if self.share or self.share_amount:
            self.review_level = ReviewLevel.MANUAL
            self.review_reason.add("reimburse_overrides_share")
            self.share = ""
            self.share_amount = ""

    def fill_relevant_review_accounts(
        self,
        aa_account: str,
        receivable_account: str,
        share_account: str,
    ) -> None:
        if self.direction == Direction.EXPENSE:
            self.aa_account = self.aa_account or aa_account
            self.receivable_account = self.receivable_account or receivable_account
            self.share_account = self.share_account or share_account

    def apply_family_card_share(self, share_account: str) -> None:
        self.share = self.share or "whole"
        self.share_account = self.share_account or share_account
        self.review_reason.add("family_card_receivable")

    def convert_refund_to_negative_expense(self, expense_account: str) -> None:
        self.direction = Direction.EXPENSE
        self.amount = -self.amount
        self.expense_account = expense_account
        self.review_reason.add("refund_as_negative_expense")

    def mark_unmatched_credit_card_repayment(self, income_account: str) -> None:
        self.action = "post"
        self.income_account = income_account
        self.flag_account(income_account)
        self.review_reason.add("unmatched_credit_card_repayment")

    def mark_unmatched_repayment_transfer(self, discount_account: str) -> None:
        self.direction = Direction.TRANSFER
        self.action = "transfer"
        self.expense_account = ""
        self.review_level = ReviewLevel.MANUAL
        self.fill_default_discount_account(discount_account)
        self.review_reason.add("unmatched_credit_card_repayment_transfer")

    def mark_check_transfer(
        self,
        target_account: str,
        reason: str,
        source_account: str = "",
    ) -> None:
        self.direction = Direction.TRANSFER
        self.action = "transfer"
        self.review_level = ReviewLevel.CHECK
        if source_account:
            self.source_account_hint = source_account
        self.expense_account = target_account
        self.review_reason.add(reason)

    def mark_same_account_transfer(self, reason: str) -> None:
        self.action = "skip"
        self.review_level = ReviewLevel.OK
        self.review_reason.add(reason)

    def mark_investment_trade(self, commission_account: str) -> None:
        self.action = "invest"
        self.review_level = ReviewLevel.OK
        self.commission_account = self.commission_account or commission_account
        self.review_reason.add("investment_trade")

    def fill_default_discount_account(self, default_discount_account: str) -> None:
        self.discount_account = self.discount_account or default_discount_account

    def force_manual_post(
        self,
        default_income_account: str,
        default_expense_account: str,
        suspense_account: str,
    ) -> None:
        if self.review_level != ReviewLevel.MANUAL or self.action != "skip":
            return
        self.action = "post"
        self.review_reason.add("forced_manual_post")
        if self.direction == Direction.INCOME:
            self.income_account = default_income_account
            self.flag_account(default_income_account)
        elif self.direction == Direction.EXPENSE:
            self.expense_account = default_expense_account
            self.flag_account(default_expense_account)
        elif self.direction == Direction.TRANSFER:
            self.expense_account = suspense_account
            self.flag_account(suspense_account)
        else:
            self.direction = Direction.EXPENSE
            self.expense_account = default_expense_account
            self.flag_account(default_expense_account)
