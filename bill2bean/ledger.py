from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re

from .config import Config
from .model import BillTransaction, Direction, ReviewLevel, ReviewReasons


REVIEW_FIELDS = [
    "uid",
    "action",
    "review_level",
    "aa_amount",
    "share",
    "share_amount",
    "discount_amount",
    "time",
    "source",
    "direction",
    "amount",
    "currency",
    "review_reason",
    "payee",
    "narration",
    "source_account",
    "expense_account",
    "income_account",
    "receivable_account",
    "aa_account",
    "share_account",
    "discount_account",
    "notes",
    "tags",
    "links",
    "source_id",
]


@dataclass
class ReviewRow:
    uid: str = ""
    action: str = ""
    review_level: str = ""
    aa_amount: str = ""
    share: str = ""
    share_amount: str = ""
    discount_amount: str = ""
    time: str = ""
    source: str = ""
    direction: str = ""
    amount: str = ""
    currency: str = ""
    review_reason: str = ""
    payee: str = ""
    narration: str = ""
    source_account: str = ""
    expense_account: str = ""
    income_account: str = ""
    receivable_account: str = ""
    aa_account: str = ""
    share_account: str = ""
    discount_account: str = ""
    notes: str = ""
    tags: str = ""
    links: str = ""
    source_id: str = ""

    @classmethod
    def from_dict(cls, row: dict[str, str]) -> "ReviewRow":
        return cls(**{field: row.get(field, "") for field in REVIEW_FIELDS})

    @classmethod
    def from_transaction(cls, tx: BillTransaction) -> "ReviewRow":
        return cls(
            uid=tx.uid,
            action=tx.action,
            review_level=tx.review_level.value,
            aa_amount=tx.aa_amount,
            share=tx.share,
            share_amount=tx.share_amount,
            discount_amount=tx.discount_amount,
            time=tx.time.isoformat(sep=" "),
            source=tx.source,
            direction=tx.direction.value,
            amount=str(tx.amount),
            currency=tx.currency,
            review_reason=str(tx.review_reason),
            payee=tx.payee,
            narration=tx.narration,
            source_account=tx.source_account_hint,
            expense_account=tx.expense_account,
            income_account=tx.income_account,
            receivable_account=tx.receivable_account,
            aa_account=tx.aa_account,
            share_account=tx.share_account,
            discount_account=tx.discount_account,
            notes=tx.notes,
            tags=tx.tags,
            links=tx.links,
            source_id=tx.source_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in REVIEW_FIELDS}

    def get(self, key: str, default: str = "") -> str:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)

    def __setitem__(self, key: str, value: str) -> None:
        setattr(self, key, value)

    @property
    def reasons(self) -> ReviewReasons:
        return ReviewReasons.parse(self.review_reason)

    @property
    def posting_date(self) -> str:
        if len(self.time) >= 10:
            return self.time[:10]
        raise ValueError(f"cannot infer posting date for row {self.uid}")

    def duplicate_parent_uid(self) -> str:
        return self.reasons.duplicate_parent_uid()

    def is_post_manual(self) -> bool:
        return self.action == "post" and self.review_level == ReviewLevel.MANUAL.value

    def is_skip_ok(self) -> bool:
        return self.action == "skip" and self.review_level == ReviewLevel.OK.value

    def is_skip_check_duplicate(self) -> bool:
        return (
            self.action == "skip"
            and self.review_level == ReviewLevel.CHECK.value
            and bool(self.duplicate_parent_uid())
        )


class TransactionList:
    def __init__(self, txs: list[BillTransaction], config: Config):
        self.txs = txs
        self.config = config

    def normalize(self) -> "TransactionList":
        for tx in self.txs:
            tx.source_account_hint = self.config.source_account_for(tx)
            if tx.direction == Direction.EXPENSE:
                tx.expense_account = self.config.expense_account_for(tx)
                self._apply_discount_metadata(tx)
                if self._is_family_card(tx):
                    tx.share = tx.share or "whole"
                    tx.share_account = tx.share_account or self.config.family_card_receivable_account
                    tx.review_reason.add("family_card_receivable")
            elif tx.direction == Direction.INCOME:
                if self._is_refund(tx):
                    tx.direction = Direction.EXPENSE
                    tx.amount = -tx.amount
                    tx.expense_account = self.config.expense_account_for(tx)
                    tx.review_reason.add("refund_as_negative_expense")
                    continue
                tx.income_account = (
                    self.config.cashback_income_account
                    if tx.metadata.get("txn_type") == "刷卡金"
                    else self.config.income_account_for(tx)
                )
                if self._is_credit_card_repayment_credit(tx):
                    tx.action = "post"
                    tx.income_account = self.config.manual_income_account
                    tx.review_level = ReviewLevel.MANUAL
                    tx.review_reason.add("unmatched_credit_card_repayment")
            elif self._is_payment_platform_credit_card_repayment(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.expense_account = ""
                tx.review_level = ReviewLevel.MANUAL
                self._apply_discount_metadata(tx)
                tx.review_reason.add("unmatched_credit_card_repayment_transfer")
            elif self._is_alipay_yuebao_transfer(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.review_level = ReviewLevel.CHECK
                tx.source_account_hint = self._alipay_payment_method_account(tx)
                tx.expense_account = "Assets:Current:Alipay:YuEBao"
                tx.review_reason.add("yuebao_transfer")
            elif self._is_alipay_credit_repayment(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.review_level = ReviewLevel.CHECK
                tx.source_account_hint = self._alipay_payment_method_account(tx)
                tx.expense_account = self._alipay_credit_repayment_account(tx)
                tx.review_reason.add("credit_repayment")
            tx.aa_account = tx.aa_account or self.config.aa_account
            tx.receivable_account = tx.receivable_account or self.config.receivable_account
            tx.share_account = tx.share_account or self.config.family_card_receivable_account
            if tx.action == "receivable":
                self._normalize_receivable_action(tx)
            reason = self.config.needs_manual_review(tx)
            if reason:
                self._mark_manual(tx, f"manual_pattern:{reason}")
            if tx.direction == Direction.NEUTRAL:
                tx.action = "skip"
                if tx.review_level != ReviewLevel.MANUAL:
                    tx.review_level = (
                        ReviewLevel.OK
                        if self._is_alipay_safe_investment_neutral(tx)
                        else ReviewLevel.CHECK
                    )
                if not tx.review_reason:
                    tx.review_reason.add("neutral_transaction")
            if not tx.source_account_hint or tx.source_account_hint == "Assets:Unknown":
                self._mark_manual(tx, "unknown_source_account")
            if tx.direction == Direction.EXPENSE and tx.expense_account == self.config.default_expense_account:
                tx.review_level = max_review(tx.review_level, ReviewLevel.CHECK)
                tx.review_reason.add("default_expense_account")
        CrossSourceMatcher(self.txs, self.config).deduplicate()
        self._force_manual_post()
        return self

    def _mark_manual(self, tx: BillTransaction, reason: str) -> None:
        tx.review_level = ReviewLevel.MANUAL
        tx.review_reason.add(reason)

    def _normalize_receivable_action(self, tx: BillTransaction) -> None:
        if tx.share or tx.share_amount:
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason.add("receivable_overrides_share")
            tx.share = ""
            tx.share_amount = ""

    def _force_manual_post(self) -> None:
        for tx in self.txs:
            if tx.review_level != ReviewLevel.MANUAL or tx.action != "skip":
                continue
            tx.action = "post"
            tx.review_reason.add("forced_manual_post")
            if tx.direction == Direction.INCOME:
                tx.income_account = self.config.manual_income_account
            elif tx.direction == Direction.EXPENSE:
                tx.expense_account = self.config.manual_expense_account
            elif tx.direction == Direction.TRANSFER:
                tx.expense_account = self.config.manual_income_account
            else:
                tx.direction = Direction.EXPENSE
                tx.expense_account = self.config.manual_expense_account

    def _is_family_card(self, tx: BillTransaction) -> bool:
        return is_family_card(tx)

    def _apply_discount_metadata(self, tx: BillTransaction) -> None:
        tx.discount_account = tx.discount_account or self.config.discount_income_account
        if tx.metadata.get("discount_amount"):
            tx.discount_amount = str(tx.metadata["discount_amount"])
            tx.review_reason.add("payment_discount")
        elif tx.source == "alipay" and "&" in tx.metadata.get("收/付款方式", ""):
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason.add("payment_discount_amount_unknown")

    def _is_payment_platform_credit_card_repayment(self, tx: BillTransaction) -> bool:
        return is_payment_platform_credit_card_repayment(tx)

    def _is_alipay_yuebao_transfer(self, tx: BillTransaction) -> bool:
        return (
            tx.source == "alipay"
            and tx.direction == Direction.NEUTRAL
            and tx.metadata.get("交易分类") == "投资理财"
            and tx.payee == "余额宝"
            and "收益发放" not in tx.narration
        )

    def _is_alipay_credit_repayment(self, tx: BillTransaction) -> bool:
        return (
            tx.source == "alipay"
            and tx.direction == Direction.NEUTRAL
            and tx.metadata.get("交易分类") == "信用借还"
        )

    def _is_alipay_safe_investment_neutral(self, tx: BillTransaction) -> bool:
        text = self._text(tx)
        return (
            tx.source == "alipay"
            and (
                tx.metadata.get("交易分类") == "投资理财"
                or "蚂蚁财富" in text
                or "基金" in text
                or "黄金ETF" in text
            )
            and tx.payee != "余额宝"
        )

    def _alipay_payment_method_account(self, tx: BillTransaction) -> str:
        method = tx.metadata.get("收/付款方式", "")
        return self.config.account_for_text(method) or tx.source_account_hint

    def _alipay_credit_repayment_account(self, tx: BillTransaction) -> str:
        if "花呗" in self._text(tx):
            return "Liabilities:Credit:Alipay:Huabei"
        return ""

    def _is_credit_card_repayment_credit(self, tx: BillTransaction) -> bool:
        return is_credit_card_repayment_credit(tx)

    def _text(self, tx: BillTransaction) -> str:
        return transaction_text(tx)

    def _is_refund(self, tx: BillTransaction) -> bool:
        if tx.source == "icbc_credit" and tx.metadata.get("txn_type") == "退款":
            return True
        if tx.source == "wechat":
            return "退款" in tx.metadata.get("交易类型", "") or "退款" in tx.metadata.get("当前状态", "")
        return False

    def write_review_csv(
        self,
        path: str | Path,
        previous_rows: list[ReviewRow] | None = None,
    ) -> None:
        rows = [ReviewRow.from_transaction(tx) for tx in self.txs]
        if previous_rows:
            rows = merge_previous_review_rows(rows, previous_rows)
        with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS)
            writer.writeheader()
            for row in review_row_order(rows):
                writer.writerow(row.to_dict())


class CrossSourceMatcher:
    def __init__(self, txs: list[BillTransaction], config: Config):
        self.txs = txs
        self.config = config
        self.used_family_card_uids: set[str] = set()
        self.used_platform_expense_uids: set[str] = set()
        self.used_repayment_transfer_uids: set[str] = set()

    def deduplicate(self) -> None:
        detailed: dict[tuple[str, Decimal, str], list[BillTransaction]] = {}
        for tx in self.txs:
            if tx.source in {"wechat", "alipay"} and tx.direction == Direction.EXPENSE:
                detailed.setdefault(tx.same_money_day_key(), []).append(tx)
            if tx.action == "transfer":
                detailed.setdefault(tx.same_money_day_key(), []).append(tx)

        for tx in self.txs:
            if tx.source != "icbc_credit":
                continue
            if tx.direction != Direction.EXPENSE and not is_credit_card_repayment_credit(tx):
                continue
            if tx.review_reason.has("unionpay_via_tenpay_missing_from_wechat"):
                continue
            candidates = detailed.get(tx.same_money_day_key(), [])
            merchant = tx.payee.replace("财付通-", "").replace("支付宝-", "")
            for candidate in candidates:
                if (
                    is_credit_card_repayment_credit(tx)
                    and candidate.action == "transfer"
                    and candidate.uid not in self.used_repayment_transfer_uids
                ):
                    candidate.expense_account = tx.source_account_hint
                    candidate.review_level = ReviewLevel.OK
                    candidate.review_reason.replace(
                        "unmatched_credit_card_repayment_transfer",
                        "credit_card_repayment",
                    )
                    tx.action = "skip"
                    tx.income_account = ""
                    tx.review_level = ReviewLevel.CHECK
                    tx.review_reason = ReviewReasons.parse(
                        "duplicate_credit_card_repayment:" + candidate.uid
                    )
                    self.used_repayment_transfer_uids.add(candidate.uid)
                    break
            if tx.action == "skip":
                continue

            expense_candidates = [
                candidate
                for candidate in candidates
                if self._is_available_platform_expense_candidate(
                    tx,
                    candidate,
                )
            ]
            duplicate_candidate = self._select_duplicate_candidate(
                tx,
                expense_candidates,
                merchant,
            )
            if duplicate_candidate:
                self._mark_duplicate_match(
                    tx,
                    duplicate_candidate,
                )
                continue
            if len(expense_candidates) > 1:
                tx.review_level = max_review(tx.review_level, ReviewLevel.CHECK)
                tx.review_reason.add("ambiguous_same_amount_duplicate")
        for tx in self.txs:
            if (
                is_payment_platform_credit_card_repayment(tx)
                and tx.action == "transfer"
                and not tx.expense_account
            ):
                tx.action = "post"
                tx.expense_account = self.config.manual_income_account
                tx.review_level = ReviewLevel.MANUAL

    def _is_available_platform_expense_candidate(
        self,
        credit_tx: BillTransaction,
        candidate: BillTransaction,
    ) -> bool:
        if candidate.source not in {"wechat", "alipay"} or candidate.direction != Direction.EXPENSE:
            return False
        if candidate.amount != credit_tx.amount:
            return False
        if candidate.source_account_hint != credit_tx.source_account_hint:
            return False
        if candidate.uid in self.used_platform_expense_uids:
            return False
        if is_family_card(candidate) and candidate.uid in self.used_family_card_uids:
            return False
        return True

    def _select_duplicate_candidate(
        self,
        credit_tx: BillTransaction,
        candidates: list[BillTransaction],
        merchant: str,
    ) -> BillTransaction | None:
        if len(candidates) == 1:
            return candidates[0]
        merchant_matches = [
            candidate
            for candidate in candidates
            if self._matches_credit_card_merchant(credit_tx, candidate, merchant)
        ]
        if len(merchant_matches) == 1:
            return merchant_matches[0]
        return None

    def _mark_duplicate_match(
        self,
        credit_tx: BillTransaction,
        candidate: BillTransaction,
    ) -> None:
        credit_tx.action = "skip"
        credit_tx.review_level = ReviewLevel.CHECK
        if is_family_card(candidate):
            self._enrich_family_card(candidate, credit_tx)
            candidate.expense_account = self.config.expense_account_for(candidate)
            if candidate.expense_account != self.config.default_expense_account:
                candidate.review_reason.remove("default_expense_account")
            credit_tx.review_reason.add(f"duplicate_family_card:{candidate.uid}")
            self.used_family_card_uids.add(candidate.uid)
        else:
            credit_tx.review_reason.add(f"duplicate_of:{candidate.uid}")
        self.used_platform_expense_uids.add(candidate.uid)

    def _matches_credit_card_merchant(
        self,
        credit_tx: BillTransaction,
        candidate: BillTransaction,
        merchant: str,
    ) -> bool:
        candidate_text = " ".join([candidate.payee, candidate.narration])
        return (
            bool(merchant)
            and candidate.amount == credit_tx.amount
            and (
                merchant in candidate.payee
                or merchant in candidate.narration
                or candidate.payee in merchant
                or self.config.same_merchant_text(merchant, candidate_text)
            )
        )

    def _enrich_family_card(self, family_tx: BillTransaction, credit_tx: BillTransaction) -> None:
        original = family_tx.narration.strip()
        if not original or original == "/":
            family_tx.narration = credit_tx.payee
        family_tx.metadata["matched_credit_payee"] = credit_tx.payee
        family_tx.metadata["matched_credit_type"] = credit_tx.narration
        family_tx.review_reason.add("enriched_from_credit_card")


def max_review(a: ReviewLevel, b: ReviewLevel) -> ReviewLevel:
    order = {ReviewLevel.OK: 0, ReviewLevel.CHECK: 1, ReviewLevel.MANUAL: 2}
    return a if order[a] >= order[b] else b


def transaction_text(tx: BillTransaction) -> str:
    return " ".join([tx.payee, tx.narration, str(tx.metadata)])


def is_family_card(tx: BillTransaction) -> bool:
    return tx.source == "wechat" and tx.metadata.get("交易类型") == "亲属卡交易"


def is_payment_platform_credit_card_repayment(tx: BillTransaction) -> bool:
    return tx.source in {"alipay", "wechat"} and "信用卡还款" in transaction_text(tx)


def is_credit_card_repayment_credit(tx: BillTransaction) -> bool:
    return tx.source.endswith("_credit") and (
        tx.metadata.get("txn_type") == "信用卡还款"
        or tx.metadata.get("is_credit_card_repayment") == "true"
    )


def merge_previous_review_rows(
    rows: list[ReviewRow],
    previous_rows: list[ReviewRow],
) -> list[ReviewRow]:
    previous_by_uid = {
        row.uid: row
        for row in previous_rows
        if row.uid
    }
    merged_rows: list[ReviewRow] = []
    for row in rows:
        previous = previous_by_uid.get(row.uid)
        if not previous:
            merged_rows.append(row)
            continue
        merged = ReviewRow.from_dict(row.to_dict())
        for field in REVIEW_FIELDS:
            setattr(merged, field, getattr(previous, field))
        merged.uid = row.uid
        merged_rows.append(merged)
    return merged_rows


def review_row_order(rows: list[ReviewRow]) -> list[ReviewRow]:
    ordered = sorted(
        rows,
        key=lambda r: (r.time, r.source, r.uid),
    )
    by_uid = {row.uid: row for row in ordered if row.uid}
    duplicate_children: dict[str, list[ReviewRow]] = {}
    duplicate_uids: set[str] = set()
    for row in ordered:
        if not row.is_skip_check_duplicate():
            continue
        parent_uid = row.duplicate_parent_uid()
        if parent_uid and parent_uid in by_uid:
            duplicate_children.setdefault(parent_uid, []).append(row)
            duplicate_uids.add(row.uid)

    post_manual = [
        row
        for row in ordered
        if row.is_post_manual() and row.uid not in duplicate_uids
    ]
    initial = [
        row
        for row in ordered
        if not row.is_post_manual()
        and not row.is_skip_ok()
        and row.uid not in duplicate_uids
    ]
    skip_ok = [row for row in ordered if row.is_skip_ok()]
    result: list[ReviewRow] = []
    emitted: set[str] = set()

    def emit(row: ReviewRow) -> None:
        uid = row.uid
        if uid in emitted:
            return
        emitted.add(uid)
        result.append(row)
        for child in duplicate_children.get(uid, []):
            emit(child)

    for row in post_manual:
        emit(row)
    for row in initial:
        emit(row)
    for row in skip_ok:
        emit(row)
    return result


def read_review_csv(path: str | Path) -> list[ReviewRow]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        return [ReviewRow.from_dict(row) for row in csv.DictReader(fh)]


def extract_accounts(path: str | Path) -> set[str]:
    accounts: set[str] = set()
    account_re = re.compile(r'^\s*\d{4}-\d{2}-\d{2}\s+open\s+([A-Z][A-Za-z0-9:-]+)')
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = account_re.match(line)
        if match:
            accounts.add(match.group(1))
    return accounts
