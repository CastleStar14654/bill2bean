from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import ClassVar, Generic, TypeVar


PostingT = TypeVar("PostingT", bound="BasePosting")


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


@dataclass(frozen=True)
class BaseTransactionDraft(Generic[PostingT]):
    date: str
    payee: str
    narration: str
    metadata: list[tuple[str, str]]
    postings: list[PostingT]
    tags_links: str = ""

    def format(self) -> str:
        payee = quote(self.payee)
        narration = quote(self.narration)
        suffix = f" {self.tags_links}" if self.tags_links else ""
        lines = [f"{self.date} * {payee} {narration}{suffix}"]
        for key, value in self.metadata:
            lines.append(f"  {key}: {quote(value)}")
        implicit_index = self.implicit_posting_index()
        for index, posting in enumerate(self.postings):
            lines.append(posting.format(implicit=index == implicit_index))
        return "\n".join(lines) + "\n"

    def implicit_posting_index(self) -> int | None:
        return None


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
