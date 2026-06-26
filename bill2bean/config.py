from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

from .accounts import is_account_name
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
    income_account: str = ""

    def matches(self, tx: BillTransaction) -> bool:
        if re.search(self.merchant_pattern, tx.payee, re.IGNORECASE) is None:
            return False
        if self.card_pattern and re.search(
            self.card_pattern, tx.metadata.get("card", ""), re.IGNORECASE
        ) is None:
            return False
        return True


@dataclass
class FundSettlementRule:
    pattern: str
    settlement_days: int

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE) is not None


@dataclass
class FundConfig:
    default_account: str
    default_income_account: str
    commission_account: str
    discount_income_account: str
    default_settlement_days: int
    share_precision: int
    accounts: dict[str, str]
    income_accounts: dict[str, str]
    settlement_rules: list[FundSettlementRule]

    def account_for_asset_class(self, asset_class: str) -> str:
        return self.accounts.get(asset_class, self.default_account)

    def income_account_for_asset_class(self, asset_class: str) -> str:
        return self.income_accounts.get(asset_class, self.default_income_account)

    def settlement_days_for_text(self, text: str) -> int:
        for rule in self.settlement_rules:
            if rule.matches(text):
                return rule.settlement_days
        return self.default_settlement_days


@dataclass
class Config:
    default_expense_account: str
    default_income_account: str
    suspense_account: str
    cashback_income_account: str
    icbc_shuakajin_income_account: str
    aa_account: str
    reimburse_account: str
    default_share_account: str
    discount_income_account: str
    alipay_balance_account: str
    alipay_yuebao_account: str
    alipay_huabei_account: str
    wechat_balance_account: str
    wechat_lqt_account: str
    account_rules: list[RegexRule]
    expense_rules: list[RegexRule]
    income_rules: list[RegexRule]
    merchant_alias_rules: list[MerchantAliasRule]
    credit_card_cashback_rules: list[CreditCardCashbackRule]
    manual_review_patterns: list[str]
    funds: FundConfig

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        funds = data.get("funds", {})
        return cls(
            default_expense_account=defaults.get("expense_account", "Expenses:Other"),
            default_income_account=defaults.get("income_account", "Income:Other"),
            suspense_account=defaults.get("suspense_account", "Assets:Unknown"),
            cashback_income_account=defaults.get("cashback_income_account", "Income:Rebate:Bank"),
            icbc_shuakajin_income_account=defaults.get(
                "icbc_shuakajin_income_account",
                defaults.get("cashback_income_account", "Income:Rebate:Bank"),
            ),
            aa_account=defaults.get("aa_account", "Assets:Receivables:Other"),
            reimburse_account=defaults.get("reimburse_account", "Assets:Receivables:Employer"),
            default_share_account=defaults.get(
                "default_share_account",
                "Assets:Receivables:Partner",
            ),
            discount_income_account=defaults.get("discount_income_account", "Income:Other"),
            alipay_balance_account=defaults.get(
                "alipay_balance_account",
                "Assets:Current:Alipay:Balance",
            ),
            alipay_yuebao_account=defaults.get(
                "alipay_yuebao_account",
                "Assets:Current:Alipay:YuEBao",
            ),
            alipay_huabei_account=defaults.get(
                "alipay_huabei_account",
                "Liabilities:Credit:Alipay:Huabei",
            ),
            wechat_balance_account=defaults.get(
                "wechat_balance_account",
                "Assets:Current:WeChat:Balance",
            ),
            wechat_lqt_account=defaults.get(
                "wechat_lqt_account",
                "Assets:Current:WeChat:LQT",
            ),
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
                    r.get("income_account", ""),
                )
                for r in data.get("credit_card_cashback_rules", [])
            ],
            manual_review_patterns=data.get("manual_review", {}).get("patterns", []),
            funds=FundConfig(
                default_account=funds.get("default_account", "Assets:Invest:Alipay:Fund"),
                default_income_account=funds.get(
                    "default_income_account",
                    "Income:Invest:Alipay:Fund",
                ),
                commission_account=funds.get(
                    "commission_account",
                    "Expenses:Invest:Commissions",
                ),
                discount_income_account=defaults.get(
                    "discount_income_account",
                    "Income:Other",
                ),
                default_settlement_days=int(funds.get("default_settlement_days", 1)),
                share_precision=int(funds.get("share_precision", 2)),
                accounts={
                    str(key): str(value)
                    for key, value in funds.get("accounts", {}).items()
                },
                income_accounts={
                    str(key): str(value)
                    for key, value in funds.get("income_accounts", {}).items()
                },
                settlement_rules=[
                    FundSettlementRule(str(r["pattern"]), int(r["settlement_days"]))
                    for r in funds.get("settlement_rules", [])
                ],
            ),
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

    def manual_review_text_for(self, tx: BillTransaction) -> str:
        metadata = {
            key: value
            for key, value in tx.metadata.items()
            if not _is_opaque_metadata_key(str(key))
        }
        parts = [
            tx.source,
            tx.payee,
            tx.narration,
            tx.source_account_hint,
            str(metadata),
        ]
        return " ".join(p for p in parts if p)

    def source_account_for(self, tx: BillTransaction) -> str:
        if is_account_name(tx.source_account_hint):
            return tx.source_account_hint
        if tx.source_account_hint and tx.source_account_hint != "/":
            return self.account_for_text(tx.source_account_hint) or tx.source_account_hint
        text = self.text_for(tx)
        account = self.account_for_text(text)
        if account:
            return account
        return self.suspense_account

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

    def alipay_account_for_method(self, method: str) -> str:
        method = (method or "").strip()
        if not method or method == "/":
            return ""
        if method == "账户余额":
            return self.alipay_balance_account
        if method == "余额宝":
            return self.alipay_yuebao_account
        if "花呗" in method:
            return self.alipay_huabei_account
        return self.account_for_text(method) or method

    def wechat_account_for_method(self, method: str, status: str = "") -> str:
        method = (method or "").strip()
        status = (status or "").strip()
        if method == "零钱":
            return self.wechat_balance_account
        if method == "零钱通":
            return self.wechat_lqt_account
        if method == "/" and "零钱" in status:
            return self.wechat_balance_account
        if not method or method == "/":
            return ""
        return self.account_for_text(method) or method

    def needs_manual_review(self, tx: BillTransaction) -> str:
        text = self.manual_review_text_for(tx)
        for pattern in self.manual_review_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        return ""

    def same_merchant_text(self, left: str, right: str) -> bool:
        return any(rule.matches_pair(left, right) for rule in self.merchant_alias_rules)

    def credit_card_cashback_income_account(self, tx: BillTransaction) -> str:
        for rule in self.credit_card_cashback_rules:
            if rule.matches(tx):
                return rule.income_account or self.cashback_income_account
        return ""


def _list_value(data: dict, list_key: str, scalar_key: str) -> list[str]:
    values = data.get(list_key)
    if values is None and scalar_key in data:
        values = [data[scalar_key]]
    return [str(value) for value in values or []]


def _is_opaque_metadata_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in (
            "id",
            "uid",
            "订单号",
            "单号",
            "流水号",
        )
    )
