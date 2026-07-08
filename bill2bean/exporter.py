from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .accounts import account_kind
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
    include_files: list[str] | None = None,
    operating_currency: str = "",
    investment_header_options: bool = False,
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> None:
    all_rows = read_review_csv(review_csv)
    text = render_beancount(
        all_rows,
        include_accounts=include_accounts,
        include_files=include_files,
        operating_currency=operating_currency,
        investment_header_options=investment_header_options,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=start_date,
        end_date=end_date,
    )
    Path(output).write_text(text, encoding="utf-8")


def render_beancount(
    rows: list[ReviewRow],
    include_accounts: str = "",
    include_files: list[str] | None = None,
    operating_currency: str = "",
    investment_header_options: bool = False,
    include_sources: set[str] | None = None,
    exclude_sources: set[str] | None = None,
    start_date: str = "",
    end_date: str = "",
) -> str:
    all_rows = rows
    cashback_by_parent = cashback_mapping(all_rows)
    filtered = filter_review_rows(
        all_rows,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=start_date,
        end_date=end_date,
    )

    chunks: list[str] = []
    for row in filtered:
        draft = _build_transaction_draft(row, cashback_by_parent.get(row["uid"], []))
        if not draft:
            continue
        chunk = _format_transaction(draft)
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
        row_date = row.posting_date
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


def _format_header(
    include_accounts: str,
    include_files: list[str],
    operating_currency: str,
    investment_header_options: bool,
) -> list[str]:
    lines: list[str] = []
    if include_accounts:
        lines.append(f'include "{_escape_directive_value(include_accounts)}"')
    for include_file in include_files:
        if include_file:
            lines.append(f'include "{_escape_directive_value(include_file)}"')
    if operating_currency:
        lines.append(
            f'option "operating_currency" "{_escape_directive_value(operating_currency)}"'
        )
    if include_accounts or include_files or operating_currency or investment_header_options:
        lines.append('option "render_commas" "True"')
    if investment_header_options:
        lines.append('option "booking_method" "FIFO"')
        lines.append('option "infer_tolerance_from_cost" "TRUE"')
    if lines:
        lines.append("")
    return lines


def _build_transaction_draft(
    row: ReviewRow,
    cashbacks: list[ReviewRow],
) -> TransactionDraft | None:
    action = (row.action or "post").strip()
    if action in {"skip", "merge_cashback", "invest"}:
        return None
    if action not in {"post", "reimburse", "transfer"}:
        raise ValueError(f"unsupported action {action!r}{_row_context(row)}")

    amount = _decimal_field(row, "amount")
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

    direction = row.get("direction")
    if action == "reimburse":
        if direction != "expense":
            raise ValueError(f"action='reimburse' requires direction='expense'{_row_context(row)}")
        postings = _build_reimburse_postings(row, cashbacks, amount, currency)
    elif action == "transfer":
        if direction not in {"transfer", "income"}:
            raise ValueError(
                "action='transfer' requires direction='transfer' or direction='income'"
                f"{_row_context(row)}"
            )
        postings = _build_transfer_postings(row, amount, currency)
    elif direction == "expense":
        postings = _build_expense_postings(row, cashbacks, amount, currency)
    elif direction == "income":
        postings = _build_income_postings(row, amount, currency)
    elif direction == "transfer":
        postings = _build_transfer_postings(row, amount, currency)
    else:
        raise ValueError(f"unsupported direction {direction!r}{_row_context(row)}")
    return _draft(row, metadata, postings)


def _build_reimburse_postings(
    row: ReviewRow,
    cashbacks: list[ReviewRow],
    amount: Decimal,
    currency: str,
) -> list[Posting]:
    _reject_present_fields(
        row,
        ("share", "share_amount", "discount_amount", "commission_amount", "original_amount", "original_currency"),
        "reimburse rows",
    )
    postings: list[Posting] = []
    source_amount = amount
    aa_amount = _nonnegative_decimal_field(row, "aa_amount", "0")
    reimbursable_amount = amount - aa_amount
    if reimbursable_amount:
        postings.append(
            _posting(row, _required_account(row, "receivable_account"), reimbursable_amount, currency)
        )
    if aa_amount:
        postings.append(_posting(row, _required_account(row, "aa_account"), aa_amount, currency))
    source_amount = _apply_cashbacks(row, cashbacks, postings, source_amount, currency)
    postings.append(_posting(row, _required_account(row, "source_account"), -source_amount, currency))
    return postings


def _build_expense_postings(
    row: ReviewRow,
    cashbacks: list[ReviewRow],
    amount: Decimal,
    currency: str,
) -> list[Posting]:
    _reject_present_fields(row, ("commission_amount",), "expense rows")
    postings: list[Posting] = []
    source_amount = amount
    discount_amount = parse_deduction_discount_amount(
        row.get("discount_amount", ""),
        row.get("uid", ""),
    )
    gross_amount = amount + discount_amount if amount >= 0 else amount - discount_amount
    aa_amount = _nonnegative_decimal_field(row, "aa_amount", "0")
    share_amount = _share_receivable_amount(row, amount - aa_amount)
    personal_amount = gross_amount - aa_amount - share_amount
    if aa_amount or share_amount:
        if personal_amount:
            postings.append(_posting(row, _required_account(row, "expense_account"), personal_amount, currency))
        if aa_amount:
            postings.append(_posting(row, _required_account(row, "aa_account"), aa_amount, currency))
        if share_amount:
            postings.append(_posting(row, _required_account(row, "share_account"), share_amount, currency))
    else:
        postings.append(_expense_posting(row, _required_account(row, "expense_account"), gross_amount, currency))
    if discount_amount:
        postings.append(
            _posting(
                row,
                _required_account(row, "discount_account"),
                -discount_amount,
                currency,
            )
        )
    source_amount = _apply_cashbacks(row, cashbacks, postings, source_amount, currency)
    postings.append(_posting(row, _required_account(row, "source_account"), -source_amount, currency))
    return postings


def _build_income_postings(
    row: ReviewRow,
    amount: Decimal,
    currency: str,
) -> list[Posting]:
    _reject_present_fields(
        row,
        ("aa_amount", "share", "share_amount", "discount_amount", "commission_amount"),
        "income rows",
    )
    _reject_priced_income_row(row)
    return [
        _posting(row, _required_account(row, "source_account"), amount, currency),
        _posting(row, _required_account(row, "income_account"), -amount, currency),
    ]


def _build_transfer_postings(
    row: ReviewRow,
    amount: Decimal,
    currency: str,
) -> list[Posting]:
    _reject_present_fields(row, ("aa_amount", "share", "share_amount"), "transfer rows")
    target_field, source_field = _transfer_account_fields(row)
    postings: list[Posting] = []
    discount_amount = parse_deduction_discount_amount(
        row.get("discount_amount", ""),
        row.get("uid", ""),
    )
    commission_amount = _nonnegative_decimal_field(row, "commission_amount", "0")
    original_price = _transfer_original_price(row, currency)
    if original_price:
        _reject_original_transfer_adjustments(row, discount_amount, commission_amount)
        original_amount, original_currency = original_price
        postings.append(
            _priced_transfer_target_posting(
                row,
                _required_account(row, target_field),
                amount,
                currency,
                original_amount,
                original_currency,
            )
        )
        postings.append(_posting(row, _required_account(row, source_field), -original_amount, original_currency))
        return postings

    target_amount = amount + discount_amount
    postings.append(_posting(row, _required_account(row, target_field), target_amount, currency))
    if discount_amount:
        postings.append(
            _posting(
                row,
                _required_account(row, "discount_account"),
                -discount_amount,
                currency,
            )
        )
    if commission_amount:
        postings.append(
            _posting(
                row,
                _required_account(row, "commission_account"),
                commission_amount,
                currency,
            )
        )
    postings.append(_posting(row, _required_account(row, source_field), -(amount + commission_amount), currency))
    return postings


def _transfer_account_fields(row: ReviewRow) -> tuple[str, str]:
    if row.direction == "income":
        return "source_account", "income_account"
    return "expense_account", "source_account"


def _draft(
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
        tags_links=_tags_links(row),
    )


def _required_account(row: ReviewRow, field: str) -> str:
    account = row.get(field, "")
    if not account:
        raise ValueError(f"missing required {field}{_row_context(row)}")
    return account


def _apply_cashbacks(
    row: ReviewRow,
    cashbacks: list[ReviewRow],
    postings: list[Posting],
    source_amount: Decimal,
    currency: str,
) -> Decimal:
    for cashback in cashbacks:
        cb_amount = _decimal_field(cashback, "amount")
        source_amount -= cb_amount
        income_account = _required_account(cashback, "income_account")
        postings.append(_posting(row, income_account, -cb_amount, currency))
    return source_amount


def _format_transaction(draft: TransactionDraft) -> str:
    payee = _quote(draft.payee)
    narration = _quote(draft.narration)
    suffix = f" {draft.tags_links}" if draft.tags_links else ""
    lines = [f"{draft.date} * {payee} {narration}{suffix}"]
    for key, value in draft.metadata:
        lines.append(f"  {key}: {_quote(value)}")
    implicit_index = _implicit_posting_index(draft.postings)
    for index, posting in enumerate(draft.postings):
        flag = f"{posting.flag} " if posting.flag else ""
        line = f"  {flag}{posting.account}"
        if index != implicit_index:
            line += f"  {posting.amount:.2f} {posting.currency}"
        if posting.total_price_amount is not None and posting.total_price_currency:
            line += f" @@ {posting.total_price_amount:.2f} {posting.total_price_currency}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _implicit_posting_index(postings: list[Posting]) -> int | None:
    if len(postings) != 2:
        return None
    if any(posting.total_price_amount is not None for posting in postings):
        return None
    if postings[0].currency != postings[1].currency:
        return None

    left, right = postings
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


def _quote(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_directive_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


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
    share_amount = (row.get("share_amount") or "").strip()
    if share_amount and share != "custom":
        raise ValueError(f"share_amount is only allowed with share='custom'{_row_context(row)}")
    if not share:
        return Decimal("0")
    if share == "split":
        return (share_base / Decimal("2")).quantize(Decimal("0.01"))
    if share == "whole":
        return share_base
    if share == "custom":
        if not share_amount:
            raise ValueError(f"share='custom' requires share_amount{_row_context(row)}")
        return _nonnegative_decimal_field(row, "share_amount")
    raise ValueError(f"unsupported share value {share!r}{_row_context(row)}")


def _expense_posting(
    row: ReviewRow,
    account: str,
    amount: Decimal,
    currency: str,
) -> Posting:
    original_amount = row.get("original_amount")
    original_currency = row.get("original_currency")
    if bool(original_amount) != bool(original_currency):
        raise ValueError(f"original_amount and original_currency must be set together{_row_context(row)}")
    if original_amount and original_currency and original_currency != currency:
        converted_amount = _decimal_field(row, "original_amount")
        if amount < 0 < converted_amount:
            converted_amount = -converted_amount
        return Posting(
            account,
            converted_amount,
            original_currency,
            amount,
            currency,
            _posting_flag(row, account),
        )
    return _posting(row, account, amount, currency)


def _priced_transfer_target_posting(
    row: ReviewRow,
    account: str,
    amount: Decimal,
    currency: str,
    original_amount: Decimal,
    original_currency: str,
) -> Posting:
    return Posting(
        account,
        amount,
        currency,
        original_amount,
        original_currency,
        _posting_flag(row, account),
    )


def _transfer_original_price(row: ReviewRow, currency: str) -> tuple[Decimal, str] | None:
    original_amount = row.get("original_amount")
    original_currency = row.get("original_currency")
    if original_amount:
        original_currency = original_currency or "CNY"
        if original_currency == currency:
            raise ValueError(
                "transfer row original_currency matches currency"
                f"{_row_context(row)}; remove original_amount/original_currency or correct the currency"
            )
        return _decimal_field(row, "original_amount"), original_currency
    if original_currency:
        raise ValueError(f"original_amount and original_currency must be set together{_row_context(row)}")
    return None


def _reject_original_transfer_adjustments(
    row: ReviewRow,
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
        f"original_amount/original_currency{_row_context(row)}"
    )


def _reject_priced_income_row(row: ReviewRow) -> None:
    if not row.get("original_amount") and not row.get("original_currency"):
        return
    if row.get("original_amount") and row.get("original_currency"):
        raise ValueError(
            "income row with original_amount/original_currency is ambiguous"
            f"{_row_context(row)}; convert it to action=transfer,direction=transfer and set "
            "source_account and expense_account, or use action=transfer,direction=income with "
            "income_account as the transfer source"
        )
    raise ValueError(f"original_amount and original_currency must be set together{_row_context(row)}")


def _reject_present_fields(
    row: ReviewRow,
    fields: tuple[str, ...],
    context: str,
) -> None:
    present = [field for field in fields if (row.get(field) or "").strip()]
    if not present:
        return
    raise ValueError(
        f"{', '.join(present)} {'is' if len(present) == 1 else 'are'} "
        f"not supported on {context}{_row_context(row)}"
    )


def _posting(
    row: ReviewRow,
    account: str,
    amount: Decimal,
    currency: str,
) -> Posting:
    return Posting(account, amount, currency, flag=_posting_flag(row, account))


def _posting_flag(row: ReviewRow, account: str) -> str:
    if row.review_level == "manual" and account in row.reasons.flagged_accounts():
        return "!"
    return ""


def _decimal_field(row: ReviewRow, field: str, default: str | None = None) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        if default is None:
            raise ValueError(f"missing {field}{_row_context(row)}")
        value = default
    try:
        return Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal {field}={value!r}{_row_context(row)}") from exc


def _nonnegative_decimal_field(
    row: ReviewRow,
    field: str,
    default: str | None = None,
) -> Decimal:
    value = _decimal_field(row, field, default)
    if value < 0:
        raise ValueError(f"{field} must be non-negative{_row_context(row)}")
    return value


def _row_context(row: ReviewRow) -> str:
    if row.get("uid"):
        return f" for row {row.get('uid')}"
    details = " ".join(part for part in [row.get("time"), row.get("payee")] if part)
    return f" for {details}" if details else ""
