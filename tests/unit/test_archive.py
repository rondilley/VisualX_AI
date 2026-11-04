"""X data archive importer (FR-06, IR-04, TSC-02) with hand-made archives (zip and folder). No network."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers import make_env, profile
from visualx.build import run_build
from visualx.collect.archive import ArchiveError, archive_time, classify, run_import
from visualx.collect.xapi import map_post
from visualx.model import dumps

OWNER = "1000"
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%a %b %d %H:%M:%S +0000 %Y")


def tw(id_: str, text: str, days_ago: float = 3, mentions=(), urls=(), reply_user=None, reply_post=None) -> dict:
    t = {
        "id_str": id_, "id": id_, "created_at": at(days_ago), "full_text": text,
        "entities": {
            "user_mentions": [{"id_str": u, "id": u, "screen_name": h, "name": h} for u, h in mentions],
            "urls": [{"url": "https://t.co/abc", "expanded_url": u, "display_url": u} for u in urls],
            "hashtags": [],
        },
    }
    if reply_user is not None:
        t["in_reply_to_user_id_str"] = reply_user
        t["in_reply_to_status_id_str"] = reply_post
    return {"tweet": t}


TWEETS = [
    tw("2001", "@alice @bob hello", mentions=[("3001", "alice"), ("3002", "bob")], reply_user="3001", reply_post="2500"),
    tw("2002", "RT @carol: a reposted post", mentions=[("3003", "carol")]),
    tw("2003", "RT @dave: no ID for this handle"),
    tw("2004", "look at this https://t.co/abc", urls=["https://twitter.com/Erin/status/2600"]),
    tw("2005", "and this https://t.co/abc", urls=["https://x.com/frank/status/2700"]),
    tw("2006", "a thread of my own", reply_user=OWNER, reply_post="2001"),
    tw("1999", "an old post @gina", days_ago=400, mentions=[("3009", "gina")]),
    tw("2008", "my own post again https://t.co/abc", urls=["https://twitter.com/Ron_Dilley/status/2001"]),
]
PART1 = [tw("2007", "hi @hank", mentions=[("3007", "hank")])]


def js(name: str, items: list) -> str:
    return f"window.YTD.{name}.part0 = " + json.dumps(items, indent=2)


def archive_files(account_id: str = OWNER, tweets_text: str | None = None) -> dict[str, str]:
    return {
        "data/account.js": js("account", [{"account": {"accountId": account_id, "username": "Ron_Dilley",
                                                       "accountDisplayName": "Ron", "createdAt": "2009-01-01T00:00:00.000Z"}}]),
        "data/tweets.js": tweets_text if tweets_text is not None else js("tweets", TWEETS),
        "data/tweets-part1.js": js("tweets", PART1).replace("part0", "part1"),
        "data/like.js": js("like", [{"like": {"tweetId": "1"}}]),
        "data/follower.js": js("follower", [{"follower": {"accountId": "5"}}]),
        "data/direct-messages.js": js("direct_messages", []),
        "data/strange-new.js": js("strange_new", []),
        "data/tweets_media/2004-a.jpg": "binary",
        "Your archive.html": "<html></html>",
        "notes.txt": "x",
    }


def write_zip(path: Path, files: dict[str, str], top: str = "twitter-2026-11-02-abc/") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(top + name, body)
    return path


def write_dir(path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return path


def env(tmp_path: Path, ego: str | None = OWNER):
    cfg, store, cache, log = make_env(tmp_path)
    if ego is not None:
        store.set_meta("ego_user_id", ego)
    store.put_profiles([profile("3005", handle="frank")])
    return cfg, store, log


def edges(store) -> set[tuple[str, str, str, str | None]]:
    return {(r["type"], r["source_user_id"], r["target_user_id"], r["target_post_id"]) for r in store.interactions()}


EXPECTED = {
    ("reply", OWNER, "3001", "2500"),
    ("mention", OWNER, "3002", None),
    ("repost", OWNER, "3003", None),
    ("quote", OWNER, "h:erin", "2600"),
    ("quote", OWNER, "3005", "2700"),
    ("mention", OWNER, "3007", None),
}


def test_zip_import_maps_each_type(tmp_path):
    cfg, store, log = env(tmp_path)
    z = write_zip(tmp_path / "archive.zip", archive_files())
    rep = run_import(cfg, store, log, z, "run_a", now=NOW)
    assert edges(store) == EXPECTED
    assert rep.mapped == {"reply": 1, "quote": 2, "mention": 2, "repost": 1}
    assert rep.unmapped["repost_target_unknown"] == 1
    assert rep.outside_window == 1  # the 400-day-old post, with the 90-day default window
    assert rep.posts_without_events == 2  # the self-reply and the self-quote
    assert rep.provisional_quote_targets == 1
    assert rep.interactions_added == 6
    # The provisional quote target has a handle-only profile, as from the Grok source.
    prov = store.profiles()["h:erin"]
    assert prov["handle"] == "erin" and prov["adapter"] == "archive"
    assert prov["field_status"]["created_at"] == "absent"


def test_fields_link_to_the_archive_record(tmp_path):
    cfg, store, log = env(tmp_path)
    run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    rows = {(r["type"], r["target_user_id"]): r for r in store.interactions()}
    reply = rows[("reply", "3001")]
    assert reply["source_post_id"] == "2001"
    assert reply["source_endpoint"] == "archive:data/tweets.js"
    assert reply["adapter"] == "archive"
    assert reply["raw_request_hash"] == hashlib.sha256(dumps(TWEETS[0]).encode("utf-8")).hexdigest()
    assert reply["interaction_created_at"] == (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert reply["text"] == "@alice @bob hello"
    fs = reply["field_status"]
    assert fs["conversation_id"] == "absent" and fs["text"] == "present" and fs["target_post_id"] == "present"
    assert rows[("repost", "3003")]["field_status"]["target_post_id"] == "absent"
    part = rows[("mention", "3007")]
    assert part["source_endpoint"] == "archive:data/tweets-part1.js"


def test_folder_import_gives_the_same_events(tmp_path):
    cfg, store, log = env(tmp_path)
    d = write_dir(tmp_path / "extracted", archive_files())
    rep = run_import(cfg, store, log, d, "run_a", now=NOW)
    assert edges(store) == EXPECTED
    assert "data/tweets-part1.js" in rep.files_used


def test_data_folder_path_is_accepted(tmp_path):
    cfg, store, log = env(tmp_path)
    d = write_dir(tmp_path / "extracted", archive_files())
    run_import(cfg, store, log, d / "data", "run_a", now=NOW)
    assert edges(store) == EXPECTED


def test_report_names_each_file_it_does_not_recognize(tmp_path):
    cfg, store, log = env(tmp_path)
    rep = run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW, dry_run=True)
    assert rep.files_unrecognized == ["data/strange-new.js", "notes.txt"]
    assert rep.files_known_unused == ["data/direct-messages.js", "data/follower.js", "data/like.js"]
    assert rep.files_used == ["data/account.js", "data/tweets.js", "data/tweets-part1.js"]
    assert rep.media_files == 1 and rep.viewer_files == 1
    text = "\n".join(rep.summary_lines())
    assert "data/strange-new.js" in text and "notes.txt" in text
    assert "files NOT recognized (2)" in text


def test_classify_names():
    assert classify("data/tweet.js") == "tweets"
    assert classify("data/tweets-part12.js") == "tweets"
    assert classify("data/like-part1.js") == "known"
    assert classify("data/direct-messages-group.js") == "known"
    assert classify("data/account.js") == "account"
    assert classify("data/new-thing.js") == "unknown"
    assert classify("data/direct_messages_media/x.jpg") == "media"


def test_archive_time_is_the_store_form():
    assert archive_time("Wed Oct 10 20:19:24 +0000 2018") == "2018-10-10T20:19:24.000Z"
    assert archive_time("Wed Oct 10 22:19:24 +0200 2018") == "2018-10-10T20:19:24.000Z"
    assert archive_time("not a time") is None
    assert archive_time(None) is None


def test_owner_check_stops_for_another_account(tmp_path):
    cfg, store, log = env(tmp_path)
    z = write_zip(tmp_path / "a.zip", archive_files(account_id="4242"))
    with pytest.raises(ArchiveError, match="different account"):
        run_import(cfg, store, log, z, "run_a", now=NOW)
    assert store.interactions() == []
    assert store.latest_stage_run("collect") is None


def test_owner_check_needs_a_known_owner(tmp_path):
    cfg, store, log = env(tmp_path, ego=None)
    with pytest.raises(ArchiveError, match="owner account ID"):
        run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    store.set_meta("ego_user_id", "h:ron_dilley")
    with pytest.raises(ArchiveError, match="provisional owner ID"):
        run_import(cfg, store, log, write_zip(tmp_path / "b.zip", archive_files()), "run_a", now=NOW)
    assert store.interactions() == []


def test_broken_prefix_stops_with_no_write(tmp_path):
    cfg, store, log = env(tmp_path)
    bad = "var tweets = " + json.dumps(TWEETS)
    with pytest.raises(ArchiveError, match="does not start with"):
        run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files(tweets_text=bad)), "run_a", now=NOW)
    broken_json = "window.YTD.tweets.part0 = [ {"
    with pytest.raises(ArchiveError, match="JSON that cannot be read"):
        run_import(cfg, store, log, write_zip(tmp_path / "b.zip", archive_files(tweets_text=broken_json)), "run_a", now=NOW)
    code = "window.YTD.tweets.part0 = alert(1)"
    with pytest.raises(ArchiveError, match="JSON that cannot be read"):
        run_import(cfg, store, log, write_zip(tmp_path / "c.zip", archive_files(tweets_text=code)), "run_a", now=NOW)
    assert store.interactions() == []
    assert store.q("SELECT COUNT(*) FROM stage_runs")[0][0] == 0


@pytest.mark.parametrize("member", ["../evil.js", "data/../../evil.js", "/abs/data/x.js", "C:/x/data.js", "a\\..\\b.js"])
def test_zip_with_an_unsafe_member_is_refused(tmp_path, member):
    cfg, store, log = env(tmp_path)
    z = tmp_path / "a.zip"
    write_zip(z, archive_files(), top="")
    with zipfile.ZipFile(z, "a") as zf:
        zf.writestr(zipfile.ZipInfo(member), "x")
    with pytest.raises(ArchiveError, match="unsafe member name"):
        run_import(cfg, store, log, z, "run_a", now=NOW)
    assert store.interactions() == []


def test_not_an_archive(tmp_path):
    cfg, store, log = env(tmp_path)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ArchiveError, match="no data/account.js"):
        run_import(cfg, store, log, tmp_path / "empty", "run_a", now=NOW)
    (tmp_path / "x.zip").write_text("not a zip", encoding="utf-8")
    with pytest.raises(ArchiveError, match="not a readable zip"):
        run_import(cfg, store, log, tmp_path / "x.zip", "run_a", now=NOW)


def test_archive_and_xapi_events_dedupe(tmp_path):
    cfg, store, log = env(tmp_path)
    # The same reply from the X API v2 (post 2001), stored first.
    post = {"id": "2001", "author_id": OWNER, "text": "@alice @bob hello", "in_reply_to_user_id": "3001",
            "created_at": "2026-10-30T12:00:00.000Z", "conversation_id": "2500",
            "referenced_tweets": [{"type": "replied_to", "id": "2500"}],
            "entities": {"mentions": [{"id": "3001", "username": "alice"}, {"id": "3002", "username": "bob"}]}}
    api = map_post(post, OWNER, "/2/users/:id/tweets", "run_x")
    assert store.put_interactions(api) == 2
    rep = run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    assert rep.already_in_store == 2
    assert rep.interactions_added == 4
    assert len(store.interactions()) == 6
    kept = {r["interaction_id"]: r for r in store.interactions()}
    assert {i.interaction_id for i in api} <= set(kept)
    assert all(kept[i.interaction_id]["adapter"] == "xapi" for i in api)


def test_repost_dedupes_with_xapi(tmp_path):
    cfg, store, log = env(tmp_path)
    post = {"id": "2002", "author_id": OWNER, "text": "RT @carol: a reposted post",
            "referenced_tweets": [{"type": "retweeted", "id": "2900"}]}
    api = map_post(post, OWNER, "/2/users/:id/tweets", "run_x", includes={"tweets": [{"id": "2900", "author_id": "3003"}]})
    store.put_interactions(api)
    run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    assert [r["adapter"] for r in store.interactions() if r["type"] == "repost"] == ["xapi"]


def test_dry_run_writes_nothing(tmp_path):
    cfg, store, log = env(tmp_path)
    before = store.profiles()
    rep = run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW, dry_run=True)
    assert rep.events == 6
    assert store.interactions() == []
    assert store.profiles() == before
    assert store.q("SELECT COUNT(*) FROM stage_runs")[0][0] == 0
    assert "dry run, nothing written" in rep.summary_lines()[0]


def test_stage_record_is_fresh_input_for_build(tmp_path):
    cfg, store, log = env(tmp_path)
    run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    row = store.latest_stage_run("collect")
    counts = json.loads(row["counts_json"])
    assert counts["source"] == "archive" and counts["complete"] is True and counts["cost_usd"] == 0.0
    assert counts["window_days"] == 90 and counts["interactions_added"] == 6
    # The provisional quote target merges into the X user ID when an X profile with the handle is there.
    store.put_profiles([profile("3004", handle="Erin")])
    out = run_build(cfg, store, log, "run_b")
    assert out["interactions"] == 6
    assert ("quote", OWNER, "3004", "2600") in edges(store)
    assert not any(r["target_user_id"].startswith("h:") for r in store.interactions())


def test_days_zero_imports_all_posts(tmp_path):
    cfg, store, log = env(tmp_path)
    rep = run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW, days=0)
    assert rep.outside_window == 0
    assert ("mention", OWNER, "3009", None) in edges(store)
    assert "window_start" not in json.loads(store.latest_stage_run("collect")["counts_json"])


def test_retain_text_false_drops_text(tmp_path):
    cfg, store, log = env(tmp_path)
    cfg.raw["data"]["retain_text"] = False
    rep = run_import(cfg, store, log, write_zip(tmp_path / "a.zip", archive_files()), "run_a", now=NOW)
    assert rep.text_dropped == 6
    assert all(r["text"] is None and r["field_status"]["text"] == "absent" for r in store.interactions())


def test_old_archive_without_tweet_wrapper(tmp_path):
    cfg, store, log = env(tmp_path)
    files = archive_files()
    del files["data/tweets.js"], files["data/tweets-part1.js"]
    files["data/tweet.js"] = js("tweet", [TWEETS[0]["tweet"]])
    rep = run_import(cfg, store, log, write_dir(tmp_path / "old", files), "run_a", now=NOW)
    assert rep.files_used == ["data/account.js", "data/tweet.js"]
    assert edges(store) == {("reply", OWNER, "3001", "2500"), ("mention", OWNER, "3002", None)}


def test_parse_js_accepts_a_trailing_semicolon():
    # A-16 (review 20261103).
    from visualx.collect.archive import parse_js

    assert parse_js(b"window.YTD.tweets.part0 = [1, 2];\n", "data/tweets.js") == [1, 2]
    assert parse_js(b"window.YTD.tweets.part0 = [3]", "data/tweets.js") == [3]
    # A-25: a file with no ".partN" in the prefix.
    assert parse_js(b"window.YTD.tweets = [4]", "data/tweets.js") == [4]


def test_post_files_larger_than_the_total_limit_are_refused(tmp_path, monkeypatch):
    # A-15 (review 20261103): all post files are in memory at the same time.
    from visualx.collect import archive as A

    monkeypatch.setattr(A, "MAX_POST_BYTES", 10)
    cfg, store, log = env(tmp_path)
    z = write_zip(tmp_path / "archive.zip", archive_files())
    with pytest.raises(ArchiveError, match="more than the limit"):
        run_import(cfg, store, log, z, "run_big", now=NOW)
    assert store.interactions() == []
