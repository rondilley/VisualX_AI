"""Internal records: Interaction and Profile (ARCHITECTURE AD-02).

Adapters write only these two records. Each record carries a complete
``field_status`` map for its optional fields. A record with a value must mark
that field ``present``; a record without a value must mark it ``absent`` or
``unreadable``. The model never fills a missing field (invariant 10, DR-02).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = 1

FieldStatus = Literal["present", "absent", "unreadable"]
FIELD_STATES: frozenset[str] = frozenset({"present", "absent", "unreadable"})

InteractionType = Literal["reply", "quote", "mention", "repost"]
INTERACTION_TYPES: tuple[str, ...] = ("reply", "quote", "mention", "repost")

SIGNAL_STATES: tuple[str, ...] = ("not_scored", "no_signals", "some_signals", "many_signals")
STATE_LABELS: dict[str, str] = {
    "not_scored": "Not scored",
    "no_signals": "No signals seen",
    "some_signals": "Some signals",
    "many_signals": "Many signals",
}

INTERACTION_OPTIONAL_FIELDS: tuple[str, ...] = (
    "source_post_id",
    "target_post_id",
    "interaction_created_at",
    "conversation_id",
    "text",
    "urls",
)

PROFILE_OPTIONAL_FIELDS: tuple[str, ...] = (
    "handle",
    "name",
    "description",
    "url",
    "location",
    "created_at",
    "followers_count",
    "following_count",
    "tweet_count",
    "listed_count",
    "verified_type",
    "protected",
    "profile_image_url",
)


class ContractError(ValueError):
    """An adapter tried to write a record that breaks the adapter contract."""


def _check_field_status(kind: str, values: dict[str, Any], status: dict[str, str], fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if f not in status]
    if missing:
        raise ContractError(f"{kind}: field_status has no entry for {missing}")
    for f in fields:
        s = status[f]
        if s not in FIELD_STATES:
            raise ContractError(f"{kind}: field_status[{f}] = {s!r} is not a known state")
        v = values.get(f)
        has_value = v is not None and not (f == "urls" and v == [] and s != "present")
        if s == "present" and v is None:
            raise ContractError(f"{kind}: {f} is marked present but has no value")
        if s != "present" and has_value:
            raise ContractError(f"{kind}: {f} has a value but is marked {s}")


def normalize_text(text: str) -> str:
    """Normalized text for duplicate checks: NFKC, lower case, no URLs or @handles, single spaces."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"@\w+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


@dataclass
class Interaction:
    type: str
    source_user_id: str
    target_user_id: str
    observed_at: str
    source_endpoint: str
    adapter: str
    adapter_version: str
    run_id: str
    field_status: dict[str, str]
    source_post_id: str | None = None
    target_post_id: str | None = None
    interaction_created_at: str | None = None
    conversation_id: str | None = None
    text: str | None = None
    urls: list[str] | None = None
    raw_request_hash: str | None = None
    interaction_id: str = ""
    text_hash: str | None = None

    def __post_init__(self) -> None:
        if self.type not in INTERACTION_TYPES:
            raise ContractError(f"Interaction: unknown type {self.type!r}")
        for req in ("source_user_id", "target_user_id", "observed_at", "source_endpoint", "adapter", "adapter_version", "run_id"):
            if not getattr(self, req):
                raise ContractError(f"Interaction: {req} is required")
        _check_field_status("Interaction", asdict(self), self.field_status, INTERACTION_OPTIONAL_FIELDS)
        if self.text is not None and self.text_hash is None:
            self.text_hash = text_hash(self.text)
        if not self.interaction_id:
            post = self.source_post_id or self.target_post_id or ""
            key = f"{self.type}|{self.source_user_id}|{self.target_user_id}|{post}"
            self.interaction_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass
class Profile:
    user_id: str
    fetched_at: str
    source_endpoint: str
    adapter: str
    adapter_version: str
    field_status: dict[str, str]
    handle: str | None = None
    name: str | None = None
    description: str | None = None
    url: str | None = None
    location: str | None = None
    created_at: str | None = None
    followers_count: int | None = None
    following_count: int | None = None
    tweet_count: int | None = None
    listed_count: int | None = None
    verified_type: str | None = None
    protected: bool | None = None
    profile_image_url: str | None = None

    def __post_init__(self) -> None:
        for req in ("user_id", "fetched_at", "source_endpoint", "adapter", "adapter_version"):
            if not getattr(self, req):
                raise ContractError(f"Profile: {req} is required")
        _check_field_status("Profile", asdict(self), self.field_status, PROFILE_OPTIONAL_FIELDS)


def status_for(values: dict[str, Any], fields: tuple[str, ...], unreadable: frozenset[str] = frozenset()) -> dict[str, str]:
    """Build a field_status map from the values that an endpoint gave.

    ``unreadable`` names fields that the endpoint gives in principle but that the
    response hid (for example an access restriction). Everything else without a
    value is ``absent``.
    """
    out: dict[str, str] = {}
    for f in fields:
        if f in unreadable:
            out[f] = "unreadable"
        elif values.get(f) is None:
            out[f] = "absent"
        else:
            out[f] = "present"
    return out


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
