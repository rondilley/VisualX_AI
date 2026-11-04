"""Slice 1 gate: store, cursor, missing fields, deletion, cache scrub, logs, config."""

from __future__ import annotations

import json

import pytest

from tests.helpers import interaction, make_env, profile
from visualx.cache import request_hash
from visualx.config import ConfigError, load
from visualx.delete import delete_ids, find_id
from visualx.logs import NullLog, RunLog
from visualx.model import INTERACTION_OPTIONAL_FIELDS, ContractError, Interaction, status_for
from visualx.store import LockError, lock, now_iso


def test_insert_with_duplicates(tmp_path):
    _, store, _, _ = make_env(tmp_path)
    a = interaction("reply", "u1", "ego", spost="p1", tpost="p0", at="2026-10-01T00:00:00Z", text="hi")
    b = interaction("reply", "u1", "ego", spost="p1", tpost="p0", at="2026-10-01T00:00:00Z", text="hi")
    assert a.interaction_id == b.interaction_id
    assert store.put_interactions([a]) == 1
    assert store.put_interactions([b]) == 0
    assert len(store.interactions()) == 1


def test_cursor_save_and_load(tmp_path):
    _, store, _, _ = make_env(tmp_path)
    assert store.get_cursor(3, "p9") == (None, False)
    store.set_cursor(3, "p9", "tok2", False, "r1")
    assert store.get_cursor(3, "p9") == ("tok2", False)
    store.set_cursor(3, "p9", None, True, "r1")
    assert store.get_cursor(3, "p9") == (None, True)


def test_interaction_missing_fields_tsc33(tmp_path):
    """A repost from retweeted_by keeps source, target, observation time; post ID and time are absent."""
    _, store, _, _ = make_env(tmp_path)
    it = interaction("repost", "u2", "ego", tpost="p0", endpoint="GET /2/tweets/:id/retweeted_by")
    assert it.field_status["source_post_id"] == "absent"
    assert it.field_status["interaction_created_at"] == "absent"
    store.put_interactions([it])
    row = store.interactions()[0]
    assert row["source_post_id"] is None and row["interaction_created_at"] is None
    assert row["observed_at"] and row["source_user_id"] == "u2" and row["target_user_id"] == "ego"
    assert row["field_status"]["source_post_id"] == "absent"


def test_contract_rejects_invented_or_unmarked_fields():
    fs = status_for({}, INTERACTION_OPTIONAL_FIELDS)
    fs["source_post_id"] = "present"
    with pytest.raises(ContractError):
        Interaction(type="repost", source_user_id="a", target_user_id="b", observed_at=now_iso(),
                    source_endpoint="e", adapter="x", adapter_version="0", run_id="r", field_status=fs)
    fs2 = status_for({}, INTERACTION_OPTIONAL_FIELDS)
    with pytest.raises(ContractError):
        Interaction(type="repost", source_user_id="a", target_user_id="b", observed_at=now_iso(),
                    source_endpoint="e", adapter="x", adapter_version="0", run_id="r", field_status=fs2,
                    source_post_id="123")
    with pytest.raises(ContractError):
        Interaction(type="like", source_user_id="a", target_user_id="b", observed_at=now_iso(),
                    source_endpoint="e", adapter="x", adapter_version="0", run_id="r", field_status=fs2)
    del fs2["text"]
    with pytest.raises(ContractError):
        Interaction(type="repost", source_user_id="a", target_user_id="b", observed_at=now_iso(),
                    source_endpoint="e", adapter="x", adapter_version="0", run_id="r", field_status=fs2)


def _batch_body():
    return {
        "data": [
            {"id": "111", "username": "alpha", "name": "A"},
            {"id": "222", "username": "bravo", "name": "B", "description": "see x.com/i/status/9001"},
            {"id": "333", "username": "charlie", "name": "C"},
        ],
        "includes": {"tweets": [
            {"id": "9001", "author_id": "222", "text": "post by B"},
            {"id": "9002", "author_id": "333", "text": "reply", "in_reply_to_user_id": "222",
             "referenced_tweets": [{"type": "replied_to", "id": "9001"}],
             "entities": {"mentions": [{"id": "222", "username": "bravo"}, {"id": "111", "username": "alpha"}]}},
        ]},
    }


def test_cache_scrub_keeps_other_users(tmp_path):
    """Part of TSC-09: a user deletion rewrites a batch file and keeps the other users."""
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "ego")
    rh = request_hash("GET /2/users", {"ids": "111,222,333"})
    cache.put(rh, "GET /2/users", _batch_body())
    assert cache.files_with({"222"})
    store.put_profiles([profile("222", handle="bravo", name="B")])
    store.put_interactions([interaction("reply", "333", "222", spost="9002", tpost="9001", at="2026-10-01T00:00:00Z")])
    rep = delete_ids(store, cache, tmp_path / "config", ["222"], "user")
    assert rep.remaining == []
    body = cache.get(rh)
    assert [u["id"] for u in body["data"]] == ["111", "333"]
    tw = {t["id"]: t for t in body["includes"]["tweets"]}
    # The post by 222 is gone. The reply by 333 stays, without the references to 222.
    assert "9001" not in tw and "9002" in tw
    assert "in_reply_to_user_id" not in tw["9002"]
    assert [m["id"] for m in tw["9002"]["entities"]["mentions"]] == ["111"]
    assert find_id(store, cache, "222") == []
    assert not cache.files_with({"222"})


def test_delete_by_post_and_user_in_store(tmp_path):
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "ego")
    store.put_interactions([
        interaction("reply", "u1", "ego", spost="p1", tpost="p0", at="2026-10-01T00:00:00Z",
                    text="see https://x.com/u2/status/p2"),
        interaction("quote", "u2", "ego", spost="p2", tpost="p0", at="2026-10-02T00:00:00Z"),
        interaction("mention", "u2", "u1", spost="p2", at="2026-10-02T00:00:00Z"),
    ])
    store.put_profiles([profile("u1", handle="one"), profile("u2", handle="two")])
    store.put_post({"id": "p2", "author_id": "u2", "text": "quote"}, "test", "r1")
    rep = delete_ids(store, cache, tmp_path / "config", ["p2"], "post")
    assert rep.remaining == []
    assert rep.redacted_cells == 1  # the URL in the reply text by u1
    assert {i["source_post_id"] for i in store.interactions()} == {"p1"}
    rep = delete_ids(store, cache, tmp_path / "config", ["u1"], "user")
    assert rep.remaining == []
    assert store.interactions() == []
    assert set(store.profiles()) == {"u2"}
    with pytest.raises(ValueError):
        delete_ids(store, cache, tmp_path / "config", ["ego"], "user")


def test_delete_removes_override_entry(tmp_path):
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "ego")
    (tmp_path / "config" / "overrides.yaml").write_text(
        'overrides:\n  - {user_id: "u7", state: no_signals, note: n}\n  - {user_id: "u8", state: some_signals}\n',
        encoding="utf-8")
    rep = delete_ids(store, cache, tmp_path / "config", ["u7"], "user")
    assert rep.overrides_removed == 1
    cfg2 = load(tmp_path / "config", root=tmp_path)
    assert [o["user_id"] for o in cfg2.overrides] == ["u8"]


def test_cache_retention(tmp_path):
    _, store, cache, _ = make_env(tmp_path)
    rh = request_hash("GET /x", {})
    p = cache.put(rh, "GET /x", {"data": []})
    store.conn.execute("UPDATE cache_index SET fetched_at='2020-01-01T00:00:00'")
    assert cache.purge_older_than(30) == 1
    assert not p.exists()


def test_log_hashes_ids_and_rejects_text(tmp_path):
    log = RunLog(tmp_path / "log.jsonl", b"k" * 32)
    log.write("call", endpoint="GET /2/users", user_id="12345", ids=["1", "2"], status=200)
    raw = (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    rec = json.loads(raw)
    assert rec["user_id"] != "12345" and len(rec["user_id"]) == 24
    assert "12345" not in raw
    with pytest.raises(ValueError):
        log.write("call", text="hello")
    with pytest.raises(ValueError):
        NullLog().write("x", access_token="t")


def test_lock_is_exclusive(tmp_path):
    with lock(tmp_path):
        with pytest.raises(LockError):
            with lock(tmp_path):
                raise AssertionError("second lock must not open")
    with lock(tmp_path):
        assert (tmp_path / "visualx.lock").exists()


def test_config_rejects_bad_color(tmp_path):
    make_env(tmp_path)
    (tmp_path / "config" / "visualx.local.toml").write_text(
        '[render.state_colors]\nmany_signals = "red; background:url(x)"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path / "config", root=tmp_path)
