from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import cached_property
from pathlib import Path
import re
import subprocess
import sys

from tqdm import tqdm

from .beancount_format import format_decimal, format_tags_links, quote
from .config import FundConfig
from .discounts import DiscountAmount
from .ledger import ReviewRow


PRICE_DIRECTIVE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+price\s+(\S+)\s+([+-]?[0-9.]+)\s+([A-Z][A-Z0-9_]*)"
)


@dataclass(frozen=True)
class InvestmentCommodity:
    symbol: str
    name: str
    asset_class: str
    price_source: str = ""
    settlement_days: int | None = None

    def settlement_text(self) -> str:
        return " ".join([self.name, self.symbol, self.asset_class, self.price_source])


@dataclass(frozen=True)
class FundTrade:
    row: ReviewRow
    commodity: InvestmentCommodity
    side: str

    @classmethod
    def from_row(
        cls,
        row: ReviewRow,
        commodities: list[InvestmentCommodity],
    ) -> "FundTrade | None":
        text = " ".join([row.payee, row.narration])
        for commodity in sorted(commodities, key=lambda item: len(item.name), reverse=True):
            if commodity.name not in text:
                continue
            if f"{commodity.name}-买入" in row.narration:
                return cls(row, commodity, "buy")
            if f"{commodity.name}-卖出" in row.narration:
                return cls(row, commodity, "sell")
        return None

    @property
    def narration(self) -> str:
        marker = "买入" if self.side == "buy" else "卖出"
        return f"{self.commodity.name}-{marker}"

    def price_date(self, fund_config: FundConfig) -> date:
        if self.row.investment_price_date:
            return date.fromisoformat(self.row.investment_price_date)
        row_time = datetime.fromisoformat(self.row.time)
        if self.side == "buy":
            trade_date = row_time.date()
            if row_time.time() >= time(15, 0):
                return next_business_day(trade_date)
            return business_day_on_or_after(trade_date)
        settlement_anchor = previous_business_day_on_or_before(row_time.date())
        if row_time.time() < time(15, 0):
            settlement_anchor = previous_business_day(settlement_anchor)
        settlement_days = self.commodity.settlement_days
        if settlement_days is None:
            settlement_days = fund_config.settlement_days_for_text(
                self.commodity.settlement_text()
            )
        return subtract_business_days(settlement_anchor, settlement_days)


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class InvestmentExportResult:
    transactions: str
    prices: str


@dataclass(frozen=True)
class InvestmentCommoditySource:
    path: str | Path

    @cached_property
    def items(self) -> list[InvestmentCommodity]:
        return self.parse_text(Path(self.path).read_text(encoding="utf-8"))

    @staticmethod
    def parse_text(text: str) -> list[InvestmentCommodity]:
        commodities: list[InvestmentCommodity] = []
        current_symbol = ""
        metadata: dict[str, str] = {}
        commodity_re = re.compile(r"^\d{4}-\d{2}-\d{2}\s+commodity\s+(\S+)")
        meta_re = re.compile(r'^\s+([A-Za-z0-9_-]+):\s+"(.*)"\s*$')

        def flush() -> None:
            nonlocal current_symbol, metadata
            if current_symbol and metadata.get("name"):
                commodities.append(
                    InvestmentCommodity(
                        symbol=current_symbol,
                        name=metadata.get("name", ""),
                        asset_class=metadata.get("asset-class", "fund"),
                        price_source=metadata.get("price", ""),
                        settlement_days=_metadata_int(metadata, "settlement-days")
                        if "settlement-days" in metadata
                        else _metadata_int(metadata, "settlement_days"),
                    )
                )
            current_symbol = ""
            metadata = {}

        for line in text.splitlines():
            commodity_match = commodity_re.match(line)
            if commodity_match:
                flush()
                current_symbol = commodity_match.group(1)
                continue
            meta_match = meta_re.match(line)
            if meta_match and current_symbol:
                metadata[meta_match.group(1)] = meta_match.group(2)
        flush()
        return commodities


@dataclass(frozen=True)
class InvestmentPriceSource:
    path: str | Path = ""
    inline_text: str | None = None

    @classmethod
    def from_path(cls, path: str | Path) -> "InvestmentPriceSource":
        return cls(path=path)

    @classmethod
    def from_text(cls, text: str) -> "InvestmentPriceSource":
        return cls(inline_text=text)

    @cached_property
    def text(self) -> str:
        if self.inline_text is not None:
            return self.inline_text
        target = Path(self.path)
        if not target.exists():
            return ""
        return target.read_text(encoding="utf-8")

    @cached_property
    def existing_directives(self) -> str:
        return self.extract_directives(self.text)

    @cached_property
    def existing_prices(self) -> dict[tuple[str, str], Price]:
        return self.parse_text(self.text)

    def merged_with(self, fetched_prices: str) -> str:
        return self.merge_directives(self.existing_directives, fetched_prices)

    @staticmethod
    def parse_text(text: str) -> dict[tuple[str, str], Price]:
        prices: dict[tuple[str, str], Price] = {}
        for line in text.splitlines():
            match = PRICE_DIRECTIVE_RE.match(line)
            if not match:
                continue
            date_text, symbol, amount, currency = match.groups()
            prices[(date_text, symbol)] = Price(Decimal(amount), currency)
        return prices

    @staticmethod
    def extract_directives(text: str) -> str:
        lines = [line for line in text.splitlines() if PRICE_DIRECTIVE_RE.match(line)]
        return "\n".join(lines).rstrip() + "\n" if lines else ""

    @staticmethod
    def merge_directives(*texts: str) -> str:
        keyed_lines: dict[tuple[str, str, str], str] = {}
        for text in texts:
            for line in text.splitlines():
                match = PRICE_DIRECTIVE_RE.match(line)
                if not match:
                    continue
                date_text, symbol, _amount, currency = match.groups()
                keyed_lines[(date_text, symbol, currency)] = line
        lines = [keyed_lines[key] for key in sorted(keyed_lines)]
        return "\n".join(lines).rstrip() + "\n" if lines else ""


@dataclass(frozen=True)
class InvestmentPosting:
    account: str
    amount: str = ""
    currency: str = ""
    cost: str = ""
    flagged: bool = False

    @classmethod
    def amount_posting(
        cls,
        account: str,
        amount: Decimal,
        currency: str,
        flagged: bool = False,
    ) -> "InvestmentPosting":
        return cls(account, format_decimal(amount), currency, flagged=flagged)

    @classmethod
    def units_with_cost(
        cls,
        account: str,
        units: str,
        commodity: str,
        price: Price,
    ) -> "InvestmentPosting":
        return cls(
            account,
            units,
            commodity,
            cost=f"{{{format_decimal(price.amount)} {price.currency}}}",
        )

    @classmethod
    def units_empty_cost(
        cls,
        account: str,
        units: str,
        commodity: str,
    ) -> "InvestmentPosting":
        return cls(account, units, commodity, cost="{}")

    @classmethod
    def balancing(cls, account: str) -> "InvestmentPosting":
        return cls(account)

    def format(self) -> str:
        flag = "! " if self.flagged else ""
        line = f"  {flag}{self.account}"
        if self.amount and self.currency:
            line += f"  {self.amount} {self.currency}"
        if self.cost:
            line += f" {self.cost}"
        return line


@dataclass(frozen=True)
class InvestmentTransactionDraft:
    date: str
    payee: str
    narration: str
    metadata: list[tuple[str, str]]
    postings: list[InvestmentPosting]
    tags_links: str = ""

    def format(self) -> str:
        suffix = f" {self.tags_links}" if self.tags_links else ""
        lines = [f"{self.date} * {quote(self.payee)} {quote(self.narration)}{suffix}"]
        for key, value in self.metadata:
            lines.append(f"  {key}: {quote(value)}")
        lines.extend(posting.format() for posting in self.postings)
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class InvestmentExporter:
    rows: tuple[ReviewRow, ...]
    commodity_source: InvestmentCommoditySource
    price_source: InvestmentPriceSource
    fund_config: FundConfig
    fetch_prices: bool = False
    bean_price_command: str = "bean-price"

    @cached_property
    def trades(self) -> list[FundTrade]:
        return [
            trade
            for row in self.rows
            if row.action == "invest"
            for trade in [FundTrade.from_row(row, self.commodity_source.items)]
            if trade is not None
        ]

    @cached_property
    def missing_dates(self) -> list[date]:
        required_symbols_by_date: dict[date, set[str]] = {}
        for trade in self.trades:
            if trade.row.investment_price:
                continue
            price_date = self.price_date_for(trade)
            required_symbols_by_date.setdefault(price_date, set()).add(
                trade.commodity.symbol
            )
        return sorted(
            price_date
            for price_date, symbols in required_symbols_by_date.items()
            if any(
                (price_date.isoformat(), symbol) not in self.price_source.existing_prices
                for symbol in symbols
            )
        )

    @cached_property
    def price_dates_by_uid(self) -> dict[str, date]:
        return {
            trade.row.uid: trade.price_date(self.fund_config)
            for trade in self.trades
            if trade.row.uid
        }

    def price_date_for(self, trade: FundTrade) -> date:
        if trade.row.uid:
            return self.price_dates_by_uid[trade.row.uid]
        return trade.price_date(self.fund_config)

    @cached_property
    def fetched_prices(self) -> str:
        if not self.fetch_prices or not self.missing_dates:
            return ""
        return fetch_price_directives(
            self.commodity_source.path,
            self.missing_dates,
            bean_price_command=self.bean_price_command,
        )

    @cached_property
    def price_table(self) -> dict[tuple[str, str], Price]:
        return InvestmentPriceSource.parse_text(
            "\n".join(
                part for part in [self.price_source.text, self.fetched_prices] if part
            )
        )

    @cached_property
    def transaction_drafts(self) -> list[InvestmentTransactionDraft]:
        return [self.build_transaction_draft(trade) for trade in self.trades]

    def build_transaction_draft(self, trade: FundTrade) -> InvestmentTransactionDraft:
        row = trade.row
        price_date = self.price_date_for(trade).isoformat()
        metadata = [
            ("source", row.source),
            ("import_id", row.uid),
            ("price_date", price_date),
        ]
        postings = FundTradePostings(
            trade,
            self.fund_config,
            self.price_table,
            price_date,
        ).build()
        return InvestmentTransactionDraft(
            date=row.posting_date,
            payee=row.payee,
            narration=trade.narration,
            metadata=metadata,
            postings=postings,
            tags_links=format_tags_links(row.get("tags", ""), row.get("links", "")),
        )

    def render(self) -> InvestmentExportResult:
        rendered = [draft.format() for draft in self.transaction_drafts]
        return InvestmentExportResult(
            transactions="\n".join(chunk for chunk in rendered if chunk).rstrip() + "\n"
            if rendered
            else "",
            prices=self.fetched_prices.rstrip() + "\n" if self.fetched_prices.strip() else "",
        )

    def required_accounts(self) -> set[str]:
        accounts: set[str] = set()
        for trade in self.trades:
            row = trade.row
            account = self.fund_config.account_for_asset_class(trade.commodity.asset_class)
            accounts.add(account)
            accounts.add(row.source_account)
            if trade.side == "sell":
                accounts.add(
                    self.fund_config.income_account_for_asset_class(
                        trade.commodity.asset_class
                    )
                )
            if row.commission_amount:
                accounts.add(row.commission_account or self.fund_config.commission_account)
            if DiscountAmount.parse(row.discount_amount).amount:
                accounts.add(
                    row.discount_account or self.fund_config.discount_income_account
                )
        return {account for account in accounts if account}


def render_fund_export(
    rows: list[ReviewRow],
    commodities_path: str | Path,
    price_text: str,
    fund_config: FundConfig,
    fetch_prices: bool = False,
    bean_price_command: str = "bean-price",
) -> InvestmentExportResult:
    return InvestmentExporter(
        tuple(rows),
        InvestmentCommoditySource(commodities_path),
        InvestmentPriceSource.from_text(price_text),
        fund_config,
        fetch_prices=fetch_prices,
        bean_price_command=bean_price_command,
    ).render()


def required_accounts_for_fund_export(
    rows: list[ReviewRow],
    commodities_path: str | Path,
    fund_config: FundConfig,
) -> set[str]:
    return InvestmentExporter(
        tuple(rows),
        InvestmentCommoditySource(commodities_path),
        InvestmentPriceSource.from_text(""),
        fund_config,
    ).required_accounts()


def parse_commodities(path: str | Path) -> list[InvestmentCommodity]:
    return InvestmentCommoditySource(path).items


def parse_prices(text: str) -> dict[tuple[str, str], Price]:
    return InvestmentPriceSource.parse_text(text)


def extract_price_directives(text: str) -> str:
    return InvestmentPriceSource.extract_directives(text)


def merge_price_directives(*texts: str) -> str:
    return InvestmentPriceSource.merge_directives(*texts)


def fetch_price_directives(
    commodities_path: str | Path,
    price_dates: list[date],
    bean_price_command: str = "bean-price",
) -> str:
    chunks: list[str] = []
    with tqdm(
        price_dates,
        desc="fetching fund prices",
        unit="date",
        file=sys.stderr,
        dynamic_ncols=True,
    ) as progress:
        for price_date in progress:
            progress.set_postfix_str(price_date.isoformat())
            result = subprocess.run(
                [
                    bean_price_command,
                    "-i",
                    "-c",
                    "-d",
                    price_date.isoformat(),
                    str(commodities_path),
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                if result.stdout.strip():
                    chunks.append(result.stdout.strip())
                    tqdm.write(
                        f"warning: bean-price returned {result.returncode} for {price_date}; "
                        f"using partial output: {_summarize_stderr(result.stderr)}",
                        file=sys.stderr,
                    )
                    continue
                tqdm.write(
                    f"warning: bean-price failed for {price_date}: "
                    f"{_summarize_stderr(result.stderr)}",
                    file=sys.stderr,
                )
                continue
            chunks.append(result.stdout.strip())
    return "\n".join(chunk for chunk in chunks if chunk)


def _summarize_stderr(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "no error output"
    preferred_patterns = (
        "Invalid reference",
        "requests.exceptions.",
        "PermissionError",
        "ProxyError",
        "SSLError",
    )
    for pattern in preferred_patterns:
        for line in lines:
            if pattern in line:
                return _truncate(line)
    return _truncate(lines[-1])


def _truncate(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def price_date_for_trade(trade: FundTrade, fund_config: FundConfig) -> date:
    return trade.price_date(fund_config)


@dataclass(frozen=True)
class FundTradePostings:
    trade: FundTrade
    fund_config: FundConfig
    prices: dict[tuple[str, str], Price]
    price_date: str

    def build(self) -> list[InvestmentPosting]:
        postings: list[InvestmentPosting] = []
        if self.trade.side == "buy":
            postings.extend(self.buy_postings())
        else:
            postings.extend(self.sell_postings())
        return postings

    def buy_postings(self) -> list[InvestmentPosting]:
        postings: list[InvestmentPosting] = []
        if self.price:
            units = _units(
                self.row,
                self.trade_amount,
                self.price.amount,
                self.fund_config.share_precision,
            )
            postings.append(
                InvestmentPosting.units_with_cost(
                    self.account,
                    units,
                    self.trade.commodity.symbol,
                    self.price,
                )
            )
        else:
            postings.append(
                InvestmentPosting.amount_posting(
                    self.account,
                    self.trade_amount,
                    self.currency,
                    flagged=True,
                )
            )
        postings.extend(self.fee_postings)
        postings.append(
            InvestmentPosting.amount_posting(
                self.row.source_account,
                -self.amount,
                self.currency,
            )
        )
        return postings

    def sell_postings(self) -> list[InvestmentPosting]:
        postings: list[InvestmentPosting] = []
        if self.price:
            units = _units(
                self.row,
                self.trade_amount,
                self.price.amount,
                self.fund_config.share_precision,
            )
            postings.append(
                InvestmentPosting.units_empty_cost(
                    self.account,
                    f"-{units}",
                    self.trade.commodity.symbol,
                )
            )
        else:
            postings.append(
                InvestmentPosting.amount_posting(
                    self.account,
                    -self.trade_amount,
                    self.currency,
                    flagged=True,
                )
            )
        postings.append(
            InvestmentPosting.amount_posting(
                self.row.source_account,
                self.amount,
                self.currency,
            )
        )
        postings.extend(self.fee_postings)
        postings.append(
            InvestmentPosting.balancing(
                self.fund_config.income_account_for_asset_class(
                    self.trade.commodity.asset_class
                )
            )
        )
        return postings

    @cached_property
    def fee_postings(self) -> tuple[InvestmentPosting, ...]:
        postings: list[InvestmentPosting] = []
        if self.commission_amount:
            postings.append(
                InvestmentPosting.amount_posting(
                    self.commission_account,
                    self.commission_amount,
                    self.currency,
                )
            )
        discount_amount = self.discount.amount
        if discount_amount:
            if self.discount.is_cashback:
                postings.append(
                    InvestmentPosting.amount_posting(
                        self.row.source_account,
                        discount_amount,
                        self.currency,
                    )
                )
            postings.append(
                InvestmentPosting.amount_posting(
                    self.discount_account,
                    -discount_amount,
                    self.currency,
                )
            )
        return tuple(postings)

    @property
    def row(self) -> ReviewRow:
        return self.trade.row

    @cached_property
    def amount(self) -> Decimal:
        return self.row.decimal_field("amount")

    @cached_property
    def price(self) -> Price | None:
        if self.row.investment_price:
            return Price(self.row.decimal_field("investment_price"), "CNY")
        return self.prices.get((self.price_date, self.trade.commodity.symbol))

    @cached_property
    def account(self) -> str:
        return self.fund_config.account_for_asset_class(self.trade.commodity.asset_class)

    @property
    def currency(self) -> str:
        return self.row.currency or "CNY"

    @cached_property
    def discount(self) -> DiscountAmount:
        return DiscountAmount.parse(self.row.discount_amount)

    @cached_property
    def discount_account(self) -> str:
        return self.row.discount_account or self.fund_config.discount_income_account

    @cached_property
    def commission_amount(self) -> Decimal:
        return self.row.nonnegative_decimal_field("commission_amount", "0")

    @cached_property
    def commission_account(self) -> str:
        return self.row.commission_account or self.fund_config.commission_account

    @cached_property
    def net_fee(self) -> Decimal:
        return self.commission_amount - self.discount.fee_deduction

    @cached_property
    def trade_amount(self) -> Decimal:
        if self.trade.side == "buy":
            return self.amount - self.net_fee
        return self.amount + self.net_fee


def _units(
    row: ReviewRow,
    amount: Decimal,
    price: Decimal,
    precision: int,
) -> str:
    if row.investment_units:
        return row.investment_units
    quantum = Decimal(1).scaleb(-precision)
    return str((amount / price).quantize(quantum, rounding=ROUND_HALF_UP))


def _metadata_int(metadata: dict[str, str], key: str) -> int | None:
    value = metadata.get(key)
    if value is None or not value.strip():
        return None
    return int(value.strip())


def business_day_on_or_after(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def next_business_day(value: date) -> date:
    return business_day_on_or_after(value + timedelta(days=1))


def previous_business_day_on_or_before(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def previous_business_day(value: date) -> date:
    current = value - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def subtract_business_days(value: date, days: int) -> date:
    current = value
    for _ in range(days):
        current = previous_business_day(current)
    return current
