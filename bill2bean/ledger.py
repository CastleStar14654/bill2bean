from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path
import re

from .config import Config
from .model import BillTransaction, Direction, ReviewLevel


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


class TransactionList:
    def __init__(self, txs: list[BillTransaction], config: Config):
        self.txs = txs
        self.config = config

    def normalize(self) -> "TransactionList":
        used_family_card_uids: set[str] = set()
        for tx in self.txs:
            tx.source_account_hint = self.config.source_account_for(tx)
            if tx.direction == Direction.EXPENSE:
                tx.expense_account = self.config.expense_account_for(tx)
                self._apply_discount_metadata(tx)
                if self._is_family_card(tx):
                    tx.share = tx.share or "whole"
                    tx.share_account = tx.share_account or self.config.family_card_receivable_account
                    tx.review_reason = append_reason(tx.review_reason, "family_card_receivable")
            elif tx.direction == Direction.INCOME:
                if self._is_refund(tx):
                    tx.direction = Direction.EXPENSE
                    tx.amount = -tx.amount
                    tx.expense_account = self.config.expense_account_for(tx)
                    tx.review_reason = append_reason(tx.review_reason, "refund_as_negative_expense")
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
                    tx.review_reason = append_reason(
                        tx.review_reason, "unmatched_credit_card_repayment"
                    )
            elif self._is_payment_platform_credit_card_repayment(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.expense_account = ""
                tx.review_level = ReviewLevel.MANUAL
                self._apply_discount_metadata(tx)
                tx.review_reason = append_reason(
                    tx.review_reason, "unmatched_credit_card_repayment_transfer"
                )
            elif self._is_alipay_yuebao_transfer(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.review_level = ReviewLevel.CHECK
                tx.source_account_hint = self._alipay_payment_method_account(tx)
                tx.expense_account = "Assets:Current:Alipay:YuEBao"
                tx.review_reason = append_reason(tx.review_reason, "yuebao_transfer")
            elif self._is_alipay_credit_repayment(tx):
                tx.direction = Direction.TRANSFER
                tx.action = "transfer"
                tx.review_level = ReviewLevel.CHECK
                tx.source_account_hint = self._alipay_payment_method_account(tx)
                tx.expense_account = self._alipay_credit_repayment_account(tx)
                tx.review_reason = append_reason(tx.review_reason, "credit_repayment")
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
                tx.review_reason = tx.review_reason or "neutral_transaction"
            if not tx.source_account_hint or tx.source_account_hint == "Assets:Unknown":
                self._mark_manual(tx, "unknown_source_account")
            if tx.direction == Direction.EXPENSE and tx.expense_account == self.config.default_expense_account:
                tx.review_level = max_review(tx.review_level, ReviewLevel.CHECK)
                tx.review_reason = append_reason(tx.review_reason, "default_expense_account")
        self._deduplicate_cross_sources()
        self._force_manual_post()
        return self

    def _deduplicate_cross_sources(self) -> None:
        detailed: dict[tuple[str, Decimal, str], list[BillTransaction]] = {}
        for tx in self.txs:
            if tx.source in {"wechat", "alipay"} and tx.direction == Direction.EXPENSE:
                detailed.setdefault(tx.same_money_day_key(), []).append(tx)
            if tx.action == "transfer":
                detailed.setdefault(tx.same_money_day_key(), []).append(tx)

        used_family_card_uids: set[str] = set()
        used_repayment_transfer_uids: set[str] = set()
        for tx in self.txs:
            if tx.source != "icbc_credit":
                continue
            if tx.direction != Direction.EXPENSE and not self._is_credit_card_repayment_credit(tx):
                continue
            if tx.review_reason == "unionpay_via_tenpay_missing_from_wechat":
                continue
            candidates = detailed.get(tx.same_money_day_key(), [])
            merchant = tx.payee.replace("财付通-", "").replace("支付宝-", "")
            for candidate in candidates:
                if (
                    self._is_credit_card_repayment_credit(tx)
                    and candidate.action == "transfer"
                    and candidate.uid not in used_repayment_transfer_uids
                ):
                    candidate.expense_account = tx.source_account_hint
                    candidate.review_level = ReviewLevel.OK
                    candidate.review_reason = replace_reason(
                        candidate.review_reason,
                        "unmatched_credit_card_repayment_transfer",
                        "credit_card_repayment",
                    )
                    tx.action = "skip"
                    tx.income_account = ""
                    tx.review_level = ReviewLevel.CHECK
                    tx.review_reason = "duplicate_credit_card_repayment:" + candidate.uid
                    used_repayment_transfer_uids.add(candidate.uid)
                    break
                if (
                    self._is_family_card(candidate)
                    and candidate.uid not in used_family_card_uids
                    and candidate.source_account_hint == tx.source_account_hint
                ):
                    self._enrich_family_card(candidate, tx)
                    candidate.expense_account = self.config.expense_account_for(candidate)
                    if candidate.expense_account != self.config.default_expense_account:
                        candidate.review_reason = remove_reason(
                            candidate.review_reason, "default_expense_account"
                        )
                    tx.action = "skip"
                    tx.review_level = ReviewLevel.CHECK
                    tx.review_reason = append_reason(
                        tx.review_reason, f"duplicate_family_card:{candidate.uid}"
                    )
                    used_family_card_uids.add(candidate.uid)
                    break
                if merchant and (
                    merchant in candidate.payee
                    or merchant in candidate.narration
                    or candidate.payee in merchant
                ):
                    tx.action = "skip"
                    tx.review_level = ReviewLevel.CHECK
                    tx.review_reason = append_reason(
                        tx.review_reason, f"duplicate_of:{candidate.uid}"
                    )
                    break
        for tx in self.txs:
            if (
                self._is_payment_platform_credit_card_repayment(tx)
                and tx.action == "transfer"
                and not tx.expense_account
            ):
                tx.action = "post"
                tx.expense_account = self.config.manual_income_account
                tx.review_level = ReviewLevel.MANUAL

    def _mark_manual(self, tx: BillTransaction, reason: str) -> None:
        tx.review_level = ReviewLevel.MANUAL
        tx.review_reason = append_reason(tx.review_reason, reason)

    def _normalize_receivable_action(self, tx: BillTransaction) -> None:
        if tx.share or tx.share_amount:
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason = append_reason(tx.review_reason, "receivable_overrides_share")
            tx.share = ""
            tx.share_amount = ""

    def _force_manual_post(self) -> None:
        for tx in self.txs:
            if tx.review_level != ReviewLevel.MANUAL or tx.action != "skip":
                continue
            tx.action = "post"
            tx.review_reason = append_reason(tx.review_reason, "forced_manual_post")
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
        return tx.source == "wechat" and tx.metadata.get("交易类型") == "亲属卡交易"

    def _enrich_family_card(self, family_tx: BillTransaction, credit_tx: BillTransaction) -> None:
        original = family_tx.narration.strip()
        if not original or original == "/":
            family_tx.narration = credit_tx.payee
        family_tx.metadata["matched_credit_payee"] = credit_tx.payee
        family_tx.metadata["matched_credit_type"] = credit_tx.narration
        family_tx.review_reason = append_reason(
            family_tx.review_reason, "enriched_from_credit_card"
        )

    def _apply_discount_metadata(self, tx: BillTransaction) -> None:
        tx.discount_account = tx.discount_account or self.config.discount_income_account
        if tx.metadata.get("discount_amount"):
            tx.discount_amount = str(tx.metadata["discount_amount"])
            tx.review_reason = append_reason(tx.review_reason, "payment_discount")
        elif tx.source == "alipay" and "&" in tx.metadata.get("收/付款方式", ""):
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason = append_reason(
                tx.review_reason, "payment_discount_amount_unknown"
            )

    def _is_payment_platform_credit_card_repayment(self, tx: BillTransaction) -> bool:
        return tx.source in {"alipay", "wechat"} and "信用卡还款" in self._text(tx)

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
        return tx.source.endswith("_credit") and (
            tx.metadata.get("txn_type") == "信用卡还款"
            or tx.metadata.get("is_credit_card_repayment") == "true"
        )

    def _text(self, tx: BillTransaction) -> str:
        return " ".join([tx.payee, tx.narration, str(tx.metadata)])

    def _is_refund(self, tx: BillTransaction) -> bool:
        if tx.source == "icbc_credit" and tx.metadata.get("txn_type") == "退款":
            return True
        if tx.source == "wechat":
            return "退款" in tx.metadata.get("交易类型", "") or "退款" in tx.metadata.get("当前状态", "")
        return False

    def write_review_csv(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS)
            writer.writeheader()
            for tx in review_order(self.txs):
                writer.writerow(review_row(tx))


def append_reason(current: str, reason: str) -> str:
    if not current:
        return reason
    if reason in current.split(";"):
        return current
    return f"{current};{reason}"


def remove_reason(current: str, reason: str) -> str:
    return ";".join(part for part in current.split(";") if part and part != reason)


def replace_reason(current: str, old: str, new: str) -> str:
    parts = [new if part == old else part for part in current.split(";") if part]
    if new not in parts:
        parts.append(new)
    return ";".join(parts)


def max_review(a: ReviewLevel, b: ReviewLevel) -> ReviewLevel:
    order = {ReviewLevel.OK: 0, ReviewLevel.CHECK: 1, ReviewLevel.MANUAL: 2}
    return a if order[a] >= order[b] else b


def review_order(txs: list[BillTransaction]) -> list[BillTransaction]:
    ordered = sorted(txs, key=lambda t: (t.time, t.source, t.uid))
    by_uid = {tx.uid: tx for tx in ordered}
    duplicate_children: dict[str, list[BillTransaction]] = {}
    duplicate_uids: set[str] = set()
    for tx in ordered:
        if not is_skip_check_duplicate(tx):
            continue
        parent_uid = duplicate_parent_uid(tx.review_reason)
        if parent_uid and parent_uid in by_uid:
            duplicate_children.setdefault(parent_uid, []).append(tx)
            duplicate_uids.add(tx.uid)

    post_manual = [
        tx
        for tx in ordered
        if is_post_manual(tx) and tx.uid not in duplicate_uids
    ]
    initial = [
        tx
        for tx in ordered
        if not is_post_manual(tx) and not is_skip_ok(tx) and tx.uid not in duplicate_uids
    ]
    skip_ok = [tx for tx in ordered if is_skip_ok(tx)]
    result: list[BillTransaction] = []
    emitted: set[str] = set()

    def emit(tx: BillTransaction) -> None:
        if tx.uid in emitted:
            return
        emitted.add(tx.uid)
        result.append(tx)
        for child in duplicate_children.get(tx.uid, []):
            emit(child)

    for tx in post_manual:
        emit(tx)
    for tx in initial:
        emit(tx)
    for tx in skip_ok:
        emit(tx)
    return result


def is_post_manual(tx: BillTransaction) -> bool:
    return tx.action == "post" and tx.review_level == ReviewLevel.MANUAL


def is_skip_ok(tx: BillTransaction) -> bool:
    return tx.action == "skip" and tx.review_level == ReviewLevel.OK


def is_skip_check_duplicate(tx: BillTransaction) -> bool:
    return (
        tx.action == "skip"
        and tx.review_level == ReviewLevel.CHECK
        and bool(duplicate_parent_uid(tx.review_reason))
    )


def duplicate_parent_uid(review_reason: str) -> str:
    for reason in review_reason.split(";"):
        if "duplicate" not in reason or ":" not in reason:
            continue
        return reason.split(":", 1)[1]
    return ""


def review_row(tx: BillTransaction) -> dict[str, str]:
    return {
        "uid": tx.uid,
        "action": tx.action,
        "review_level": tx.review_level.value,
        "aa_amount": tx.aa_amount,
        "share": tx.share,
        "share_amount": tx.share_amount,
        "discount_amount": tx.discount_amount,
        "time": tx.time.isoformat(sep=" "),
        "source": tx.source,
        "direction": tx.direction.value,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "review_reason": tx.review_reason,
        "payee": tx.payee,
        "narration": tx.narration,
        "source_account": tx.source_account_hint,
        "expense_account": tx.expense_account,
        "income_account": tx.income_account,
        "receivable_account": tx.receivable_account,
        "aa_account": tx.aa_account,
        "share_account": tx.share_account,
        "discount_account": tx.discount_account,
        "notes": tx.notes,
        "tags": tx.tags,
        "links": tx.links,
        "source_id": tx.source_id,
    }


def read_review_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def extract_accounts(path: str | Path) -> set[str]:
    accounts: set[str] = set()
    account_re = re.compile(r'^\s*\d{4}-\d{2}-\d{2}\s+open\s+([A-Z][A-Za-z0-9:-]+)')
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = account_re.match(line)
        if match:
            accounts.add(match.group(1))
    return accounts

