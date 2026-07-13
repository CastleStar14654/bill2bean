from __future__ import annotations

import argparse
import getpass
import sys

from .config import Config
from .exporter import BeanExporter, ExportDefaults, ReviewRowFilter
from .investments import (
    InvestmentCommoditySource,
    InvestmentExporter,
    InvestmentPriceSource,
)
from .ledger import TransactionList, extract_accounts, read_review_csv
from .parsers import parser_for


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "review":
        return run_review(args)
    if args.command == "export":
        return run_export(args)
    parser.error(f"unsupported command {args.command!r}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bill2bean")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="parse bills and write editable review CSV")
    review.add_argument("files", nargs="+")
    review.add_argument("-c", "--config", default="config.toml")
    review.add_argument("-o", "--output", default="review.csv")
    review.add_argument(
        "--previous-review",
        help="reuse edited rows from an existing review CSV when transaction uid matches",
    )
    review.add_argument(
        "--zip-password",
        help="password for encrypted Alipay or WeChat zip bills; if omitted, prompt per encrypted zip",
    )

    export = sub.add_parser("export", help="export reviewed CSV to Beancount")
    export.add_argument("review_csv")
    export.add_argument("-o", "--output", default="imported.bean")
    export.add_argument("--accounts", help="optional Beancount account definition file")
    export.add_argument(
        "--with-header",
        action="store_true",
        help="prepend include/options header for opening the exported file directly",
    )
    export.add_argument(
        "--operating-currency",
        default="CNY",
        help='currency written as option "operating_currency" when --with-header is used',
    )
    export.add_argument(
        "--include-source",
        action="append",
        default=[],
        help="only export these sources; can be repeated or comma-separated",
    )
    export.add_argument(
        "--exclude-source",
        action="append",
        default=[],
        help="do not export these sources; can be repeated or comma-separated",
    )
    export.add_argument(
        "--start-date",
        default="",
        help="only export rows on or after YYYY-MM-DD",
    )
    export.add_argument(
        "--end-date",
        default="",
        help="only export rows on or before YYYY-MM-DD",
    )
    export.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="config used for investment export",
    )
    export.add_argument("--fund-commodities", help="commodity bean file for Alipay fund trades")
    export.add_argument(
        "--fund-output",
        nargs="?",
        const="",
        help="bean file for investment transactions; omit value to use -o",
    )
    export.add_argument(
        "--price-output",
        nargs="?",
        const="",
        help="bean file for fetched or existing price directives; omit value to use -o",
    )
    export.add_argument(
        "--fetch-fund-prices",
        action="store_true",
        help="call bean-price for required investment price dates",
    )
    export.add_argument(
        "--bean-price-command",
        default="bean-price",
        help="bean-price executable used with --fetch-fund-prices",
    )
    return parser


def run_review(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    txs = []
    try:
        for filename in args.files:
            txs.extend(
                parser_for(
                    filename,
                    config,
                    zip_password_provider=_zip_password_provider(args.zip_password),
                ).parse(filename)
            )
        previous_rows = (
            read_review_csv(args.previous_review) if args.previous_review else None
        )
        TransactionList(txs, config).normalize().write_review_csv(
            args.output,
            previous_rows,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"wrote {args.output} with {len(txs)} rows")
    return 0


def run_export(args: argparse.Namespace) -> int:
    if args.with_header and not args.accounts:
        print("--with-header requires --accounts", file=sys.stderr)
        return 2

    include_sources = parse_values(args.include_source)
    exclude_sources = parse_values(args.exclude_source)
    rows = read_review_csv(args.review_csv)
    try:
        fund_enabled = normalize_fund_outputs(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    config = Config.load(args.config)
    export_defaults = ExportDefaults.from_config(config)
    row_filter = ReviewRowFilter(
        rows,
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    try:
        filtered_rows = row_filter.filtered_rows
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    exporter = BeanExporter(export_defaults, rows=filtered_rows)
    investment_price_source = (
        InvestmentPriceSource.from_path(args.price_output) if fund_enabled else None
    )
    investment_exporter = create_investment_exporter(
        args,
        filtered_rows,
        config,
        investment_price_source,
    )
    try:
        validate_accounts(args.accounts, exporter, investment_exporter)
        outputs = build_export_outputs(
            args,
            fund_enabled,
            exporter,
            investment_exporter,
            investment_price_source,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for output, chunks in outputs.items():
        write_chunks(output, chunks)
    for output in outputs:
        print(f"wrote {output}")
    return 0


def normalize_fund_outputs(args: argparse.Namespace) -> bool:
    fund_enabled = any(
        [
            args.fund_commodities,
            args.fund_output is not None,
            args.price_output is not None,
        ]
    )
    if fund_enabled and not all(
        [
            args.fund_commodities,
            args.fund_output is not None,
            args.price_output is not None,
        ]
    ):
        raise ValueError(
            "--fund-commodities, --fund-output, and --price-output must be used together"
        )
    if args.fund_output == "":
        args.fund_output = args.output
    if args.price_output == "":
        args.price_output = args.output
    return fund_enabled


def create_investment_exporter(
    args: argparse.Namespace,
    filtered_rows,
    config: Config,
    investment_price_source: InvestmentPriceSource | None,
) -> InvestmentExporter | None:
    if not investment_price_source:
        return None
    return InvestmentExporter(
        filtered_rows,
        InvestmentCommoditySource(args.fund_commodities),
        investment_price_source,
        config.funds,
        fetch_prices=args.fetch_fund_prices,
        bean_price_command=args.bean_price_command,
    )


def validate_accounts(
    accounts_path: str | None,
    exporter: BeanExporter,
    investment_exporter: InvestmentExporter | None,
) -> None:
    if not accounts_path:
        return
    required_accounts = exporter.required_accounts()
    if investment_exporter:
        required_accounts |= investment_exporter.required_accounts()
    missing = sorted(required_accounts - extract_accounts(accounts_path))
    if not missing:
        return
    lines = ["unknown accounts:", *(f"  {account}" for account in missing)]
    raise ValueError("\n".join(lines))


def build_export_outputs(
    args: argparse.Namespace,
    fund_enabled: bool,
    exporter: BeanExporter,
    investment_exporter: InvestmentExporter | None,
    investment_price_source: InvestmentPriceSource | None,
) -> dict[str, list[str]]:
    include_files = [args.fund_commodities] if fund_enabled and args.with_header else []
    normal_text = exporter.render(
        include_accounts=args.accounts if args.with_header else "",
        include_files=include_files,
        operating_currency=args.operating_currency if args.with_header else "",
        investment_header_options=fund_enabled and args.with_header,
    )
    outputs: dict[str, list[str]] = {args.output: [normal_text]}
    if investment_exporter and investment_price_source:
        append_investment_outputs(
            args,
            outputs,
            investment_exporter,
            investment_price_source,
        )
    return outputs


def append_investment_outputs(
    args: argparse.Namespace,
    outputs: dict[str, list[str]],
    investment_exporter: InvestmentExporter,
    investment_price_source: InvestmentPriceSource,
) -> None:
    if investment_transactions := investment_exporter.render_transactions():
        outputs.setdefault(args.fund_output, []).append(investment_transactions)
    investment_prices = investment_exporter.render_prices()
    if investment_price_source.existing_directives or investment_prices:
        merged_prices = investment_price_source.merged_with(investment_prices)
        outputs.setdefault(args.price_output, []).append(merged_prices)


def parse_values(values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        result.update(part.strip() for part in value.split(",") if part.strip())
    return result


def _zip_password_provider(password: str | None):
    if password is not None:
        return lambda _path: password
    return lambda path: getpass.getpass(f"Password for {path}: ")


def write_chunks(path: str, chunks: list[str]) -> None:
    from pathlib import Path

    text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).rstrip() + "\n"
    Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
