from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import re

from .ledger import read_review_csv


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
        action = (row.get("action") or "post").strip()
        if action in {"skip", "merge_cashback"}:
            continue
        if action not in {"post", "receivable", "transfer"}:
            continue
        chunk = _format_transaction(row, cashback_by_parent.get(row["uid"], []))
        if chunk:
            chunks.append(chunk)
    parts = _format_header(include_accounts, operating_currency)
    parts.extend(chunks)
    Path(output).write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")


def filter_review_rows(
    rows: list[dict[str, str]],
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> list[dict[str, str]]:
    _validate_date_range(start_date, end_date)
    include_sources = include_sources or set()
    exclude_sources = exclude_sources or set()
    filtered: list[dict[str, str]] = []
    for row in rows:
        source = row.get("source", "")
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


def cashback_mapping(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    cashbacks: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row.get("action") != "merge_cashback":
            continue
        for reason in row.get("review_reason", "").split(";"):
            if reason.startswith("cashback_for:"):
                cashbacks.setdefault(reason.removeprefix("cashback_for:"), []).append(row)
    return cashbacks


def required_accounts_for_export(
    rows: list[dict[str, str]],
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
        accounts.update(_posting_accounts(row, cashback_by_parent.get(row["uid"], [])))
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


def _format_transaction(row: dict[str, str], cashbacks: list[dict[str, str]]) -> str:
    amount = Decimal(row["amount"])
    currency = row.get("currency") or "CNY"
    payee = _quote(row["payee"])
    narration = _quote(row["narration"])
    action = (row.get("action") or "post").strip()
    tags_links = _tags_links(row)
    suffix = f" {tags_links}" if tags_links else ""
    lines = [f'{_posting_date(row)} * {payee} {narration}{suffix}']
    if row.get("source") or row.get("uid"):
        lines.append(f'  source: "{row.get("source", "")}"')
        lines.append(f'  import_id: "{row.get("uid", "")}"')
    if row.get("notes"):
        lines.append(f'  note: "{row["notes"]}"')
    if action == "receivable":
        if row.get("share") or row.get("share_amount"):
            lines.append('  warning: "receivable_overrides_share"')
    elif row.get("share"):
        lines.append(f'  share: "{row["share"]}"')
        if row.get("share_account"):
            lines.append(f'  share_account: "{row["share_account"]}"')
        if row.get("share_amount"):
            lines.append(f'  share_amount: "{row["share_amount"]}"')

    direction = row.get("direction")
    if direction == "expense":
        if action == "receivable":
            aa_amount = Decimal(row.get("aa_amount") or "0")
            reimbursable_amount = amount - aa_amount
            if reimbursable_amount:
                lines.append(f'  {row["receivable_account"]}  {reimbursable_amount:.2f} {currency}')
            if aa_amount:
                lines.append(f'  {row["aa_account"]}  {aa_amount:.2f} {currency}')
            lines.append(f'  {row["source_account"]}  {-amount:.2f} {currency}')
            return "\n".join(lines) + "\n"
        source_amount = amount
        discount_amount = Decimal(row.get("discount_amount") or "0")
        gross_amount = amount + discount_amount if amount >= 0 else amount - discount_amount
        aa_amount = Decimal(row.get("aa_amount") or "0")
        share_amount = _share_receivable_amount(row, amount - aa_amount)
        personal_amount = gross_amount - aa_amount - share_amount
        if aa_amount or share_amount:
            if personal_amount:
                lines.append(f'  {row["expense_account"]}  {personal_amount:.2f} {currency}')
            if aa_amount:
                lines.append(f'  {row["aa_account"]}  {aa_amount:.2f} {currency}')
            if share_amount:
                lines.append(f'  {row["share_account"]}  {share_amount:.2f} {currency}')
        else:
            lines.append(f'  {row["expense_account"]}  {gross_amount:.2f} {currency}')
        if discount_amount:
            lines.append(
                f'  {row.get("discount_account") or "Income:Other"}  {-discount_amount:.2f} {currency}'
            )
        for cashback in cashbacks:
            cb_amount = Decimal(cashback["amount"])
            source_amount -= cb_amount
            income_account = cashback.get("income_account") or "Income:Cashback"
            lines.append(f"  {income_account}  -{cb_amount:.2f} {currency}")
        lines.append(f'  {row["source_account"]}  {-source_amount:.2f} {currency}')
    elif direction == "income":
        lines.append(f'  {row["source_account"]}  {amount:.2f} {currency}')
        lines.append(f'  {row["income_account"]}  -{amount:.2f} {currency}')
    elif direction == "transfer":
        if not row.get("expense_account"):
            return ""
        discount_amount = Decimal(row.get("discount_amount") or "0")
        target_amount = amount + discount_amount
        lines.append(f'  {row["expense_account"]}  {target_amount:.2f} {currency}')
        if discount_amount:
            lines.append(
                f'  {row.get("discount_account") or "Income:Other"}  -{discount_amount:.2f} {currency}'
            )
        lines.append(f'  {row["source_account"]}  -{amount:.2f} {currency}')
    else:
        lines.append(f'  {row["source_account"]}  {amount:.2f} {currency}')
    return "\n".join(lines) + "\n"


def _quote(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_directive_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _posting_date(row: dict[str, str]) -> str:
    time_value = row.get("time", "")
    if len(time_value) >= 10:
        return time_value[:10]
    raise ValueError(f"cannot infer posting date for row {row.get('uid', '')}")


def _posting_accounts(row: dict[str, str], cashbacks: list[dict[str, str]]) -> set[str]:
    action = (row.get("action") or "post").strip()
    if action in {"skip", "merge_cashback"} or action not in {"post", "receivable", "transfer"}:
        return set()

    amount = Decimal(row["amount"])
    direction = row.get("direction")
    if direction == "expense":
        if action == "receivable":
            aa_amount = Decimal(row.get("aa_amount") or "0")
            accounts = {row["source_account"]}
            if amount - aa_amount:
                accounts.add(row["receivable_account"])
            if aa_amount:
                accounts.add(row["aa_account"])
            return {account for account in accounts if account}

        accounts = {row["source_account"]}
        aa_amount = Decimal(row.get("aa_amount") or "0")
        discount_amount = Decimal(row.get("discount_amount") or "0")
        share_amount = _share_receivable_amount(row, amount - aa_amount)
        gross_amount = amount + discount_amount if amount >= 0 else amount - discount_amount
        personal_amount = gross_amount - aa_amount - share_amount
        if aa_amount or share_amount:
            if personal_amount:
                accounts.add(row["expense_account"])
            if aa_amount:
                accounts.add(row["aa_account"])
            if share_amount:
                accounts.add(row["share_account"])
        else:
            accounts.add(row["expense_account"])
        if discount_amount:
            accounts.add(row.get("discount_account") or "Income:Other")
        for cashback in cashbacks:
            accounts.add(cashback.get("income_account") or "Income:Cashback")
        return {account for account in accounts if account}

    if direction == "income":
        return {account for account in {row["source_account"], row["income_account"]} if account}
    if direction == "transfer":
        if not row.get("expense_account"):
            return set()
        accounts = {row["source_account"], row["expense_account"]}
        if Decimal(row.get("discount_amount") or "0"):
            accounts.add(row.get("discount_account") or "Income:Other")
        return {account for account in accounts if account}
    return {row["source_account"]} if row.get("source_account") else set()


def _tags_links(row: dict[str, str]) -> str:
    tags = [_prefixed_token(token, "#") for token in _split_tokens(row.get("tags", ""))]
    links = [_prefixed_token(token, "^") for token in _split_tokens(row.get("links", ""))]
    return " ".join(tags + links)


def _split_tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,;]+", value.strip()) if part]


def _prefixed_token(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value
    return prefix + value


def _share_receivable_amount(row: dict[str, str], share_base: Decimal) -> Decimal:
    share = (row.get("share") or "").strip().lower()
    if share == "split":
        return (share_base / Decimal("2")).quantize(Decimal("0.01"))
    if share == "whole":
        return share_base
    if share == "custom" and row.get("share_amount"):
        return Decimal(row["share_amount"])
    return Decimal("0")
