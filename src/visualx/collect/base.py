"""Collector interface and the adapter contract (ARCHITECTURE AD-02, AR-03).

An adapter turns the responses of one data source into ``Interaction`` and
``Profile`` records. The analysis stages read only these records. Each record
carries a complete ``field_status`` map; ``model.Interaction`` and
``model.Profile`` refuse a record without one. An adapter records what the
source gives and marks the other fields ``absent`` or ``unreadable``. It
never makes up a value (invariant 10, DR-02).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..model import Interaction, Profile


class SourceAdapter(Protocol):
    """The mapping half of a collector: source objects to internal records."""

    adapter: str
    adapter_version: str

    def map_user(self, user: dict[str, Any], endpoint: str, fetched_at: str | None = None) -> Profile: ...

    def map_post(
        self,
        post: dict[str, Any],
        ego_id: str,
        endpoint: str,
        run_id: str,
        **kwargs: Any,
    ) -> list[Interaction]: ...


@dataclass
class Page:
    """The records from one response page. The plan writes a page in one transaction."""

    interactions: list[Interaction] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    profile_errors: dict[str, str] = field(default_factory=dict)
    next_token: str | None = None
    resources: int = 0


def no_self_edges(items: Iterable[Interaction]) -> list[Interaction]:
    """Drop interactions from a user to the same user (FR-21)."""
    return [i for i in items if i.source_user_id != i.target_user_id]
