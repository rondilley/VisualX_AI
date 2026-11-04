"""Shared fixture builders for the signal-engine tests (Slice 5).

Records go into a tmp_path store through the Store API, the same path that
the collector uses. This module has no tests of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from visualx.config import Config
from visualx.logs import NullLog
from visualx.model import (
    INTERACTION_OPTIONAL_FIELDS,
    PROFILE_OPTIONAL_FIELDS,
    Interaction,
    Profile,
    status_for,
)
from visualx.score.engine import RuleSet, load_rules, run_score
from visualx.store import Store, now_iso

REPO = Path(__file__).resolve().parents[2]
RULES_PATH = REPO / "config" / "rules.yaml"

EGO = "100"
EGO_HANDLE = "rondilley"
EGO_NAME = "Ron Dilley"
ACC = "200"  # the account under test
FETCHED = "2026-11-01T00:00:00.000Z"

PROFILE_DEFAULTS: dict[str, Any] = {
    "handle": "quietreader",
    "name": "Quiet Reader",
    "description": "I write about software and networks.",
    "url": "https://example.com",
    "location": "Somewhere",
    "created_at": "2015-01-01T00:00:00.000Z",
    "followers_count": 500,
    "following_count": 300,
    "tweet_count": 2000,
    "listed_count": 3,
    "verified_type": "none",
    "protected": False,
    "profile_image_url": "https://pbs.twimg.com/profile_images/1/a.jpg",
}


def profile(user_id: str = ACC, unreadable: tuple[str, ...] = (), **over: Any) -> Profile:
    """A profile that fires no rule. ``field=None`` makes the field absent."""
    values = {**PROFILE_DEFAULTS, **over}
    for f in unreadable:
        values[f] = None
    return Profile(
        user_id=user_id, fetched_at=FETCHED, source_endpoint="users/lookup", adapter="test",
        adapter_version="1", field_status=status_for(values, PROFILE_OPTIONAL_FIELDS, frozenset(unreadable)),
        **{k: values[k] for k in PROFILE_OPTIONAL_FIELDS},
    )


def ego_profile() -> Profile:
    return profile(EGO, handle=EGO_HANDLE, name=EGO_NAME, followers_count=5000, listed_count=100)


_counter = [0]


def interaction(
    src: str = ACC,
    tgt: str = EGO,
    type: str = "reply",
    text: str | None = "Thanks, this was a useful post about graphs.",
    created_at: str | None = "2026-10-01T12:00:00.000Z",
    urls: list[str] | None = None,
    post_id: str | None = None,
    unreadable: tuple[str, ...] = (),
    target_post_id: str = "800000001",
) -> Interaction:
    """One interaction. With no ``urls`` given, the post has no links (``[]``, present), as the X API gives it."""
    if urls is None:
        urls = []
    _counter[0] += 1
    values: dict[str, Any] = {
        "source_post_id": post_id or f"9{_counter[0]:08d}",
        "target_post_id": target_post_id,
        "interaction_created_at": created_at,
        "conversation_id": "800000001",
        "text": text,
        "urls": urls,
    }
    if type == "repost":
        values.update(interaction_created_at=None, text=None, urls=None, source_post_id=None)
    for f in unreadable:
        values[f] = None
    return Interaction(
        type=type, source_user_id=src, target_user_id=tgt, observed_at=FETCHED,
        source_endpoint="test", adapter="test", adapter_version="1", run_id="run_test",
        field_status=status_for(values, INTERACTION_OPTIONAL_FIELDS, frozenset(unreadable)), **values,
    )


def make_store(tmp_path: Path, with_build: bool = True) -> Store:
    store = Store(tmp_path / "store.sqlite")
    store.set_meta("ego_user_id", EGO)
    store.set_meta("ego_handle", EGO_HANDLE)
    if with_build:
        sid = store.begin_stage("build", "run_test", {"collect": "collect_x"})
        store.finish_stage(sid, {"nodes": 0})
    return store


def make_config(tmp_path: Path, overrides: list[dict[str, Any]] | None = None, allowlist: list[str] | None = None) -> Config:
    return Config(root=tmp_path, raw={}, rules=[], overrides=list(overrides or []), allowlist=list(allowlist or []))


def default_rules() -> RuleSet:
    return load_rules(RULES_PATH)


class Scored:
    """Rows that one score run wrote."""

    def __init__(self, store: Store, summary: dict[str, Any], log: NullLog):
        self.store = store
        self.summary = summary
        self.log = log
        self.states = {r["user_id"]: r["state"] for r in store.q("SELECT * FROM node_states")}
        self.notes = {r["user_id"]: r["override_note"] for r in store.q("SELECT * FROM node_states")}
        self.results: dict[str, dict[str, dict[str, Any]]] = {}
        for r in store.q("SELECT * FROM rule_results"):
            self.results.setdefault(r["user_id"], {})[r["rule_id"]] = dict(r)
        self.skips: dict[str, dict[str, set[str]]] = {}
        for r in store.q("SELECT * FROM rule_skips"):
            self.skips.setdefault(r["user_id"], {}).setdefault(r["rule_id"], set()).add(r["missing_field"])

    def fired(self, user_id: str = ACC) -> set[str]:
        return set(self.results.get(user_id, {}))

    def hit(self, rule_id: str, user_id: str = ACC) -> dict[str, Any]:
        return self.results[user_id][rule_id]

    def skipped(self, user_id: str = ACC) -> dict[str, set[str]]:
        return self.skips.get(user_id, {})


def score(
    tmp_path: Path,
    profiles: list[Profile] | None = None,
    interactions: list[Interaction] | None = None,
    *,
    ruleset: RuleSet | None = None,
    overrides: list[dict[str, Any]] | None = None,
    allowlist: list[str] | None = None,
    errors: dict[str, str] | None = None,
    store: Store | None = None,
) -> Scored:
    """Insert the records, run the stage, and return the rows it wrote.

    With no profiles given, the account under test gets the default profile and
    the ego gets its profile. With no interactions given, the account sends one
    reply to the ego.
    """
    st = store or make_store(tmp_path)
    profs = profiles if profiles is not None else [ego_profile(), profile()]
    items = interactions if interactions is not None else [interaction()]
    st.put_profiles(profs)
    st.put_interactions(items)
    for uid, err in (errors or {}).items():
        st.put_profile_error(uid, err)
    cfg = make_config(tmp_path, overrides, allowlist)
    log = NullLog()
    summary = run_score(cfg, st, log, ruleset=ruleset or default_rules())
    return Scored(st, summary, log)


def iso_days_before(ts: str, days: float) -> str:
    from datetime import datetime, timedelta

    t = datetime.fromisoformat(ts.replace("Z", "+00:00")) - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


__all__ = [
    "ACC", "EGO", "EGO_HANDLE", "EGO_NAME", "FETCHED", "RULES_PATH", "Scored", "default_rules",
    "ego_profile", "interaction", "iso_days_before", "make_config", "make_store", "now_iso",
    "profile", "score",
]
