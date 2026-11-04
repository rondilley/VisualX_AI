"""Shared test helpers: a temporary config folder and store."""

from __future__ import annotations

import shutil
from pathlib import Path

from visualx import config as config_mod
from visualx.cache import Cache
from visualx.logs import NullLog
from visualx.model import (
    INTERACTION_OPTIONAL_FIELDS,
    PROFILE_OPTIONAL_FIELDS,
    Interaction,
    Profile,
    status_for,
)
from visualx.store import Store, now_iso

REPO = Path(__file__).resolve().parents[1]


def make_env(tmp: Path, extra_toml: str = ""):
    cdir = tmp / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "config" / "visualx.toml", cdir / "visualx.toml")
    rules = REPO / "config" / "rules.yaml"
    if rules.exists():
        shutil.copy(rules, cdir / "rules.yaml")
    data = (tmp / "data").as_posix()
    (cdir / "visualx.local.toml").write_text(f'[data]\ndir = "{data}"\n[owner]\nhandle = ""\n{extra_toml}', encoding="utf-8")
    cfg = config_mod.load(cdir, root=tmp)
    store = Store(cfg.store_path)
    cache = Cache(store, cfg.cache_dir)
    return cfg, store, cache, NullLog()


def mark_stage(store: Store, stage: str) -> str:
    sid = store.begin_stage(stage, None, {})
    store.finish_stage(sid, {})
    return sid


def interaction(type_: str, src: str, tgt: str, *, spost: str | None = None, tpost: str | None = None,
                at: str | None = None, text: str | None = None, urls: list[str] | None = None,
                endpoint: str = "test") -> Interaction:
    vals = {"source_post_id": spost, "target_post_id": tpost, "interaction_created_at": at,
            "conversation_id": None, "text": text, "urls": urls}
    return Interaction(
        type=type_, source_user_id=src, target_user_id=tgt, observed_at=now_iso(),
        source_endpoint=endpoint, adapter="test", adapter_version="0", run_id="r1",
        field_status=status_for(vals, INTERACTION_OPTIONAL_FIELDS),
        source_post_id=spost, target_post_id=tpost, interaction_created_at=at, text=text, urls=urls,
    )


def profile(uid: str, **vals) -> Profile:
    return Profile(
        user_id=uid, fetched_at=now_iso(), source_endpoint="test", adapter="test", adapter_version="0",
        field_status=status_for(vals, PROFILE_OPTIONAL_FIELDS), **vals,
    )
