"""Network detectors (score/network.py), their rules B13, B14, I04, and the profile history (store).

Each detector has a fixture that fires and one or more near-threshold
fixtures that do not (TR-01). The labeled benchmark (tests/validation)
measures the detectors on realistic data.
"""

from __future__ import annotations

import json
from dataclasses import replace

from tests.unit.test_patterns import WORDS, acct, at, days_before, findings, owner_post, run, uid
from tests.unit.test_rules_kit import EGO, ego_profile, interaction, make_store, profile, score
from visualx.cache import Cache
from visualx.delete import delete_ids, find_id
from visualx.score import network as N
from visualx.score import patterns as P

# -- i. co_action -------------------------------------------------------------

POSTS = [f"71000000{k}" for k in range(10)]


def co_fixture(sync_minutes: float, sync_posts: int):
    """40 background accounts reply to 2 posts each at spread times; 3 accounts reply to ``sync_posts`` posts together."""
    profs, items = [], []
    for k in range(40):
        profs.append(acct(k))
        for j in (k % 10, (k + 3) % 10):
            items.append(interaction(src=uid(k), created_at=at(j * 1440 + 30 + (k * 37) % 600), target_post_id=POSTS[j]))
    for m in range(3):
        n = 50 + m
        profs.append(acct(n))
        for j in range(sync_posts):
            items.append(interaction(src=uid(n), created_at=at(j * 1440 + 200 + m * sync_minutes),
                                     target_post_id=POSTS[j], text=f"Reply {m} {j} on {WORDS[j]}"))
    posts = [(POSTS[j], at(j * 1440), 300, 5) for j in range(10)]
    return profs, items, posts


def test_co_action_fires_for_synchronized_accounts_and_gives_b13(tmp_path):
    profs, items, posts = co_fixture(2, 5)
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "co_action")
    assert len(f) == 1
    assert f[0]["severity"] == "notable"
    assert sorted(f[0]["members"]) == [uid(50), uid(51), uid(52)]
    assert f[0]["evidence"]["pairs_tested"] >= 3
    for n in (50, 51, 52):
        assert "B13" in s.fired(uid(n))
        assert "within 10 minutes" in s.hit("B13", uid(n))["cause"]


def test_co_action_is_quiet_when_the_same_posts_get_replies_hours_apart(tmp_path):
    profs, items, posts = co_fixture(180, 5)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "co_action") == []


def test_co_action_is_quiet_for_two_synchronized_posts(tmp_path):
    profs, items, posts = co_fixture(2, 2)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "co_action") == []


# -- k. mass_tagging ----------------------------------------------------------


def tag_items(tagger: int, sets: list[list[int]], endpoint: str = "test") -> list:
    out = []
    for j, ts in enumerate(sets):
        pid = f"72000000{tagger}{j}"
        out.append(interaction(src=uid(tagger), type="mention", post_id=pid, created_at=at(j * 600)))
        for t in ts:
            out.append(interaction(src=uid(tagger), tgt=uid(t), type="mention", post_id=pid, created_at=at(j * 600)))
    if endpoint != "test":
        out = [replace(i, source_endpoint=endpoint) for i in out]
    return out


def test_mass_tagging_fires_for_new_unlinked_targets_and_gives_b14(tmp_path):
    profs = [acct(k) for k in range(20)]
    items = tag_items(0, [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14, 15]])
    s = run(tmp_path, profs, items)
    f = findings(s, "mass_tagging")
    assert len(f) == 1 and f[0]["members"] == [uid(0)]
    assert f[0]["evidence"]["tagged"] == 15 and f[0]["evidence"]["retag_share"] == 0.0
    assert "B14" in s.fired(uid(0))


def test_mass_tagging_is_quiet_when_the_same_accounts_are_tagged_again(tmp_path):
    profs = [acct(k) for k in range(20)]
    items = tag_items(0, [[1, 2, 3, 4, 5], [1, 2, 3, 4, 6], [1, 2, 3, 5, 6], [2, 3, 4, 5, 7]])
    s = run(tmp_path, profs, items)
    assert findings(s, "mass_tagging") == []


def test_mass_tagging_is_quiet_when_the_tagged_accounts_know_each_other(tmp_path):
    profs = [acct(k) for k in range(20)]
    items = tag_items(0, [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14, 15]])
    for a in range(1, 16):
        items.append(interaction(src=uid(a), tgt=uid(a % 15 + 1), type="mention", created_at=at(5000 + a)))
    s = run(tmp_path, profs, items)
    assert findings(s, "mass_tagging") == []


def test_mass_tagging_leaves_out_seed_accounts(tmp_path):
    profs = [acct(k) for k in range(20)]
    items = tag_items(0, [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14, 15]], endpoint="/2/users/:id/tweets")
    s = run(tmp_path, profs, items)
    assert findings(s, "mass_tagging") == []


# -- j. profile_change --------------------------------------------------------


def xprof(user_id: str, fetched: str, **over):
    return replace(profile(user_id, **over), adapter="xapi", fetched_at=fetched)


def history_run(tmp_path, versions, allowlist=None, extra=()):
    st = make_store(tmp_path)
    for v in versions[:-1]:
        st.put_profiles([v])
    return score(tmp_path, profiles=[ego_profile(), *extra, versions[-1]],
                 interactions=[interaction(src=versions[-1].user_id)], store=st, allowlist=allowlist)


OLD = "2010-05-01T00:00:00.000Z"


def test_rename_to_the_owner_name_is_high_and_gives_i04(tmp_path):
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle="harborwalnut", name="Harbor Walnut", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="RonDilIey", name="Ron Dilley", created_at=OLD)]
    s = history_run(tmp_path, vs)
    f = findings(s, "profile_change")
    assert len(f) == 1 and f[0]["severity"] == "high"
    assert f[0]["members"] == ["201", EGO]
    assert f[0]["evidence"]["kind"] == "toward_known" and f[0]["evidence"]["reference_role"] == "owner"
    assert "I04" in s.fired("201")
    assert "@rondilley" in s.hit("I04", "201")["cause"]


def test_rename_toward_an_allowlist_account_is_high_although_the_account_is_old(tmp_path):
    known = profile("250", handle="maple_comms", name="Maple Comms", created_at="2018-01-01T00:00:00.000Z")
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle="quartzfern", name="Quartz Fern", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="maple_comms1", name="Maple Comms", created_at=OLD)]
    s = history_run(tmp_path, vs, allowlist=["250"], extra=[known])
    f = findings(s, "profile_change")
    assert len(f) == 1 and f[0]["severity"] == "high"
    assert "I04" in s.fired("201")
    # The lookalike detector grades this pair down: the account is older than the reference.
    look = [x for x in findings(s, "lookalike") if x["members"][0] == "201"]
    assert all(x["severity"] != "high" for x in look)


def test_unrelated_rename_is_info_and_gives_no_rule(tmp_path):
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle="harborwalnut", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="glaciertrellis", created_at=OLD)]
    s = history_run(tmp_path, vs)
    f = findings(s, "profile_change")
    assert [x["severity"] for x in f] == ["info"]
    assert "I04" not in s.fired("201")


def test_display_name_change_alone_is_not_a_finding(tmp_path):
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", name="Jane Doe", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", name="Jane Doe | DFIR", created_at=OLD)]
    s = history_run(tmp_path, vs)
    assert findings(s, "profile_change") == []


def test_handle_churn_is_notable(tmp_path):
    vs = [xprof("201", f"2026-0{m}-01T00:00:00.000Z", handle=h, created_at=OLD)
          for m, h in ((6, "harborwalnut"), (7, "cobaltmeadow"), (8, "fjordsequoia"))]
    vs.append(xprof("201", "2026-11-01T00:00:00.000Z", handle="umberprairie", created_at=OLD))
    s = history_run(tmp_path, vs)
    f = findings(s, "profile_change")
    assert len(f) == 1 and f[0]["severity"] == "notable" and f[0]["evidence"]["handles"] == 4
    assert "I04" not in s.fired("201")


# -- profile history in the store ---------------------------------------------


def test_store_records_a_history_row_only_when_a_tracked_field_changes(tmp_path):
    st = make_store(tmp_path)
    st.put_profiles([xprof("201", "2026-09-01T00:00:00.000Z", handle="a_one")])
    st.put_profiles([xprof("201", "2026-09-05T00:00:00.000Z", handle="a_one", followers_count=999)])
    st.put_profiles([xprof("201", "2026-09-09T00:00:00.000Z", handle="a_two")])
    st.put_profiles([replace(profile("202", handle="g_one"), fetched_at="2026-09-01T00:00:00.000Z", adapter="grok_x_search")])
    h = st.profile_history()
    assert [r["handle"] for r in h["201"]] == ["a_one", "a_two"]
    assert "202" not in h


def test_store_keeps_the_last_value_of_a_field_that_a_fetch_does_not_give(tmp_path):
    st = make_store(tmp_path)
    st.put_profiles([xprof("201", "2026-09-01T00:00:00.000Z", handle="a_one", url="https://a.example")])
    st.put_profiles([xprof("201", "2026-09-05T00:00:00.000Z", handle="a_one", url=None)])
    assert len(st.profile_history()["201"]) == 1


def test_first_change_after_an_upgrade_keeps_the_stored_profile_as_the_first_row(tmp_path):
    st = make_store(tmp_path)
    st.put_profiles([xprof("201", "2026-09-01T00:00:00.000Z", handle="a_one")])
    st.conn.execute("DELETE FROM profile_history")
    st.put_profiles([xprof("201", "2026-09-09T00:00:00.000Z", handle="a_two")])
    assert [r["handle"] for r in st.profile_history()["201"]] == ["a_one", "a_two"]


def test_deletion_removes_the_profile_history(tmp_path):
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle="harborwalnut", name="Harbor Walnut", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="RonDilIey", name="Ron Dilley", created_at=OLD)]
    s = history_run(tmp_path, vs)
    assert findings(s, "profile_change")
    cache = Cache(s.store, tmp_path / "cache")
    delete_ids(s.store, cache, tmp_path / "config", ["201"], "user")
    assert s.store.profile_history() == {}
    assert findings(s, "profile_change") == []
    assert find_id(s.store, cache, "201") == []


# -- l. community_anomaly -----------------------------------------------------


def community_fixture(batch: bool):
    """6 linked groups of 10 old accounts, and one linked group of 10 that is a batch (or has spread dates)."""
    profs, items = [], []
    n = 0
    for g in range(7):
        members = list(range(n, n + 10))
        n += 10
        for k, m in enumerate(members):
            if g == 6:
                created = days_before(50 + k * 0.7) if batch else days_before(200 + k * 160)
            else:
                created = days_before(400 + ((m * 397) % 5000))
            profs.append(acct(m, created_at=created))
        for k, m in enumerate(members):
            for d in (1, 2, 3):
                items.append(interaction(src=uid(m), tgt=uid(members[(k + d) % 10]), type="mention",
                                         created_at=at(m * 10 + d)))
    return profs, items


def test_community_of_accounts_made_in_one_batch_is_notable(tmp_path):
    profs, items = community_fixture(batch=True)
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "community_anomaly") if x["severity"] == "notable"]
    assert len(f) == 1
    assert sorted(f[0]["members"]) == [uid(m) for m in range(60, 70)]
    assert f[0]["evidence"]["kind"] == "creation" and f[0]["evidence"]["community_size"] == 10


def test_community_with_spread_creation_dates_is_not_notable(tmp_path):
    profs, items = community_fixture(batch=False)
    s = run(tmp_path, profs, items)
    assert [x for x in findings(s, "community_anomaly") if x["severity"] == "notable"] == []


def test_communities_are_the_same_on_each_run(tmp_path):
    profs, items = community_fixture(batch=True)
    st = make_store(tmp_path)
    st.put_profiles([ego_profile(), *profs])
    st.put_interactions(items)
    d = P.load_pattern_data(st, frozenset())
    assert N.communities(d, 7) == N.communities(d, 7)
    assert all(EGO not in c for c in N.communities(d, 7))


# -- policy -------------------------------------------------------------------


def test_network_detectors_read_no_sensitive_field_and_have_defaults_in_the_rule_file():
    import yaml

    from tests.unit.test_rules_kit import RULES_PATH

    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    for name in P.NETWORK_DEFAULTS:
        assert name in P.DETECTOR_INPUTS
        assert set(data["patterns"]["detectors"][name]) <= set(P.NETWORK_DEFAULTS[name]) | {"enabled"}
        for f in P.DETECTOR_INPUTS[name]:
            assert not any(w in f for w in ("location", "lang", "description", "country")), (name, f)


def test_network_finding_text_has_no_handle(tmp_path):
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle="harborwalnut", name="Harbor Walnut", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="RonDilIey", name="Ron Dilley", created_at=OLD)]
    s = history_run(tmp_path, vs)
    for f in findings(s):
        text = (f["title"] + " " + f["summary"]).casefold()
        assert "rondil" not in text and "harborwalnut" not in text
        json.loads(f["evidence_json"])


# -- P3 items of review 20261103 ----------------------------------------------


def test_profile_change_needs_the_earlier_handle(tmp_path):
    # B-17: a history row with no handle is not "did not look like the owner".
    vs = [xprof("201", "2026-09-01T00:00:00.000Z", handle=None, name="Harbor Walnut", created_at=OLD),
          xprof("201", "2026-11-01T00:00:00.000Z", handle="RonDilIey", name="Ron Dilley", created_at=OLD)]
    s = history_run(tmp_path, vs)
    assert not [f for f in findings(s, "profile_change") if f["evidence"]["kind"] == "toward_known"]
    assert "I04" not in s.fired("201")


def test_co_action_with_no_impression_counts_uses_the_pooled_latencies(tmp_path):
    # B-18: a missing impression count was read as 0 (the low-reach band).
    profs, items, _posts = co_fixture(2, 5)
    st = make_store(tmp_path)
    for j in range(10):
        st.put_post({"id": POSTS[j], "author_id": EGO, "created_at": at(j * 1440),
                     "public_metrics": {"reply_count": 5}}, "test", "run_test")
    s = score(tmp_path, profiles=[ego_profile(), *profs], interactions=items, store=st)
    f = findings(s, "co_action")
    assert len(f) == 1 and sorted(f[0]["members"]) == [uid(50), uid(51), uid(52)]
