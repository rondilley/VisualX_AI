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
    store.set_meta("ego_user_id", "100")
    store.put_interactions([
        interaction("reply", "101", "100", spost="901", tpost="900", at="2026-10-01T00:00:00Z",
                    text="see https://x.com/u2/status/902"),
        interaction("quote", "102", "100", spost="902", tpost="900", at="2026-10-02T00:00:00Z"),
        interaction("mention", "102", "101", spost="902", at="2026-10-02T00:00:00Z"),
    ])
    store.put_profiles([profile("101", handle="one"), profile("102", handle="two")])
    store.put_post({"id": "902", "author_id": "102", "text": "quote"}, "test", "r1")
    rep = delete_ids(store, cache, tmp_path / "config", ["902"], "post")
    assert rep.remaining == []
    assert rep.redacted_cells == 1  # the URL in the reply text by 101
    assert {i["source_post_id"] for i in store.interactions()} == {"901"}
    rep = delete_ids(store, cache, tmp_path / "config", ["101"], "user")
    assert rep.remaining == []
    assert store.interactions() == []
    assert set(store.profiles()) == {"102"}
    with pytest.raises(ValueError):
        delete_ids(store, cache, tmp_path / "config", ["100"], "user")


def test_delete_removes_override_entry(tmp_path):
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    (tmp_path / "config" / "overrides.yaml").write_text(
        'overrides:\n  - {user_id: "107", state: no_signals, note: n}\n  - {user_id: "108", state: some_signals}\n',
        encoding="utf-8")
    rep = delete_ids(store, cache, tmp_path / "config", ["107"], "user")
    assert rep.overrides_removed == 1
    cfg2 = load(tmp_path / "config", root=tmp_path)
    assert [o["user_id"] for o in cfg2.overrides] == ["108"]


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


# -- Review 20261103: deletion defects A-01, A-02, A-03 and log IDs A-05 --


def _snapshot_meta(store):
    return {r["key"]: r["value"] for r in store.q("SELECT key, value FROM meta")}


@pytest.mark.parametrize("bad", ["", " ", "%", "12_34", "h:", "h:bad handle", "1" * 26, "../x"])
def test_delete_refuses_a_malformed_id_and_changes_nothing(tmp_path, bad):
    # A-01: an empty ID matched each row with LIKE '%%' and the redaction regex
    # put "[deleted]" between the characters of each JSON cell.
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    store.put_profiles([profile("101", handle="one")])
    store.put_interactions([interaction("reply", "101", "100", spost="901", tpost="900", at="2026-10-01T00:00:00Z")])
    before = (_snapshot_meta(store), store.interactions(), store.profiles())
    for kind in ("user", "post"):
        with pytest.raises(ValueError, match="not a valid"):
            delete_ids(store, cache, tmp_path / "config", [bad], kind)
    assert (_snapshot_meta(store), store.interactions(), store.profiles()) == before


def test_delete_refuses_an_empty_id_list_and_empty_patterns(tmp_path):
    from visualx.cache import _id_pattern

    _, store, cache, _ = make_env(tmp_path)
    with pytest.raises(ValueError):
        delete_ids(store, cache, tmp_path / "config", [], "user")
    with pytest.raises(ValueError):
        _id_pattern({""})
    with pytest.raises(ValueError):
        cache.scrub({""})


def test_delete_accepts_provisional_user_ids_but_not_provisional_post_ids(tmp_path):
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    rep = delete_ids(store, cache, tmp_path / "config", ["h:some_handle"], "user")
    assert rep.remaining == []
    with pytest.raises(ValueError):
        delete_ids(store, cache, tmp_path / "config", ["h:some_handle"], "post")


def test_delete_of_two_seed_users_removes_their_cursor_rows(tmp_path):
    # A-02: step 6 keeps one cursor row for each seed, keyed by the user ID. The
    # redaction rewrote both rows to (6, "[deleted]") and the second one collided.
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    for u in ("201", "202"):
        store.put_profiles([profile(u, handle=f"seed{u}")])
        store.set_cursor(6, u, f"tok{u}", False, "r1")
    store.set_cursor(1, "100", None, True, "r1")
    rep = delete_ids(store, cache, tmp_path / "config", ["201", "202"], "user")
    assert rep.remaining == []
    assert [(r["step"], r["item"]) for r in store.q("SELECT step, item FROM cursor ORDER BY step")] == [(1, "100")]


def test_cache_scrub_finds_an_id_that_occurs_only_inside_a_string(tmp_path):
    # A-03: a Grok response names a post only in a status URL (citations and model
    # text). The cache_ids index has no entry for it, so the scrub skipped the
    # file and the delete could not complete.
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    body = {"output_text": "see https://x.com/someone/status/1850000000000000001 here",
            "citations": ["https://x.com/i/status/1850000000000000001", "https://x.com/i/status/1850000000000000002"]}
    rh = request_hash("POST /v1/responses", {"q": "w1"})
    cache.put(rh, "POST /v1/responses", body)
    assert cache.files_with({"1850000000000000001"}) == []  # not in the ID-key index
    rep = delete_ids(store, cache, tmp_path / "config", ["1850000000000000001"], "post")
    assert rep.remaining == []
    assert rep.cache_files_scrubbed == 1
    kept = cache.get(rh)
    assert "1850000000000000001" not in json.dumps(kept)
    assert kept["citations"][1] == "https://x.com/i/status/1850000000000000002"


def test_log_hashes_since_id_and_pagination_tokens():
    # A-05: since_id holds the post ID of another user (steps 2 and 6).
    from visualx.logs import NullLog

    log = NullLog()
    log.write("x_api_call", params={"since_id": "1999999999999999999", "until_id": "1888888888888888888",
                                    "pagination_token": "abc", "max_results": 100})
    rec = log.records[0]["params"]
    assert "1999999999999999999" not in json.dumps(rec) and "1888888888888888888" not in json.dumps(rec)
    assert rec["pagination_token"] != "abc" and rec["max_results"] == 100


def test_config_refuses_billed_expansions(tmp_path):
    # A-08 (review 20261103): X gives no maximum for "includes", so the plan
    # cannot reserve the maximum cost of a call with this setting.
    make_env(tmp_path)
    local = tmp_path / "config" / "visualx.local.toml"
    local.write_text(local.read_text(encoding="utf-8") + "\n[billing_assumptions]\nexpansions_billed = true\n",
                     encoding="utf-8")
    with pytest.raises(ConfigError, match="expansions_billed"):
        load(tmp_path / "config", root=tmp_path)


def test_drop_text_keeps_the_domains_when_it_runs_two_times(tmp_path):
    # A-11 (review 20261103): the second run parsed the stored hosts as URLs and
    # removed them, so the domain rules lost their input.
    _, store, cache, _ = make_env(tmp_path)
    store.put_interactions([interaction("reply", "101", "100", spost="901", tpost="900", at="2026-10-01T00:00:00Z",
                                        text="see https://deals.example/p/1",
                                        urls=["https://deals.example/p/1", "https://www.other.example/x?y=1"])])
    assert store.drop_text() == 1
    first = store.interactions()[0]
    assert first["text"] is None and first["urls"] == ["deals.example", "www.other.example"]
    assert first["field_status"]["urls"] == "present" and first["field_status"]["url_paths"] == "absent"
    store.drop_text()
    again = store.interactions()[0]
    assert again["urls"] == first["urls"] and again["field_status"] == first["field_status"]


def test_cache_drop_text_removes_post_text_and_keeps_ids(tmp_path):
    # A-12 (review 20261103): retain_text = false left the text in the raw cache.
    _, store, cache, _ = make_env(tmp_path)
    rh = request_hash("GET /2/users", {"ids": "111,222,333"})
    cache.put(rh, "GET /2/users", _batch_body())
    cache.put(request_hash("xai", {}), "xai", {"output_text": "model text", "citations": ["https://x.com/i/status/9"]})
    assert cache.drop_text() == 2
    body = cache.get(rh)
    assert "post by B" not in json.dumps(body)
    assert all("text" not in tw for tw in body["includes"]["tweets"])
    assert [t["id"] for t in body["includes"]["tweets"]] == ["9001", "9002"]
    assert cache.drop_text() == 0


def test_user_delete_also_removes_the_handle(tmp_path):
    # A-13 (review 20261103): a Grok cache file that names the user only by handle,
    # and a reply by another user that mentions the handle, kept the handle.
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    store.set_meta("ego_handle", "owner")
    store.put_profiles([profile("101", handle="victim_acct"), profile("102", handle="bystander")])
    store.put_interactions([
        interaction("reply", "102", "100", spost="902", tpost="900", at="2026-10-01T00:00:00Z",
                    text="thanks @Victim_Acct and see https://x.com/victim_acct/status/5"),
        interaction("mention", "h:victim_acct", "100", spost="903", at="2026-10-02T00:00:00Z"),
    ])
    cache.put(request_hash("xai", {"w": 1}), "xai", {
        "items": [{"author_handle": "victim_acct", "type": "mention"}, {"author_handle": "bystander"}],
        "citations": ["https://x.com/victim_acct/status/1850000000000000009"]})
    rep = delete_ids(store, cache, tmp_path / "config", ["101"], "user")
    assert rep.remaining == []
    assert "h:victim_acct" not in {i["source_user_id"] for i in store.interactions()}
    reply = store.interactions()[0]
    assert "victim_acct" not in reply["text"].casefold() and "thanks [deleted]" in reply["text"]
    body = cache.get(request_hash("xai", {"w": 1}))
    assert "victim_acct" not in json.dumps(body).casefold()
    assert body["items"][1]["author_handle"] == "bystander"
    # A word equal to a short handle in ordinary text is not changed.
    assert "bystander" in json.dumps(body)


def test_rekey_user_moves_the_snapshot_finding_and_calibration_rows(tmp_path):
    # A-14 (review 20261103): rows under h:<handle> stayed after the rekey, and a
    # later delete by the X user ID did not find them.
    _, store, cache, _ = make_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    old, new = "h:late_joiner", "104"
    c = store.conn
    c.execute("INSERT INTO snapshot_nodes(snapshot_id,user_id,state) VALUES ('s1',?, 'no_signals')", (old,))
    c.execute("INSERT INTO snapshot_findings VALUES ('s1','lookalike','info',?)", (json.dumps([old, "100"]),))
    c.execute("INSERT INTO findings VALUES ('f1','lookalike','t','info','s',?,?,'r')",
              (json.dumps([old, "100"]), json.dumps({"lookalike": old})))
    c.execute("INSERT INTO calibration_samples VALUES ('c1','b1',?,'x','no_signals','[]','[]',1,'r','t')", (old,))
    c.execute("INSERT INTO profile_history VALUES (?, 't1', 'late_joiner', NULL, NULL, NULL)", (old,))
    store.rekey_user(old, new)
    for t, col in (("snapshot_nodes", "user_id"), ("snapshot_findings", "members_json"), ("findings", "members_json"),
                   ("findings", "evidence_json"), ("calibration_samples", "user_id"), ("profile_history", "user_id")):
        vals = [r[0] for r in c.execute(f"SELECT {col} FROM {t}").fetchall()]
        assert vals and all(old not in v for v in vals) and any(new in v for v in vals), (t, col, vals)
    rep = delete_ids(store, cache, tmp_path / "config", [new], "user")
    assert rep.remaining == []
    assert c.execute("SELECT COUNT(*) FROM snapshot_nodes").fetchone()[0] == 0


def test_log_hashes_other_x_id_keys_and_long_numbers():
    # B-27 (review 20261103): the list of ID keys was a list of exact names.
    log = NullLog()
    log.write("e", quoted_post_id="1850000000000000001", note="1850000000000000002", run_id="run_x",
              reset="1793000000", counts={"seed_ids": ["2002", "2005"]}, page_size=100)
    rec = json.dumps(log.records[0])
    for raw in ("1850000000000000001", "1850000000000000002", '"2002"', '"2005"'):
        assert raw not in rec
    r = log.records[0]
    assert r["run_id"] == "run_x" and r["reset"] == "1793000000" and r["page_size"] == 100
