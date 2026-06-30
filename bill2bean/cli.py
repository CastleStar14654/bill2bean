from __future__ import annotations

import argparse
import getpass
import sys

from .config import Config
from .exporter import filter_review_rows, render_beancount, required_accounts_for_export
from .investments import (
    extract_price_directives,
    merge_price_directives,
    render_fund_export,
    required_accounts_for_fund_export,
)
from .ledger import TransactionList, extract_accounts, read_review_csv
from .parsers import parser_for


def main(argv: list[str] | None = None) -> int:
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
    export.add_argument("--start-date", default="", help="only export rows on or after YYYY-MM-DD")
    export.add_argument("--end-date", default="", help="only export rows on or before YYYY-MM-DD")
    export.add_argument("-c", "--config", default="config.toml", help="config used for investment export")
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

    args = parser.parse_args(argv)
    if args.command == "review":
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
            previous_rows = read_review_csv(args.previous_review) if args.previous_review else None
            TransactionList(txs, config).normalize().write_review_csv(args.output, previous_rows)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"wrote {args.output} with {len(txs)} rows")
        return 0

    if args.with_header and not args.accounts:
        print("--with-header requires --accounts", file=sys.stderr)
        return 2

    include_sources = parse_values(args.include_source)
    exclude_sources = parse_values(args.exclude_source)
    rows = read_review_csv(args.review_csv)
    fund_enabled = any(
        [args.fund_commodities, args.fund_output is not None, args.price_output is not None]
    )
    if fund_enabled and not all(
        [args.fund_commodities, args.fund_output is not None, args.price_output is not None]
    ):
        print(
            "--fund-commodities, --fund-output, and --price-output must be used together",
            file=sys.stderr,
        )
        return 2
    if args.fund_output == "":
        args.fund_output = args.output
    if args.price_output == "":
        args.price_output = args.output
    config = Config.load(args.config)
    try:
        filtered_rows = filter_review_rows(
            rows,
            include_sources=include_sources,
            exclude_sources=exclude_sources,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.accounts:
            required_accounts = required_accounts_for_export(
                rows,
                include_sources=include_sources,
                exclude_sources=exclude_sources,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            if fund_enabled:
                required_accounts |= required_accounts_for_fund_export(
                    filtered_rows,
                    args.fund_commodities,
                    config.funds,
                )
            missing = sorted(required_accounts - extract_accounts(args.accounts))
            if missing:
                print("unknown accounts:", file=sys.stderr)
                for account in missing:
                    print(f"  {account}", file=sys.stderr)
                return 2
        normal_text = render_beancount(
            rows,
            include_accounts=args.accounts if args.with_header else "",
            include_files=[args.fund_commodities] if fund_enabled and args.with_header else [],
            operating_currency=args.operating_currency if args.with_header else "",
            investment_header_options=fund_enabled and args.with_header,
            include_sources=include_sources,
            exclude_sources=exclude_sources,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        outputs: dict[str, list[str]] = {args.output: [normal_text]}
        if fund_enabled:
            price_text = read_text_if_exists(args.price_output)
            existing_prices = extract_price_directives(price_text)
            fund_result = render_fund_export(
                filtered_rows,
                args.fund_commodities,
                price_text,
                config.funds,
                fetch_prices=args.fetch_fund_prices,
                bean_price_command=args.bean_price_command,
            )
            if fund_result.transactions:
                outputs.setdefault(args.fund_output, []).append(fund_result.transactions)
            if existing_prices or fund_result.prices:
                merged_prices = merge_price_directives(existing_prices, fund_result.prices)
                outputs.setdefault(args.price_output, []).append(merged_prices)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for output, chunks in outputs.items():
        write_chunks(output, chunks)
    for output in outputs:
        print(f"wrote {output}")
    return 0


def parse_values(values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        result.update(part.strip() for part in value.split(",") if part.strip())
    return result


def _zip_password_provider(password: str | None):
    if password is not None:
        return lambda _path: password
    return lambda path: getpass.getpass(f"Password for {path}: ")


def read_text_if_exists(path: str) -> str:
    from pathlib import Path

    target = Path(path)
    if not target.exists():
        return ""
    return target.read_text(encoding="utf-8")


def write_chunks(path: str, chunks: list[str]) -> None:
    from pathlib import Path

    text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).rstrip() + "\n"
    Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
