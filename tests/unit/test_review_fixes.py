"""Fixes from the multi-AI code review 20261102-225212."""

from __future__ import annotations

import shutil

import pytest

from tests.helpers import interaction, make_env, mark_stage, profile
from visualx.analyze import run_analyze
from visualx.build import run_build
from visualx.config import load
from visualx.delete import delete_ids
from visualx.render.render import run_render
from visualx.score.engine import default_rules_path, run_score
from visualx.store import StaleError

T = "2026-10-01T00:00:00.000Z"


def _pipeline_env(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    store.set_meta("ego_user_id", "ego")
    store.set_meta("ego_handle", "owner")
    store.put_profiles([profile("ego", handle="owner", name="Owner"), profile("a", handle="alpha", name="A")])
    store.put_interactions([interaction("reply", "a", "ego", spost="1", tpost="0", at=T)])
    mark_stage(store, "collect")
    return cfg, store, cache, log


def test_render_refuses_analyze_and_score_from_different_builds(tmp_path):
    """The failure path from the review: collect, build, analyze, score; build again; score only; render."""
    cfg, store, cache, log = _pipeline_env(tmp_path)
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    run_score(cfg, store, log)
    run_render(cfg, store, log)
    run_build(cfg, store, log)
    run_score(cfg, store, log)  # analyze not run again
    with pytest.raises(StaleError, match="analyze"):
        run_render(cfg, store, log)
    run_analyze(cfg, store, log)
    run_render(cfg, store, log)  # consistent again


def test_new_collect_makes_build_outputs_stale(tmp_path):
    cfg, store, cache, log = _pipeline_env(tmp_path)
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    mark_stage(store, "collect")  # a newer collection
    with pytest.raises(StaleError, match="build"):
        run_analyze(cfg, store, log)
    with pytest.raises(StaleError, match="build"):
        run_score(cfg, store, log)


def test_rules_come_from_the_loaded_config_folder(tmp_path):
    make_env(tmp_path)
    other = tmp_path / "other_config"
    shutil.copytree(tmp_path / "config", other)
    cfg = load(other, root=tmp_path)
    assert default_rules_path(cfg) == other / "rules.yaml"


def test_finding_evidence_ids_are_indexed_as_post_ids(tmp_path):
    cfg, store, cache, log = _pipeline_env(tmp_path)
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    run_score(cfg, store, log)
    store.conn.execute(
        "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
        ("f1", "reply_burst", "Many accounts replied in a short time", "notable", "Summary",
         '["a"]', '{"source_post_ids": ["987654321012"]}', "s1"),
    )
    path = run_render(cfg, store, log)
    rows = store.q("SELECT render_id FROM render_members WHERE post_id=?", ("987654321012",))
    assert rows, "the post ID in the evidence must be in render_members"
    rep = delete_ids(store, cache, tmp_path / "config", ["987654321012"], "post")
    assert str(path.resolve()) in [str(p) for p in map(lambda x: __import__("pathlib").Path(x).resolve(), rep.renders_deleted)]
    assert rep.remaining == []


def test_orphan_render_file_with_the_id_is_deleted(tmp_path):
    cfg, store, cache, log = _pipeline_env(tmp_path)
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    run_score(cfg, store, log)
    path = run_render(cfg, store, log)
    orphan = path.with_name("map_copy_from_backup.html")
    shutil.copy(path, orphan)
    rep = delete_ids(store, cache, tmp_path / "config", ["a"], "user", extra_files=[orphan])
    assert not orphan.exists()
    assert rep.remaining == []
