from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import ClassVar


def quote(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def escape_directive_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class BasePosting:
    account: str
    amount: str = ""
    currency: str = ""
    flagged: bool = False
    supports_implicit_amount: ClassVar[bool] = False

    def format(self, implicit: bool = False) -> str:
        flag = "! " if self.flagged else ""
        line = f"  {flag}{self.account}"
        if self.amount and self.currency and not (
            implicit and self.supports_implicit_amount
        ):
            line += f"  {self.amount} {self.currency}"
        return self.format_suffix(line)

    def format_suffix(self, line: str) -> str:
        return line


def format_tags_links(tags: str, links: str) -> str:
    tag_tokens = [prefixed_token(token, "#") for token in split_tokens(tags)]
    link_tokens = [prefixed_token(token, "^") for token in split_tokens(links)]
    return " ".join(tag_tokens + link_tokens)


def split_tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,;]+", value.strip()) if part]


def prefixed_token(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value
    return prefix + value
