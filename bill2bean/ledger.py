from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .accounts import is_account_name
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
    "notes",
    "tags",
    "links",
    "investment_units",
    "investment_price",
    "investment_price_date",
    "commission_amount",
    "time",
    "source",
    "direction",
    "amount",
    "currency",
    "original_amount",
    "original_currency",
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
    "commission_account",
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
    notes: str = ""
    tags: str = ""
    links: str = ""
    investment_units: str = ""
    investment_price: str = ""
    investment_price_date: str = ""
    commission_amount: str = ""
    time: str = ""
    source: str = ""
    direction: str = ""
    amount: str = ""
    currency: str = ""
    original_amount: str = ""
    original_currency: str = ""
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
    commission_account: str = ""
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
            notes=tx.notes,
            tags=tx.tags,
            links=tx.links,
            investment_units=tx.investment_units,
            investment_price=tx.investment_price,
            investment_price_date=tx.investment_price_date,
            commission_amount=tx.commission_amount,
            time=tx.time.isoformat(sep=" "),
            source=tx.source,
            direction=tx.direction.value,
            amount=str(tx.amount),
            currency=tx.currency,
            original_amount=tx.metadata.get("original_amount", ""),
            original_currency=tx.metadata.get("original_currency", ""),
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
            commission_account=tx.commission_account,
            source_id=tx.source_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in REVIEW_FIELDS}

    def get(self, key: str, default: str = "") -> str:
        return getattr(self, key, default)

    def context(self) -> str:
        if self.uid:
            return f" for row {self.uid}"
        details = " ".join(part for part in [self.time, self.payee] if part)
        return f" for {details}" if details else ""

    def decimal_field(self, field: str, default: str | None = None) -> Decimal:
        value = self.get(field)
        if value is None or value == "":
            if default is None:
                raise ValueError(f"missing {field}{self.context()}")
            value = default
        try:
            return Decimal(value.replace(",", ""))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal {field}={value!r}{self.context()}") from exc

    def nonnegative_decimal_field(
        self,
        field: str,
        default: str | None = None,
    ) -> Decimal:
        value = self.decimal_field(field, default)
        if value < 0:
            raise ValueError(f"{field} must be non-negative{self.context()}")
        return value

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

    def is_invest(self) -> bool:
        return self.action == "invest"

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
        normalizer = TransactionNormalizer(self.config)
        for tx in self.txs:
            normalizer.normalize(tx)
        CrossSourceMatcher(self.txs, self.config).deduplicate()
        for tx in self.txs:
            normalizer.force_manual_post(tx)
        return self

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


class TransactionNormalizer:
    def __init__(self, config: Config):
        self.config = config

    def normalize(self, tx: BillTransaction) -> None:
        tx.source_account_hint = self.config.source_account_for(tx)
        if self._apply_direction_specific_rules(tx):
            return
        self._apply_common_accounts(tx)
        self._apply_manual_review_rules(tx)
        self._apply_neutral_policy(tx)
        self._apply_account_review_flags(tx)

    def force_manual_post(self, tx: BillTransaction) -> None:
        tx.force_manual_post(
            self.config.default_income_account,
            self.config.default_expense_account,
            self.config.suspense_account,
        )

    def _apply_direction_specific_rules(self, tx: BillTransaction) -> bool:
        if tx.metadata.get("cancelled_transaction") == "true":
            tx.action = "skip"
            tx.review_level = ReviewLevel.OK
            tx.review_reason.add("cancelled_transaction")
            return True
        if tx.metadata.get("investment_buy_refund") == "true":
            tx.action = "skip"
            tx.review_level = ReviewLevel.OK
            tx.review_reason.add("investment_buy_refund")
            return False
        if tx.is_refund():
            tx.convert_refund_to_negative_expense(self.config.expense_account_for(tx))
            return True
        if tx.direction == Direction.EXPENSE:
            self._apply_expense_rules(tx)
            return False
        if tx.direction == Direction.INCOME:
            return self._apply_income_rules(tx)
        if tx.is_platform_account_transfer():
            self._apply_platform_account_transfer(tx)
        elif tx.is_payment_platform_credit_card_repayment():
            self._apply_payment_platform_repayment(tx)
        elif tx.metadata.get("investment_trade") == "true":
            tx.mark_investment_trade(self.config.funds.commission_account)
        return False

    def _apply_expense_rules(self, tx: BillTransaction) -> None:
        tx.expense_account = self.config.expense_account_for(tx)
        self._apply_payment_discount_metadata(tx)
        if tx.is_family_card():
            tx.apply_family_card_share(self.config.default_share_account)

    def _apply_income_rules(self, tx: BillTransaction) -> bool:
        tx.income_account = self._income_account_for(tx)
        if tx.is_credit_card_repayment_credit():
            tx.mark_unmatched_credit_card_repayment(self.config.default_income_account)
        return False

    def _income_account_for(self, tx: BillTransaction) -> str:
        if (
            tx.action == "merge_cashback"
            or tx.metadata.get("is_credit_card_cashback") == "true"
        ):
            return tx.income_account or self.config.cashback_income_account
        return self.config.income_account_for(tx)

    def _apply_payment_platform_repayment(self, tx: BillTransaction) -> None:
        tx.mark_unmatched_repayment_transfer(self.config.discount_income_account)
        self._apply_payment_discount_metadata(tx)

    def _apply_platform_account_transfer(self, tx: BillTransaction) -> None:
        target_account = tx.metadata.get("target_account_hint", "")
        reason = tx.metadata.get("platform_transfer", "platform_transfer")
        has_payment_discount = (
            bool(tx.metadata.get("discount_amount"))
            or tx.metadata.get("discount_amount_unknown") == "true"
        )
        if tx.source_account_hint == target_account and not has_payment_discount:
            tx.mark_same_account_transfer("same_account_" + reason)
            return
        tx.mark_check_transfer(target_account, reason)
        self._apply_payment_discount_metadata(tx)
        if not is_account_name(target_account):
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason.add("unknown_target_account")

    def _apply_common_accounts(self, tx: BillTransaction) -> None:
        tx.fill_relevant_review_accounts(
            self.config.aa_account,
            self.config.reimburse_account,
            self.config.default_share_account,
        )
        if tx.action == "reimburse":
            tx.normalize_reimburse_action()

    def _apply_manual_review_rules(self, tx: BillTransaction) -> None:
        reason = self.config.needs_manual_review(tx)
        if reason:
            tx.mark_manual(f"manual_pattern:{reason}")

    def _apply_neutral_policy(self, tx: BillTransaction) -> None:
        if tx.direction != Direction.NEUTRAL or tx.action in {"invest", "skip"}:
            return
        tx.action = "skip"
        if tx.review_level != ReviewLevel.MANUAL:
            tx.review_level = (
                ReviewLevel.OK
                if tx.metadata.get("safe_neutral_skip") == "true"
                else ReviewLevel.CHECK
            )
        if not tx.review_reason:
            tx.review_reason.add("neutral_transaction")

    def _apply_account_review_flags(self, tx: BillTransaction) -> None:
        if not tx.source_account_hint or tx.source_account_hint == self.config.suspense_account:
            tx.mark_manual("unknown_source_account")
            tx.flag_account(self.config.suspense_account)
        elif not is_account_name(tx.source_account_hint):
            tx.mark_manual("unknown_source_account")
        if tx.direction == Direction.EXPENSE and tx.expense_account == self.config.default_expense_account:
            tx.review_level = max(tx.review_level, ReviewLevel.CHECK)
            tx.review_reason.add("default_expense_account")

    def _apply_payment_discount_metadata(self, tx: BillTransaction) -> None:
        if tx.metadata.get("discount_amount"):
            tx.fill_default_discount_account(self.config.discount_income_account)
            tx.discount_amount = str(tx.metadata["discount_amount"])
            tx.review_reason.add("payment_discount")
        elif tx.metadata.get("discount_amount_unknown") == "true":
            tx.fill_default_discount_account(self.config.discount_income_account)
            tx.review_level = ReviewLevel.MANUAL
            tx.review_reason.add("payment_discount_amount_unknown")
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
            if tx.direction != Direction.EXPENSE and not tx.is_credit_card_repayment_credit():
                continue
            if tx.review_reason.has("unionpay_via_tenpay_missing_from_wechat"):
                continue
            candidates = detailed.get(tx.same_money_day_key(), [])
            merchant = tx.payee.replace("财付通-", "").replace("支付宝-", "")
            for candidate in candidates:
                if (
                    tx.is_credit_card_repayment_credit()
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
                tx.review_level = max(tx.review_level, ReviewLevel.CHECK)
                tx.review_reason.add("ambiguous_same_amount_duplicate")
        for tx in self.txs:
            if (
                tx.is_payment_platform_credit_card_repayment()
                and tx.action == "transfer"
                and not tx.expense_account
            ):
                tx.action = "post"
                tx.expense_account = self.config.suspense_account
                tx.flag_account(self.config.suspense_account)

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
        if candidate.is_family_card() and candidate.uid in self.used_family_card_uids:
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
        if candidate.is_family_card():
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
        if not original or original in {"/", "亲情卡", "亲属卡", "亲属卡交易"}:
            family_tx.narration = credit_tx.payee
        family_tx.metadata["matched_credit_payee"] = credit_tx.payee
        family_tx.metadata["matched_credit_type"] = credit_tx.narration
        family_tx.review_reason.add("enriched_from_credit_card")


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
        merged_rows.append(merge_previous_review_row(row, previous))
    return merged_rows


def merge_previous_review_row(row: ReviewRow, previous: ReviewRow) -> ReviewRow:
    if is_review_ok(previous) or not is_review_ok(row):
        merged = ReviewRow.from_dict(row.to_dict())
        for field in REVIEW_FIELDS:
            setattr(merged, field, getattr(previous, field))
        merged.uid = row.uid
        return merged

    merged = ReviewRow.from_dict(row.to_dict())
    for field in ("notes", "tags", "links"):
        previous_value = getattr(previous, field)
        if previous_value:
            setattr(merged, field, previous_value)
    reasons = merged.reasons
    reasons.add("regenerated_ok_over_previous_review")
    merged.review_reason = str(reasons)
    return merged


def is_review_ok(row: ReviewRow) -> bool:
    return row.review_level == ReviewLevel.OK.value


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
        and not row.is_invest()
        and not row.is_skip_ok()
        and row.uid not in duplicate_uids
    ]
    invest = [
        row
        for row in ordered
        if row.is_invest()
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
    for row in invest:
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
