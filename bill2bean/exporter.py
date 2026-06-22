from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import re

from .ledger import ReviewRow, read_review_csv


@dataclass(frozen=True)
class Posting:
    account: str
    amount: Decimal
    currency: str
    total_price_amount: Decimal | None = None
    total_price_currency: str = ""


@dataclass(frozen=True)
class TransactionDraft:
    date: str
    payee: str
    narration: str
    metadata: list[tuple[str, str]]
    postings: list[Posting]
    tags_links: str = ""


def export_beancount(
    review_csv: str | Path,
    output: str | Path,
    include_accounts: str = "",
    operating_currency: str = "",
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> None:
    all_rows = read_review_csv(review_csv)
    cashback_by_parent = cashback_mapping(all_rows)
    rows = filter_review_rows(
        all_rows,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=start_date,
        end_date=end_date,
    )

    chunks: list[str] = []
    for row in rows:
        draft = _build_transaction_draft(row, cashback_by_parent.get(row["uid"], []))
        if not draft:
            continue
        chunk = _format_transaction(draft)
        if chunk:
            chunks.append(chunk)
    parts = _format_header(include_accounts, operating_currency)
    parts.extend(chunks)
    Path(output).write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def filter_review_rows(
    rows: list[ReviewRow],
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> list[ReviewRow]:
    _validate_date_range(start_date, end_date)
    include_sources = include_sources or set()
    exclude_sources = exclude_sources or set()
    filtered: list[ReviewRow] = []
    for row in rows:
        source = row.source
        if include_sources and source not in include_sources:
            continue
        if exclude_sources and source in exclude_sources:
            continue
        row_date = _posting_date(row)
        if start_date and row_date < start_date:
            continue
        if end_date and row_date > end_date:
            continue
        filtered.append(row)
    return filtered


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


def required_accounts_for_export(
    rows: list[ReviewRow],
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> set[str]:
    cashback_by_parent = cashback_mapping(rows)
    filtered = filter_review_rows(
        rows,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=start_date,
        end_date=end_date,
    )
    accounts: set[str] = set()
    for row in filtered:
        draft = _build_transaction_draft(row, cashback_by_parent.get(row["uid"], []))
        if not draft:
            continue
        accounts.update(posting.account for posting in draft.postings if posting.account)
    return accounts


def _format_header(include_accounts: str, operating_currency: str) -> list[str]:
    lines: list[str] = []
    if include_accounts:
        lines.append(f'include "{_escape_directive_value(include_accounts)}"')
    if operating_currency:
        lines.append(
            f'option "operating_currency" "{_escape_directive_value(operating_currency)}"'
        )
    if lines:
        lines.append("")
    return lines


def _build_transaction_draft(
    row: ReviewRow,
    cashbacks: list[ReviewRow],
) -> TransactionDraft | None:
    action = (row.action or "post").strip()
    if action in {"skip", "merge_cashback"} or action not in {"post", "receivable", "transfer"}:
        return None

    amount = _decimal(row["amount"])
    currency = row.get("currency") or "CNY"
    postings: list[Posting] = []
    metadata: list[tuple[str, str]] = []
    if row.get("source") or row.get("uid"):
        metadata.append(("source", row.get("source", "")))
        metadata.append(("import_id", row.get("uid", "")))
    if row.get("notes"):
        metadata.append(("note", row["notes"]))
    if action == "receivable":
        if row.get("share") or row.get("share_amount"):
            metadata.append(("warning", "receivable_overrides_share"))
    elif row.get("share"):
        metadata.append(("share", row["share"]))
        if row.get("share_account"):
            metadata.append(("share_account", row["share_account"]))
        if row.get("share_amount"):
            metadata.append(("share_amount", row["share_amount"]))

    direction = row.get("direction")
    if direction == "expense":
        if action == "receivable":
            aa_amount = _decimal(row.get("aa_amount") or "0")
            reimbursable_amount = amount - aa_amount
            if reimbursable_amount:
                postings.append(Posting(row["receivable_account"], reimbursable_amount, currency))
            if aa_amount:
                postings.append(Posting(row["aa_account"], aa_amount, currency))
            postings.append(Posting(row["source_account"], -amount, currency))
            return _draft(row, metadata, postings)
        source_amount = amount
        discount_amount = _decimal(row.get("discount_amount") or "0")
        gross_amount = amount + discount_amount if amount >= 0 else amount - discount_amount
        aa_amount = _decimal(row.get("aa_amount") or "0")
        share_amount = _share_receivable_amount(row, amount - aa_amount)
        personal_amount = gross_amount - aa_amount - share_amount
        if aa_amount or share_amount:
            if personal_amount:
                postings.append(Posting(row["expense_account"], personal_amount, currency))
            if aa_amount:
                postings.append(Posting(row["aa_account"], aa_amount, currency))
            if share_amount:
                postings.append(Posting(row["share_account"], share_amount, currency))
        else:
            postings.append(_expense_posting(row, row["expense_account"], gross_amount, currency))
        if discount_amount:
            postings.append(
                Posting(row.get("discount_account") or "Income:Other", -discount_amount, currency)
            )
        for cashback in cashbacks:
            cb_amount = _decimal(cashback["amount"])
            source_amount -= cb_amount
            income_account = cashback.get("income_account") or "Income:Cashback"
            income_amount = -cb_amount
            postings.append(Posting(income_account, income_amount, currency))
        postings.append(Posting(row["source_account"], -source_amount, currency))
    elif direction == "income":
        postings.append(Posting(row["source_account"], amount, currency))
        postings.append(Posting(row["income_account"], -amount, currency))
    elif direction == "transfer":
        if not row.get("expense_account"):
            return None
        discount_amount = _decimal(row.get("discount_amount") or "0")
        target_amount = amount + discount_amount
        postings.append(Posting(row["expense_account"], target_amount, currency))
        if discount_amount:
            postings.append(
                Posting(row.get("discount_account") or "Income:Other", -discount_amount, currency)
            )
        postings.append(Posting(row["source_account"], -amount, currency))
    else:
        postings.append(Posting(row["source_account"], amount, currency))
    return _draft(row, metadata, postings)


def _draft(
    row: ReviewRow,
    metadata: list[tuple[str, str]],
    postings: list[Posting],
) -> TransactionDraft:
    return TransactionDraft(
        date=_posting_date(row),
        payee=row["payee"],
        narration=row["narration"],
        metadata=metadata,
        postings=postings,
        tags_links=_tags_links(row),
    )


def _format_transaction(draft: TransactionDraft) -> str:
    payee = _quote(draft.payee)
    narration = _quote(draft.narration)
    suffix = f" {draft.tags_links}" if draft.tags_links else ""
    lines = [f"{draft.date} * {payee} {narration}{suffix}"]
    for key, value in draft.metadata:
        lines.append(f"  {key}: {_quote(value)}")
    for posting in draft.postings:
        line = f"  {posting.account}  {posting.amount:.2f} {posting.currency}"
        if posting.total_price_amount is not None and posting.total_price_currency:
            line += f" @@ {posting.total_price_amount:.2f} {posting.total_price_currency}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_directive_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _posting_date(row: dict[str, str]) -> str:
    return row.posting_date


def _tags_links(row: ReviewRow) -> str:
    tags = [_prefixed_token(token, "#") for token in _split_tokens(row.get("tags", ""))]
    links = [_prefixed_token(token, "^") for token in _split_tokens(row.get("links", ""))]
    return " ".join(tags + links)


def _split_tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,;]+", value.strip()) if part]


def _prefixed_token(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value
    return prefix + value


def _share_receivable_amount(row: ReviewRow, share_base: Decimal) -> Decimal:
    share = (row.get("share") or "").strip().lower()
    if share == "split":
        return (share_base / Decimal("2")).quantize(Decimal("0.01"))
    if share == "whole":
        return share_base
    if share == "custom" and row.get("share_amount"):
        return _decimal(row["share_amount"])
    return Decimal("0")


def _expense_posting(
    row: ReviewRow,
    account: str,
    amount: Decimal,
    currency: str,
) -> Posting:
    original_amount = row.get("original_amount")
    original_currency = row.get("original_currency")
    if original_amount and original_currency and original_currency != currency:
        converted_amount = _decimal(original_amount)
        if amount < 0 < converted_amount:
            converted_amount = -converted_amount
        return Posting(
            account,
            converted_amount,
            original_currency,
            amount,
            currency,
        )
    return Posting(account, amount, currency)


def _decimal(value: str) -> Decimal:
    return Decimal((value or "0").replace(",", ""))
