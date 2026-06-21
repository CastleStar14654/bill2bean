from __future__ import annotations

import argparse
import sys

from .config import Config
from .exporter import export_beancount, filter_review_rows, required_accounts_for_export
from .ledger import TransactionList, extract_accounts, read_review_csv
from .parsers import parser_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bill2bean")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="parse bills and write editable review CSV")
    review.add_argument("files", nargs="+")
    review.add_argument("-c", "--config", default="config.toml")
    review.add_argument("-o", "--output", default="review.csv")

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

    args = parser.parse_args(argv)
    if args.command == "review":
        config = Config.load(args.config)
        txs = []
        for filename in args.files:
            txs.extend(parser_for(filename).parse(filename))
        TransactionList(txs, config).normalize().write_review_csv(args.output)
        print(f"wrote {args.output} with {len(txs)} rows")
        return 0

    if args.with_header and not args.accounts:
        print("--with-header requires --accounts", file=sys.stderr)
        return 2

    include_sources = parse_values(args.include_source)
    exclude_sources = parse_values(args.exclude_source)
    rows = read_review_csv(args.review_csv)
    try:
        filter_review_rows(
            rows,
            include_sources=include_sources,
            exclude_sources=exclude_sources,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.accounts:
        missing = sorted(
            required_accounts_for_export(
                rows,
                include_sources=include_sources,
                exclude_sources=exclude_sources,
                start_date=args.start_date,
                end_date=args.end_date,
            )
            - extract_accounts(args.accounts)
        )
        if missing:
            print("unknown accounts:", file=sys.stderr)
            for account in missing:
                print(f"  {account}", file=sys.stderr)
            return 2
    export_beancount(
        args.review_csv,
        args.output,
        include_accounts=args.accounts if args.with_header else "",
        operating_currency=args.operating_currency if args.with_header else "",
        include_sources=include_sources,
        exclude_sources=exclude_sources,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    print(f"wrote {args.output}")
    return 0


def parse_values(values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        result.update(part.strip() for part in value.split(",") if part.strip())
    return result


if __name__ == "__main__":
    raise SystemExit(main())
