from __future__ import annotations

from decimal import Decimal
import re


def quote(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def escape_directive_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def format_decimal(value: Decimal) -> str:
    return format(value, "f")


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
