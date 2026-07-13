from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Generic, Iterable, TypeVar

from .beancount_format import BaseTransactionDraft


ItemT = TypeVar("ItemT")
DraftT = TypeVar("DraftT", bound=BaseTransactionDraft)


@dataclass(frozen=True)
class BaseTransactionExporter(Generic[ItemT, DraftT]):
    @property
    def transaction_items(self) -> Iterable[ItemT]:
        raise NotImplementedError

    @cached_property
    def transaction_drafts(self) -> tuple[DraftT, ...]:
        drafts: list[DraftT] = []
        for item in self.transaction_items:
            draft = self.build_transaction_draft(item)
            if draft:
                drafts.append(draft)
        return tuple(drafts)

    def build_transaction_draft(self, item: ItemT) -> DraftT | None:
        raise NotImplementedError

    def render_transactions(self) -> str:
        rendered = [draft.format() for draft in self.transaction_drafts]
        if not rendered:
            return ""
        return "\n".join(chunk for chunk in rendered if chunk).rstrip() + "\n"

    def required_accounts(self) -> set[str]:
        accounts: set[str] = set()
        for draft in self.transaction_drafts:
            accounts.update(
                posting.account for posting in draft.postings if posting.account
            )
        return accounts
