from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
import subprocess
import sys

from tqdm import tqdm

from .config import FundConfig
from .discounts import DiscountAmount
from .exporter import _quote
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


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class FundExportResult:
    transactions: str
    prices: str


def render_fund_export(
    rows: list[ReviewRow],
    commodities_path: str | Path,
    price_text: str,
    fund_config: FundConfig,
    fetch_prices: bool = False,
    bean_price_command: str = "bean-price",
) -> FundExportResult:
    commodities = parse_commodities(commodities_path)
    trades = [
        trade
        for row in rows
        if row.action == "invest"
        for trade in [_fund_trade_for_row(row, commodities)]
        if trade is not None
    ]
    existing_prices = parse_prices(price_text)
    missing_dates = missing_price_dates(trades, fund_config, existing_prices)
    fetched_prices = ""
    if fetch_prices and missing_dates:
        fetched_prices = fetch_price_directives(
            commodities_path,
            missing_dates,
            bean_price_command=bean_price_command,
        )
    prices = parse_prices("\n".join(part for part in [price_text, fetched_prices] if part))
    rendered = [_format_fund_trade(trade, fund_config, prices) for trade in trades]
    return FundExportResult(
        transactions="\n".join(chunk for chunk in rendered if chunk).rstrip() + "\n"
        if rendered
        else "",
        prices=fetched_prices.rstrip() + "\n" if fetched_prices.strip() else "",
    )


def required_accounts_for_fund_export(
    rows: list[ReviewRow],
    commodities_path: str | Path,
    fund_config: FundConfig,
) -> set[str]:
    commodities = parse_commodities(commodities_path)
    accounts: set[str] = set()
    for row in rows:
        if row.action != "invest":
            continue
        trade = _fund_trade_for_row(row, commodities)
        if trade is None:
            continue
        account = fund_config.account_for_asset_class(trade.commodity.asset_class)
        accounts.add(account)
        accounts.add(row.source_account)
        if trade.side == "sell":
            accounts.add(fund_config.income_account_for_asset_class(trade.commodity.asset_class))
        if row.commission_amount:
            accounts.add(row.commission_account or fund_config.commission_account)
        if DiscountAmount.parse(row.discount_amount).amount:
            accounts.add(row.discount_account or fund_config.discount_income_account)
    return {account for account in accounts if account}


def parse_commodities(path: str | Path) -> list[InvestmentCommodity]:
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

    for line in Path(path).read_text(encoding="utf-8").splitlines():
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


def parse_prices(text: str) -> dict[tuple[str, str], Price]:
    prices: dict[tuple[str, str], Price] = {}
    for line in text.splitlines():
        match = PRICE_DIRECTIVE_RE.match(line)
        if not match:
            continue
        date_text, symbol, amount, currency = match.groups()
        prices[(date_text, symbol)] = Price(Decimal(amount), currency)
    return prices


def extract_price_directives(text: str) -> str:
    lines = [line for line in text.splitlines() if PRICE_DIRECTIVE_RE.match(line)]
    return "\n".join(lines).rstrip() + "\n" if lines else ""


def merge_price_directives(*texts: str) -> str:
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


def missing_price_dates(
    trades: list[FundTrade],
    fund_config: FundConfig,
    existing_prices: dict[tuple[str, str], Price],
) -> list[date]:
    required_symbols_by_date: dict[date, set[str]] = {}
    for trade in trades:
        if trade.row.investment_price:
            continue
        price_date = _price_date_for_trade(trade, fund_config)
        required_symbols_by_date.setdefault(price_date, set()).add(trade.commodity.symbol)
    return sorted(
        price_date
        for price_date, symbols in required_symbols_by_date.items()
        if any((price_date.isoformat(), symbol) not in existing_prices for symbol in symbols)
    )


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
    row_time = datetime.fromisoformat(trade.row.time)
    if trade.side == "buy":
        trade_date = row_time.date()
        if row_time.time() >= time(15, 0):
            return next_business_day(trade_date)
        return business_day_on_or_after(trade_date)
    settlement_anchor = previous_business_day_on_or_before(row_time.date())
    if row_time.time() < time(15, 0):
        settlement_anchor = previous_business_day(settlement_anchor)
    settlement_days = trade.commodity.settlement_days
    if settlement_days is None:
        settlement_days = fund_config.settlement_days_for_text(trade.commodity.settlement_text())
    return subtract_business_days(settlement_anchor, settlement_days)


def _price_date_for_trade(trade: FundTrade, fund_config: FundConfig) -> date:
    if trade.row.investment_price_date:
        return date.fromisoformat(trade.row.investment_price_date)
    return price_date_for_trade(trade, fund_config)


def _fund_trade_for_row(
    row: ReviewRow,
    commodities: list[InvestmentCommodity],
) -> FundTrade | None:
    text = " ".join([row.payee, row.narration])
    for commodity in sorted(commodities, key=lambda item: len(item.name), reverse=True):
        if commodity.name not in text:
            continue
        if f"{commodity.name}-买入" in row.narration:
            return FundTrade(row, commodity, "buy")
        if f"{commodity.name}-卖出" in row.narration:
            return FundTrade(row, commodity, "sell")
    return None


def _format_fund_trade(
    trade: FundTrade,
    fund_config: FundConfig,
    prices: dict[tuple[str, str], Price],
) -> str:
    row = trade.row
    amount = _decimal(row.amount)
    price_date = _price_date_for_trade(trade, fund_config).isoformat()
    price = _price_for_trade(row, trade.commodity, price_date, prices)
    account = fund_config.account_for_asset_class(trade.commodity.asset_class)
    currency = row.currency or "CNY"
    discount = DiscountAmount.parse(row.discount_amount)
    discount_account = row.discount_account or fund_config.discount_income_account
    commission_amount = _decimal(row.commission_amount or "0")
    commission_account = row.commission_account or fund_config.commission_account
    net_fee = commission_amount - discount.fee_deduction
    trade_amount = amount - net_fee if trade.side == "buy" else amount + net_fee
    lines = [f"{row.posting_date} * {_quote(row.payee)} {_quote(_fund_narration(row, trade))}"]
    lines.append(f"  source: {_quote(row.source)}")
    lines.append(f"  import_id: {_quote(row.uid)}")
    lines.append(f"  price_date: {_quote(price_date)}")
    if trade.side == "buy":
        if price:
            units = _units(row, trade_amount, price.amount, fund_config.share_precision)
            lines.append(
                f"  {account}  {units} {trade.commodity.symbol} {{{_format_decimal(price.amount)} {price.currency}}}"
            )
        else:
            lines.append(f"  ! {account}  {_format_decimal(trade_amount)} {currency}")
        lines.extend(
            _fee_postings(
                discount,
                discount_account,
                commission_amount,
                commission_account,
                row.source_account,
                currency,
            )
        )
        lines.append(f"  {row.source_account}  -{_format_decimal(amount)} {currency}")
    else:
        if price:
            units = _units(row, trade_amount, price.amount, fund_config.share_precision)
            lines.append(f"  {account}  -{units} {trade.commodity.symbol} {{}}")
        else:
            lines.append(f"  ! {account}  -{_format_decimal(trade_amount)} {currency}")
        lines.append(f"  {row.source_account}  {_format_decimal(amount)} {currency}")
        lines.extend(
            _fee_postings(
                discount,
                discount_account,
                commission_amount,
                commission_account,
                row.source_account,
                currency,
            )
        )
        lines.append(f"  {fund_config.income_account_for_asset_class(trade.commodity.asset_class)}")
    return "\n".join(lines) + "\n"


def _fee_postings(
    discount: DiscountAmount,
    discount_account: str,
    commission_amount: Decimal,
    commission_account: str,
    source_account: str,
    currency: str,
) -> list[str]:
    lines: list[str] = []
    if commission_amount:
        lines.append(f"  {commission_account}  {_format_decimal(commission_amount)} {currency}")
    discount_amount = discount.amount
    if discount_amount:
        if discount.is_cashback:
            lines.append(f"  {source_account}  {_format_decimal(discount_amount)} {currency}")
        lines.append(f"  {discount_account}  -{_format_decimal(discount_amount)} {currency}")
    return lines


def _price_for_trade(
    row: ReviewRow,
    commodity: InvestmentCommodity,
    price_date: str,
    prices: dict[tuple[str, str], Price],
) -> Price | None:
    if row.investment_price:
        return Price(_decimal(row.investment_price), "CNY")
    return prices.get((price_date, commodity.symbol))


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


def _fund_narration(row: ReviewRow, trade: FundTrade) -> str:
    marker = "买入" if trade.side == "buy" else "卖出"
    return f"{trade.commodity.name}-{marker}"


def _format_decimal(value: Decimal) -> str:
    return format(value, "f")


def _decimal(value: str) -> Decimal:
    return Decimal((value or "0").replace(",", ""))


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
