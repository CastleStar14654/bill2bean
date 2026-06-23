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
class MerchantAliasRule:
    pattern: str
    aliases: list[str]

    def matches_pair(self, left: str, right: str) -> bool:
        pattern_in_left = re.search(self.pattern, left, re.IGNORECASE) is not None
        pattern_in_right = re.search(self.pattern, right, re.IGNORECASE) is not None
        alias_in_left = any(re.search(alias, left, re.IGNORECASE) for alias in self.aliases)
        alias_in_right = any(re.search(alias, right, re.IGNORECASE) for alias in self.aliases)
        return (pattern_in_left and alias_in_right) or (pattern_in_right and alias_in_left)


@dataclass
class CreditCardCashbackRule:
    merchant_pattern: str
    card_pattern: str = ""

    def matches(self, tx: BillTransaction) -> bool:
        if re.search(self.merchant_pattern, tx.payee, re.IGNORECASE) is None:
            return False
        if self.card_pattern and re.search(
            self.card_pattern, tx.metadata.get("card", ""), re.IGNORECASE
        ) is None:
            return False
        return True


@dataclass
class Config:
    default_expense_account: str
    default_income_account: str
    suspense_account: str
    cashback_income_account: str
    aa_account: str
    receivable_account: str
    family_card_receivable_account: str
    discount_income_account: str
    account_rules: list[RegexRule]
    expense_rules: list[RegexRule]
    income_rules: list[RegexRule]
    merchant_alias_rules: list[MerchantAliasRule]
    credit_card_cashback_rules: list[CreditCardCashbackRule]
    manual_review_patterns: list[str]

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        return cls(
            default_expense_account=defaults.get("expense_account", "Expenses:Other"),
            default_income_account=defaults.get("income_account", "Income:Other"),
            suspense_account=defaults.get("suspense_account", "Assets:Unknown"),
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
            merchant_alias_rules=[
                MerchantAliasRule(r["pattern"], _list_value(r, "aliases", "alias"))
                for r in data.get("merchant_alias_rules", [])
            ],
            credit_card_cashback_rules=[
                CreditCardCashbackRule(
                    r["merchant_pattern"],
                    r.get("card_pattern", ""),
                )
                for r in data.get("credit_card_cashback_rules", [])
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
        return tx.source_account_hint or self.suspense_account

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

    def same_merchant_text(self, left: str, right: str) -> bool:
        return any(rule.matches_pair(left, right) for rule in self.merchant_alias_rules)

    def is_credit_card_cashback(self, tx: BillTransaction) -> bool:
        return any(rule.matches(tx) for rule in self.credit_card_cashback_rules)


def _list_value(data: dict, list_key: str, scalar_key: str) -> list[str]:
    values = data.get(list_key)
    if values is None and scalar_key in data:
        values = [data[scalar_key]]
    return [str(value) for value in values or []]
