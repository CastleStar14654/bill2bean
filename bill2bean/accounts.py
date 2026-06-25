from __future__ import annotations


ACCOUNT_ROOTS = {
    "Assets",
    "Liabilities",
    "Equity",
    "Income",
    "Expenses",
}


def account_kind(account: str) -> str:
    return account.split(":", 1)[0].lower()


def is_account_name(value: str) -> bool:
    return value.split(":", 1)[0] in ACCOUNT_ROOTS
