from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cached_property
from pathlib import Path
import re

from .accounts import account_kind
from .beancount_format import escape_directive_value, format_tags_links, quote
from .discounts import parse_deduction_discount_amount
from .ledger import ReviewRow, read_review_csv


@dataclass(frozen=True)
class Posting:
    account: str
    amount: Decimal
    currency: str
    total_price_amount: Decimal | None = None
    total_price_currency: str = ""
    flag: str = ""

    @classmethod
    def plain(
        cls,
        row: ReviewRow,
        account: str,
        amount: Decimal,
        currency: str,
    ) -> "Posting":
        return cls(account, amount, currency, flag=cls.flag_for(row, account))

    @classmethod
    def from_expense_amount_fields(
        cls,
        row: ReviewRow,
        account: str,
        amount: Decimal,
        currency: str,
    ) -> "Posting":
        original_amount = row.get("original_amount")
        original_currency = row.get("original_currency")
        if bool(original_amount) != bool(original_currency):
            raise ValueError(
                f"original_amount and original_currency must be set together{row.context()}"
            )
        if original_amount and original_currency and original_currency != currency:
            original_posting_amount = row.decimal_field("original_amount")
            if amount < 0 < original_posting_amount:
                original_posting_amount = -original_posting_amount
                amount = -amount
            return cls.with_total_price(
                row,
                account,
                original_posting_amount,
                original_currency,
                amount,
                currency,
            )
        return cls.plain(row, account, amount, currency)

    @classmethod
    def with_total_price(
        cls,
        row: ReviewRow,
        account: str,
        amount: Decimal,
        currency: str,
        total_price_amount: Decimal,
        total_price_currency: str,
    ) -> "Posting":
        return cls(
            account,
            amount,
            currency,
            total_price_amount,
            total_price_currency,
            cls.flag_for(row, account),
        )

    @staticmethod
    def flag_for(row: ReviewRow, account: str) -> str:
        if row.review_level == "manual" and account in row.reasons.flagged_accounts():
            return "!"
        return ""


@dataclass(frozen=True)
class TransactionDraft:
    date: str
    payee: str
    narration: str
    metadata: list[tuple[str, str]]
    postings: list[Posting]
    tags_links: str = ""

    def format(self) -> str:
        payee = quote(self.payee)
        narration = quote(self.narration)
        suffix = f" {self.tags_links}" if self.tags_links else ""
        lines = [f"{self.date} * {payee} {narration}{suffix}"]
        for key, value in self.metadata:
            lines.append(f"  {key}: {quote(value)}")
        implicit_index = self.implicit_posting_index()
        for index, posting in enumerate(self.postings):
            flag = f"{posting.flag} " if posting.flag else ""
            line = f"  {flag}{posting.account}"
            if index != implicit_index:
                line += f"  {posting.amount:.2f} {posting.currency}"
            if posting.total_price_amount is not None and posting.total_price_currency:
                line += f" @@ {posting.total_price_amount:.2f} {posting.total_price_currency}"
            lines.append(line)
        return "\n".join(lines) + "\n"

    def implicit_posting_index(self) -> int | None:
        if len(self.postings) != 2:
            return None
        if any(posting.total_price_amount is not None for posting in self.postings):
            return None
        if self.postings[0].currency != self.postings[1].currency:
            return None

        left, right = self.postings
        left_kind = account_kind(left.account)
        right_kind = account_kind(right.account)
        if left_kind == "expenses" and right_kind in {"assets", "liabilities"}:
            return 1
        if right_kind == "expenses" and left_kind in {"assets", "liabilities"}:
            return 0
        if left_kind in {"assets", "liabilities"} and right_kind == "income":
            return 1
        if right_kind in {"assets", "liabilities"} and left_kind == "income":
            return 0
        if left_kind in {"assets", "liabilities"} and right_kind in {"assets", "liabilities"}:
            return 1
        return None


@dataclass(frozen=True)
class ExportDefaults:
    aa_account: str = ""
    receivable_account: str = ""
    share_account: str = ""
    discount_account: str = ""
    commission_account: str = ""

    @classmethod
    def from_config(cls, config) -> "ExportDefaults":
        return cls(
            aa_account=config.aa_account,
            receivable_account=config.reimburse_account,
            share_account=config.default_share_account,
            discount_account=config.discount_income_account,
            commission_account=config.default_commission_account,
        )

    def account_for(self, field: str) -> str:
        return getattr(self, field, "")


def export_beancount(
    review_csv: str | Path,
    output: str | Path,
    include_accounts: str = "",
    include_files: list[str] | None = None,
    operating_currency: str = "",
    investment_header_options: bool = False,
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    defaults: ExportDefaults | None = None,
) -> None:
    all_rows = read_review_csv(review_csv)
    text = BeanExporter(
        defaults or ExportDefaults(),
        rows=all_rows,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=start_date,
        end_date=end_date,
    ).render(
        include_accounts=include_accounts,
        include_files=include_files,
        operating_currency=operating_currency,
        investment_header_options=investment_header_options,
    )
    Path(output).write_text(text, encoding="utf-8")


def _validate_date_range(start_date: str, end_date: str) -> None:
    parsed_start = _parse_filter_date(start_date, "--start-date")
    parsed_end = _parse_filter_date(end_date, "--end-date")
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise ValueError("--start-date cannot be later than --end-date")


def _parse_filter_date(value: str, option: str) -> date | None:
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{option} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{option} is not a valid date") from exc


def cashback_mapping(rows: list[ReviewRow]) -> dict[str, list[ReviewRow]]:
    duplicate_redirects = _duplicate_parent_redirects(rows)
    cashbacks: dict[str, list[ReviewRow]] = {}
    for row in rows:
        if row.action != "merge_cashback":
            continue
        parent_uid = row.reasons.cashback_parent_uid()
        if parent_uid:
            parent_uid = _redirect_parent_uid(parent_uid, duplicate_redirects)
            cashbacks.setdefault(parent_uid, []).append(row)
    return cashbacks


def _duplicate_parent_redirects(rows: list[ReviewRow]) -> dict[str, str]:
    redirects: dict[str, str] = {}
    for row in rows:
        if not row.uid:
            continue
        parent_uid = row.duplicate_parent_uid()
        if parent_uid:
            redirects[row.uid] = parent_uid
    return redirects


def _redirect_parent_uid(parent_uid: str, redirects: dict[str, str]) -> str:
    seen: set[str] = set()
    while parent_uid in redirects and parent_uid not in seen:
        seen.add(parent_uid)
        parent_uid = redirects[parent_uid]
    return parent_uid


def _format_header(
    include_accounts: str,
    include_files: list[str],
    operating_currency: str,
    investment_header_options: bool,
) -> list[str]:
    lines: list[str] = []
    if include_accounts:
        lines.append(f'include "{escape_directive_value(include_accounts)}"')
    for include_file in include_files:
        if include_file:
            lines.append(f'include "{escape_directive_value(include_file)}"')
    if operating_currency:
        lines.append(
            f'option "operating_currency" "{escape_directive_value(operating_currency)}"'
        )
    if include_accounts or include_files or operating_currency or investment_header_options:
        lines.append('option "render_commas" "True"')
    if investment_header_options:
        lines.append('option "booking_method" "FIFO"')
        lines.append('option "infer_tolerance_from_cost" "TRUE"')
    if lines:
        lines.append("")
    return lines


@dataclass(frozen=True)
class BeanExporter:
    defaults: ExportDefaults = field(default_factory=ExportDefaults)
    rows: list[ReviewRow] = field(default_factory=list)
    include_sources: set[str] | None = None
    exclude_sources: set[str] | None = None
    start_date: str = ""
    end_date: str = ""

    @cached_property
    def filtered_rows(self) -> list[ReviewRow]:
        _validate_date_range(self.start_date, self.end_date)
        include_sources = self.include_sources or set()
        exclude_sources = self.exclude_sources or set()
        filtered: list[ReviewRow] = []
        for row in self.rows:
            source = row.source
            if include_sources and source not in include_sources:
                continue
            if exclude_sources and source in exclude_sources:
                continue
            row_date = row.posting_date
            if self.start_date and row_date < self.start_date:
                continue
            if self.end_date and row_date > self.end_date:
                continue
            filtered.append(row)
        return filtered

    @cached_property
    def cashback_by_parent(self) -> dict[str, list[ReviewRow]]:
        return cashback_mapping(self.rows)

    def render(
        self,
        include_accounts: str = "",
        include_files: list[str] | None = None,
        operating_currency: str = "",
        investment_header_options: bool = False,
    ) -> str:
        chunks: list[str] = []
        for row in self.filtered_rows:
            draft = self.build_transaction_draft(
                row,
                self.cashback_by_parent.get(row["uid"], []),
            )
            if not draft:
                continue
            chunk = draft.format()
            if chunk:
                chunks.append(chunk)
        parts = _format_header(
            include_accounts,
            include_files or [],
            operating_currency,
            investment_header_options,
        )
        parts.extend(chunks)
        return "\n".join(parts).rstrip() + "\n"

    def required_accounts(self) -> set[str]:
        accounts: set[str] = set()
        for row in self.filtered_rows:
            draft = self.build_transaction_draft(
                row,
                self.cashback_by_parent.get(row["uid"], []),
            )
            if not draft:
                continue
            accounts.update(posting.account for posting in draft.postings if posting.account)
        return accounts

    def build_transaction_draft(
        self,
        row: ReviewRow,
        cashbacks: list[ReviewRow],
    ) -> TransactionDraft | None:
        action = (row.action or "post").strip()
        if action in {"skip", "merge_cashback", "invest"}:
            return None
        if action not in {"post", "reimburse", "transfer"}:
            raise ValueError(f"unsupported action {action!r}{row.context()}")

        amount = row.decimal_field("amount")
        currency = row.get("currency") or "CNY"
        metadata: list[tuple[str, str]] = []
        if row.get("source") or row.get("uid"):
            metadata.append(("source", row.get("source", "")))
            metadata.append(("import_id", row.get("uid", "")))
        if row.get("notes"):
            metadata.append(("note", row["notes"]))
        if action == "reimburse":
            if row.get("share") or row.get("share_amount"):
                metadata.append(("warning", "reimburse_overrides_share"))
        elif row.get("share"):
            metadata.append(("share", row["share"]))
            if row.get("share_account"):
                metadata.append(("share_account", row["share_account"]))
            if row.get("share_amount"):
                metadata.append(("share_amount", row["share_amount"]))

        builder = self.posting_builder_for(row, cashbacks, amount, currency)
        postings = builder.build()
        return self.draft(row, metadata, postings)

    def posting_builder_for(
        self,
        row: ReviewRow,
        cashbacks: list[ReviewRow],
        amount: Decimal,
        currency: str,
    ) -> TransactionPostings:
        direction = row.get("direction")
        if cashbacks and direction != "expense":
            raise ValueError(
                f"merge_cashback rows can only attach to expense rows{row.context()}"
            )
        if row.action == "reimburse":
            if direction != "expense":
                raise ValueError(
                    f"action='reimburse' requires direction='expense'{row.context()}"
                )
            return OutflowPostings(
                self,
                row,
                amount,
                currency,
                cashbacks,
                target_account_field="receivable_account",
                allow_share=False,
                rejected_fields=("share", "share_amount", "commission_amount"),
                reject_context="reimburse rows",
            )
        if row.action == "transfer":
            if direction not in {"transfer", "income"}:
                raise ValueError(
                    "action='transfer' requires direction='transfer' or direction='income'"
                    f"{row.context()}"
                )
            return TransferPostings(self, row, amount, currency)
        if direction == "expense":
            return OutflowPostings(
                self,
                row,
                amount,
                currency,
                cashbacks,
                target_account_field="expense_account",
                allow_share=True,
                rejected_fields=("commission_amount",),
                reject_context="expense rows",
            )
        if direction == "income":
            return IncomePostings(self, row, amount, currency)
        if direction == "transfer":
            return TransferPostings(self, row, amount, currency)
        raise ValueError(f"unsupported direction {direction!r}{row.context()}")

    def draft(
        self,
        row: ReviewRow,
        metadata: list[tuple[str, str]],
        postings: list[Posting],
    ) -> TransactionDraft:
        return TransactionDraft(
            date=row.posting_date,
            payee=row["payee"],
            narration=row["narration"],
            metadata=metadata,
            postings=postings,
            tags_links=format_tags_links(row.get("tags", ""), row.get("links", "")),
        )

    def required_account(
        self,
        row: ReviewRow,
        field: str,
    ) -> str:
        account = row.get(field, "") or self.defaults.account_for(field)
        if not account:
            raise ValueError(f"missing required {field}{row.context()}")
        return account


@dataclass(frozen=True)
class TransactionPostings:
    exporter: BeanExporter
    row: ReviewRow
    amount: Decimal
    currency: str

    def build(self) -> list[Posting]:
        raise NotImplementedError

    def required_account(self, field: str) -> str:
        return self.exporter.required_account(self.row, field)

    def reject_present_fields(self, fields: tuple[str, ...], context: str) -> None:
        present = [field for field in fields if (self.row.get(field) or "").strip()]
        if not present:
            return
        raise ValueError(
            f"{', '.join(present)} {'is' if len(present) == 1 else 'are'} "
            f"not supported on {context}{self.row.context()}"
        )


@dataclass(frozen=True)
class OutflowPostings(TransactionPostings):
    cashbacks: list[ReviewRow]
    target_account_field: str
    allow_share: bool
    rejected_fields: tuple[str, ...]
    reject_context: str

    def build(self) -> list[Posting]:
        self.reject_present_fields(self.rejected_fields, self.reject_context)
        postings: list[Posting] = []
        source_amount = self.amount
        discount_amount = parse_deduction_discount_amount(
            self.row.get("discount_amount", ""),
            self.row.get("uid", ""),
        )
        gross_amount = (
            self.amount + discount_amount
            if self.amount >= 0
            else self.amount - discount_amount
        )
        aa_amount = self.aa_amount()
        share_amount = self.share_amount(aa_amount)
        self.reject_priced_split(aa_amount, share_amount)
        target_amount = gross_amount - aa_amount - share_amount
        if aa_amount or share_amount:
            if target_amount:
                postings.append(
                    Posting.plain(
                        self.row,
                        self.required_account(self.target_account_field),
                        target_amount,
                        self.currency,
                    )
                )
            if aa_amount:
                postings.append(
                    Posting.plain(
                        self.row,
                        self.required_account("aa_account"),
                        aa_amount,
                        self.currency,
                    )
                )
            if share_amount:
                postings.append(
                    Posting.plain(
                        self.row,
                        self.required_account("share_account"),
                        share_amount,
                        self.currency,
                    )
                )
        else:
            postings.append(
                Posting.from_expense_amount_fields(
                    self.row,
                    self.required_account(self.target_account_field),
                    gross_amount,
                    self.currency,
                )
            )
        if discount_amount:
            postings.append(
                Posting.plain(
                    self.row,
                    self.required_account("discount_account"),
                    -discount_amount,
                    self.currency,
                )
            )
        source_amount = self.apply_cashbacks(postings, source_amount)
        postings.append(
            Posting.plain(
                self.row,
                self.required_account("source_account"),
                -source_amount,
                self.currency,
            )
        )
        return postings

    def aa_amount(self) -> Decimal:
        return self.row.nonnegative_decimal_field("aa_amount", "0")

    def reject_priced_split(self, aa_amount: Decimal, share_amount: Decimal) -> None:
        if not self.row.get("original_amount") and not self.row.get("original_currency"):
            return
        if aa_amount:
            raise ValueError(
                "aa_amount is not supported with original_amount/original_currency"
                f"{self.row.context()}"
            )
        if share_amount:
            raise ValueError(
                f"share is not supported with original_amount/original_currency{self.row.context()}"
            )

    def share_amount(self, aa_amount: Decimal) -> Decimal:
        if not self.allow_share:
            return Decimal("0")

        share = (self.row.get("share") or "").strip().lower()
        share_amount = (self.row.get("share_amount") or "").strip()
        if share_amount and share != "custom":
            raise ValueError(
                f"share_amount is only allowed with share='custom'{self.row.context()}"
            )
        if not share:
            return Decimal("0")
        if share == "split":
            return ((self.amount - aa_amount) / Decimal("2")).quantize(Decimal("0.01"))
        if share == "whole":
            return self.amount - aa_amount
        if share == "custom":
            if not share_amount:
                raise ValueError(f"share='custom' requires share_amount{self.row.context()}")
            return self.row.nonnegative_decimal_field("share_amount")
        raise ValueError(f"unsupported share value {share!r}{self.row.context()}")

    def apply_cashbacks(
        self,
        postings: list[Posting],
        source_amount: Decimal,
    ) -> Decimal:
        for cashback in self.cashbacks:
            cb_amount = cashback.decimal_field("amount")
            source_amount -= cb_amount
            income_account = self.exporter.required_account(cashback, "income_account")
            postings.append(Posting.plain(self.row, income_account, -cb_amount, self.currency))
        return source_amount


@dataclass(frozen=True)
class IncomePostings(TransactionPostings):
    def build(self) -> list[Posting]:
        self.reject_present_fields(
            ("aa_amount", "share", "share_amount", "discount_amount", "commission_amount"),
            "income rows",
        )
        self.reject_priced_income_row()
        return [
            Posting.plain(
                self.row,
                self.required_account("source_account"),
                self.amount,
                self.currency,
            ),
            Posting.plain(
                self.row,
                self.required_account("income_account"),
                -self.amount,
                self.currency,
            ),
        ]

    def reject_priced_income_row(self) -> None:
        if not self.row.get("original_amount") and not self.row.get("original_currency"):
            return
        if self.row.get("original_amount") and self.row.get("original_currency"):
            raise ValueError(
                "income row with original_amount/original_currency is ambiguous"
                f"{self.row.context()}; convert it to action=transfer,direction=transfer "
                "and set source_account and expense_account, or use "
                "action=transfer,direction=income with income_account as the transfer source"
            )
        raise ValueError(
            f"original_amount and original_currency must be set together{self.row.context()}"
        )


@dataclass(frozen=True)
class TransferPostings(TransactionPostings):
    def build(self) -> list[Posting]:
        self.reject_present_fields(("aa_amount", "share", "share_amount"), "transfer rows")
        target_field, source_field = self.account_fields()
        postings: list[Posting] = []
        discount_amount = parse_deduction_discount_amount(
            self.row.get("discount_amount", ""),
            self.row.get("uid", ""),
        )
        commission_amount = self.row.nonnegative_decimal_field("commission_amount", "0")
        original_price = _transfer_original_price(self.row, self.currency)
        if original_price:
            self.reject_original_adjustments(discount_amount, commission_amount)
            original_amount, original_currency = original_price
            postings.append(
                Posting.with_total_price(
                    self.row,
                    self.required_account(target_field),
                    self.amount,
                    self.currency,
                    original_amount,
                    original_currency,
                )
            )
            postings.append(
                Posting.plain(
                    self.row,
                    self.required_account(source_field),
                    -original_amount,
                    original_currency,
                )
            )
            return postings

        if self.row.direction == "income":
            target_amount = self.amount
            source_amount = self.amount - discount_amount + commission_amount
        else:
            target_amount = self.amount + discount_amount - commission_amount
            source_amount = self.amount
        postings.append(
            Posting.plain(
                self.row,
                self.required_account(target_field),
                target_amount,
                self.currency,
            )
        )
        if discount_amount:
            postings.append(
                Posting.plain(
                    self.row,
                    self.required_account("discount_account"),
                    -discount_amount,
                    self.currency,
                )
            )
        if commission_amount:
            postings.append(
                Posting.plain(
                    self.row,
                    self.required_account("commission_account"),
                    commission_amount,
                    self.currency,
                )
            )
        postings.append(
            Posting.plain(
                self.row,
                self.required_account(source_field),
                -source_amount,
                self.currency,
            )
        )
        return postings

    def account_fields(self) -> tuple[str, str]:
        if self.row.direction == "income":
            return "source_account", "income_account"
        return "expense_account", "source_account"

    def reject_original_adjustments(
        self,
        discount_amount: Decimal,
        commission_amount: Decimal,
    ) -> None:
        unsupported_fields = []
        if discount_amount:
            unsupported_fields.append("discount_amount")
        if commission_amount:
            unsupported_fields.append("commission_amount")
        if not unsupported_fields:
            return
        fields = " and ".join(unsupported_fields)
        verb = "is" if len(unsupported_fields) == 1 else "are"
        raise ValueError(
            f"{fields} {verb} not supported on transfer rows with "
            f"original_amount/original_currency{self.row.context()}"
        )


def _transfer_original_price(row: ReviewRow, currency: str) -> tuple[Decimal, str] | None:
    original_amount = row.get("original_amount")
    original_currency = row.get("original_currency")
    if original_amount:
        original_currency = original_currency or "CNY"
        if original_currency == currency:
            raise ValueError(
                "transfer row original_currency matches currency"
                f"{row.context()}; remove original_amount/original_currency or correct the currency"
            )
        return row.decimal_field("original_amount"), original_currency
    if original_currency:
        raise ValueError(f"original_amount and original_currency must be set together{row.context()}")
    return None
