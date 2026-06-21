from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .model import BillTransaction


@dataclass
class RegexRule:
    pattern: str
    account: str

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE) is not None


@dataclass
class Config:
    default_expense_account: str
    default_income_account: str
    manual_expense_account: str
    manual_income_account: str
    cashback_income_account: str
    aa_account: str
    receivable_account: str
    family_card_receivable_account: str
    discount_income_account: str
    account_rules: list[RegexRule]
    expense_rules: list[RegexRule]
    income_rules: list[RegexRule]
    manual_review_patterns: list[str]

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        return cls(
            default_expense_account=defaults.get("expense_account", "Expenses:Unknown"),
            default_income_account=defaults.get("income_account", "Income:Unknown"),
            manual_expense_account=defaults.get(
                "manual_expense_account",
                defaults.get("expense_account", "Expenses:Unknown"),
            ),
            manual_income_account=defaults.get(
                "manual_income_account",
                defaults.get("income_account", "Income:Unknown"),
            ),
            cashback_income_account=defaults.get("cashback_income_account", "Income:Rebate:Bank"),
            aa_account=defaults.get("aa_account", "Assets:Receivables:Other"),
            receivable_account=defaults.get("receivable_account", "Assets:Receivables:Employer"),
            family_card_receivable_account=defaults.get(
                "family_card_receivable_account",
                "Assets:Receivables:Partner",
            ),
            discount_income_account=defaults.get("discount_income_account", "Income:Other"),
            account_rules=[
                RegexRule(r["pattern"], r["account"]) for r in data.get("account_rules", [])
            ],
            expense_rules=[
                RegexRule(r["pattern"], r["account"]) for r in data.get("expense_rules", [])
            ],
            income_rules=[
                RegexRule(r["pattern"], r["account"]) for r in data.get("income_rules", [])
            ],
            manual_review_patterns=data.get("manual_review", {}).get("patterns", []),
        )

    def text_for(self, tx: BillTransaction) -> str:
        parts = [
            tx.source,
            tx.payee,
            tx.narration,
            tx.source_account_hint,
            str(tx.metadata),
        ]
        return " ".join(p for p in parts if p)

    def source_account_for(self, tx: BillTransaction) -> str:
        text = self.text_for(tx)
        account = self.account_for_text(text)
        if account:
            return account
        return tx.source_account_hint or "Assets:Unknown"

    def account_for_text(self, text: str) -> str:
        for rule in self.account_rules:
            if rule.matches(text):
                return rule.account
        return ""

    def expense_account_for(self, tx: BillTransaction) -> str:
        text = self.text_for(tx)
        for rule in self.expense_rules:
            if rule.matches(text):
                return rule.account
        return self.default_expense_account

    def income_account_for(self, tx: BillTransaction) -> str:
        text = self.text_for(tx)
        for rule in self.income_rules:
            if rule.matches(text):
                return rule.account
        return self.default_income_account

    def needs_manual_review(self, tx: BillTransaction) -> str:
        text = self.text_for(tx)
        for pattern in self.manual_review_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        return ""
