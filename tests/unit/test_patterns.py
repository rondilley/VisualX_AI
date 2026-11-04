"""Network-level pattern detectors (score/patterns.py) and their rules B02, B09 to B12.

Each detector has a fixture that fires and one or more that do not (TR-01).
The fixtures are synthetic but follow the live data shape: interactions to the
owner, owner posts with public metrics, and profiles with all gate fields.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta

import pytest
import yaml

from tests.unit.test_rules_kit import (
    EGO,
    RULES_PATH,
    Scored,
    ego_profile,
    interaction,
    make_store,
    profile,
    score,
)
from visualx.score import patterns as P
from visualx.score.engine import FORBIDDEN_WORDS, RuleFileError, ruleset_from_data, sensitive_hits

T0 = datetime(2026, 10, 1, 12, 0, 0)
WORDS = ["maple", "harbor", "quartz", "falcon", "juniper", "tundra", "copper", "violet", "saffron", "glacier",
         "orchid", "pebble", "lantern", "meadow", "cobalt", "thistle", "walnut", "ember", "fjord", "indigo",
         "magnet", "nectar", "oasis", "prairie", "quiver", "rhubarb", "sequoia", "trellis", "umber", "vortex"]


def at(minutes: float = 0, base: datetime = T0) -> str:
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def days_before(days: float, base: datetime = T0) -> str:
    return (base - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def acct(n: int, **over):
    """Account 3xx with a distinct handle and an old, established profile unless changed."""
    uid = str(300 + n)
    over.setdefault("handle", WORDS[n % len(WORDS)] + "_" + WORDS[(n * 7 + 3) % len(WORDS)])
    over.setdefault("name", f"Reader {WORDS[n % len(WORDS)].title()}")
    return profile(uid, **over)


def uid(n: int) -> str:
    return str(300 + n)


def owner_post(store, post_id: str, created: str, impressions: int = 100, replies: int = 1, reply_to: str | None = None):
    store.put_post(
        {"id": post_id, "author_id": EGO, "created_at": created,
         "public_metrics": {"impression_count": impressions, "reply_count": replies},
         "referenced_tweets": [{"id": reply_to, "type": "replied_to"}] if reply_to else None},
        "test", "run_test",
    )


def run(tmp_path, profiles, items, posts=(), allowlist=None, rules=None) -> Scored:
    st = make_store(tmp_path)
    for p in posts:
        owner_post(st, *p)
    from visualx.score.engine import ruleset_from_data as rfd

    rs = rfd(rules) if rules is not None else None
    return score(tmp_path, profiles=[ego_profile(), *profiles], interactions=items, store=st,
                 allowlist=allowlist, ruleset=rs)


def findings(s: Scored, detector: str | None = None) -> list[dict]:
    rows = [dict(r) for r in s.store.q("SELECT * FROM findings ORDER BY finding_id")]
    for r in rows:
        r["members"] = json.loads(r["members_json"])
        r["evidence"] = json.loads(r["evidence_json"])
    return [r for r in rows if detector is None or r["detector"] == detector]


def rules_data() -> dict:
    with RULES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def with_detector(**changes) -> dict:
    data = copy.deepcopy(rules_data())
    for det, conf in changes.items():
        data["patterns"]["detectors"].setdefault(det, {}).update(conf)
    return data


# -- a. coordinated burst -----------------------------------------------------

POST = "700000001"


def burst_fixture(n_accounts: int, spacing_minutes: float, young: bool):
    # Young accounts are 20 to 20 + 15 * n days old: young, but not a creation cluster.
    profs = [acct(k, created_at=days_before(20 + 15 * k) if young else f"201{k % 10}-01-01T00:00:00.000Z")
             for k in range(n_accounts)]
    items = [interaction(src=uid(k), tgt=EGO, created_at=at(5 + k * spacing_minutes), target_post_id=POST,
                         text=f"Reply number {k} about {WORDS[k]}") for k in range(n_accounts)]
    return profs, items, [(POST, at(0), 120, n_accounts)]


def test_burst_of_young_accounts_fires_and_gives_b09(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=True)
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "reply_burst")
    assert len(f) == 1
    assert f[0]["severity"] == "notable"
    assert sorted(f[0]["members"]) == [uid(k) for k in range(4)]
    assert f[0]["evidence"]["anchor_post_id"] == POST
    assert f[0]["evidence"]["young_accounts"] == 4
    for k in range(4):
        assert "B09" in s.fired(uid(k))
        assert s.hit("B09", uid(k))["cause"] == "One of 4 accounts that contacted you within 6 minutes"
        assert s.hit("B09", uid(k))["family"] == "behavior"


def test_burst_of_established_accounts_is_info_and_gives_no_rule(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=False)
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "reply_burst")
    assert len(f) == 1 and f[0]["severity"] == "info"
    assert not any("B09" in s.fired(uid(k)) for k in range(4))


def test_three_accounts_are_not_a_burst(tmp_path):
    profs, items, posts = burst_fixture(3, 2, young=True)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "reply_burst") == []


def test_replies_spread_over_hours_are_not_a_burst(tmp_path):
    profs, items, posts = burst_fixture(5, 40, young=True)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "reply_burst") == []


def test_burst_without_times_gives_no_finding_and_b09_has_no_input(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=True)
    items = [interaction(src=i.source_user_id, tgt=EGO, created_at=None, target_post_id=POST) for i in items]
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "reply_burst") == []
    assert s.skipped(uid(0))["B09"] == {"interaction_created_at"}


# -- b. account-creation cluster ---------------------------------------------


def test_creation_cluster_fires_and_gives_b10(tmp_path):
    base = T0 - timedelta(days=40)
    profs = [acct(k, created_at=at(k * 600, base)) for k in range(3)]  # within 20 hours
    profs += [acct(k, created_at=f"20{10 + k}-05-01T00:00:00.000Z") for k in range(3, 8)]
    items = [interaction(src=uid(k), created_at=at(k * 300)) for k in range(8)]
    s = run(tmp_path, profs, items)
    f = findings(s, "creation_cluster")
    assert len(f) == 1
    assert sorted(f[0]["members"]) == [uid(k) for k in range(3)]
    assert f[0]["severity"] == "high"  # all younger than 90 days
    assert f[0]["evidence"]["p_adjusted"] <= 0.05
    assert s.hit("B10", uid(0))["cause"] == "One of 3 accounts created within 20 hours of each other"
    assert "B10" not in s.fired(uid(5))


def test_accounts_created_months_apart_are_not_a_cluster(tmp_path):
    profs = [acct(k, created_at=days_before(40 + 90 * k)) for k in range(5)]
    items = [interaction(src=uid(k), created_at=at(k)) for k in range(5)]
    s = run(tmp_path, profs, items)
    assert findings(s, "creation_cluster") == []


def test_old_sign_up_wave_is_not_a_cluster(tmp_path):
    # Three accounts made in the same days of 2009: outside the look-back period.
    profs = [acct(k, created_at=f"2009-03-0{k + 1}T00:00:00.000Z") for k in range(3)]
    items = [interaction(src=uid(k), created_at=at(k)) for k in range(3)]
    s = run(tmp_path, profs, items)
    assert findings(s, "creation_cluster") == []


def test_cluster_needs_more_members_when_many_accounts_are_recent():
    # The Poisson test with a Bonferroni factor: 3 in 72 h is a surprise among
    # 30 recent accounts, and not among 100.
    span_h, w = 1095 * 24.0, 72.0
    windows = span_h / w
    assert P._poisson_tail(3, 30 * w / span_h) * windows <= 0.05
    assert P._poisson_tail(3, 100 * w / span_h) * windows > 0.05
    assert P._poisson_tail(5, 100 * w / span_h) * windows <= 0.05


# -- c. fast-reply timing -----------------------------------------------------


def fast_fixture(latency_s: float, n: int, reply_posts: bool = False):
    posts, items = [], []
    for k in range(n):
        pid = f"70000010{k}"
        created = at(k * 600)
        posts.append((pid, created, 50, 1, "123" if reply_posts else None))
        t = (T0 + timedelta(minutes=k * 600, seconds=latency_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        items.append(interaction(src=uid(0), created_at=t, target_post_id=pid, text=f"answer {k} {WORDS[k]}"))
    return [acct(0)], items, posts


def test_fast_replies_fire_and_give_b11(tmp_path):
    profs, items, posts = fast_fixture(20, 3)
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "fast_reply")
    assert len(f) == 1
    assert f[0]["members"] == [uid(0)] and f[0]["severity"] == "notable"
    assert f[0]["evidence"]["median_seconds"] == 20
    assert s.hit("B11", uid(0))["cause"] == "Replies a median of 20 seconds after your post (3 replies)"


def test_very_fast_replies_are_high(tmp_path):
    profs, items, posts = fast_fixture(4, 4)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "fast_reply")[0]["severity"] == "high"


@pytest.mark.parametrize("latency, n, reply_posts", [(300, 3, False), (20, 2, False), (20, 3, True)])
def test_fast_reply_does_not_fire(tmp_path, latency, n, reply_posts):
    # Slow replies; only two replies; replies to the owner's replies (a live conversation).
    profs, items, posts = fast_fixture(latency, n, reply_posts)
    s = run(tmp_path, profs, items, posts)
    assert findings(s, "fast_reply") == []
    assert "B11" not in s.fired(uid(0))


def test_fast_reply_without_target_post_has_no_input(tmp_path):
    profs, items, posts = fast_fixture(20, 3)
    items = [interaction(src=uid(0), created_at=at(k), unreadable=("target_post_id",)) for k in range(3)]
    s = run(tmp_path, profs, items, posts)
    assert s.skipped(uid(0))["B11"] == {"target_post_id"}


# -- d. shared text and links (B02, B12) --------------------------------------

COPY = "This is the exact same message that several accounts sent to you today"


def test_same_text_from_three_accounts_fires_b02(tmp_path):
    profs = [acct(k) for k in range(4)]
    items = [interaction(src=uid(k), text=f"@someone {COPY.upper() if k else COPY}!", created_at=at(k)) for k in range(3)]
    items.append(interaction(src=uid(3), text="A different and quite original reply about graphs", created_at=at(9)))
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "text"]
    assert len(f) == 1 and sorted(f[0]["members"]) == [uid(0), uid(1), uid(2)]
    assert "text_hash" in f[0]["evidence"] and COPY not in f[0]["evidence_json"]
    assert s.hit("B02", uid(1))["cause"] == "Same text as 2 other accounts"
    assert s.hit("B02", uid(1))["points"] == 3
    assert "B02" not in s.fired(uid(3))


def test_same_link_from_three_accounts_fires_b02(tmp_path):
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"look {WORDS[k]}", urls=[f"https://www.promo-site.example/offer?utm_source={k}"],
                         created_at=at(k)) for k in range(3)]
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "link"]
    assert len(f) == 1
    assert f[0]["evidence"]["link"] == "promo-site.example/offer"
    assert s.hit("B02", uid(0))["cause"] == "Same link as 2 other accounts"
    # The same three accounts get no second (domain) finding for the same site.
    assert not [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "domain"]


def test_short_common_text_does_not_group(tmp_path):
    profs = [acct(k) for k in range(4)]
    items = [interaction(src=uid(k), text="Great post, thanks!", created_at=at(k)) for k in range(4)]
    s = run(tmp_path, profs, items)
    assert findings(s, "shared_content") == []
    assert not any("B02" in s.fired(uid(k)) for k in range(4))


def test_two_accounts_with_same_text_do_not_group(tmp_path):
    profs = [acct(k) for k in range(2)]
    items = [interaction(src=uid(k), text=COPY, created_at=at(k)) for k in range(2)]
    s = run(tmp_path, profs, items)
    assert findings(s, "shared_content") == []


def test_same_site_in_a_short_time_fires_b12(tmp_path):
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"see {WORDS[k]}", urls=[f"https://deals.example/p/{k}"],
                         created_at=at(k * 600)) for k in range(3)]
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "domain"]
    assert len(f) == 1 and f[0]["severity"] == "notable" and f[0]["evidence"]["domain"] == "deals.example"
    assert s.hit("B12", uid(2))["cause"] == "Linked to the same site as 2 other accounts in a short time"


def test_same_site_over_weeks_is_info_without_b12(tmp_path):
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"see {WORDS[k]}", urls=[f"https://blog.example/p/{k}"],
                         created_at=at(k * 60 * 24 * 10)) for k in range(3)]
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "domain"]
    assert len(f) == 1 and f[0]["severity"] == "info"
    assert "B12" not in s.fired(uid(0))


def test_common_site_is_ignored(tmp_path):
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"code {WORDS[k]}", urls=[f"https://github.com/x/repo{k}"],
                         created_at=at(k)) for k in range(3)]
    s = run(tmp_path, profs, items)
    assert findings(s, "shared_content") == []


def test_b02_has_no_input_without_text_or_links(tmp_path):
    items = [interaction(src=uid(0), unreadable=("text", "urls"))]
    s = run(tmp_path, [acct(0)], items)
    assert s.skipped(uid(0))["B02"] == {"text+urls"}
    assert s.skipped(uid(0))["B12"] == {"urls"}


# -- e. lookalike names -------------------------------------------------------


def test_lookalike_of_owner_is_a_high_finding(tmp_path):
    # Like the live case @RonDilley and @Ron_Dilley: only a separator is different.
    profs = [acct(0, handle="ron_dilley", name="Ron"), acct(1)]
    items = [interaction(src=uid(0)), interaction(src=uid(1), tgt=uid(0), type="mention")]
    s = run(tmp_path, profs, items)
    f = findings(s, "lookalike")
    assert len(f) == 1
    assert f[0]["severity"] == "high"
    assert f[0]["members"] == [uid(0), EGO]
    assert f[0]["evidence"]["match"] == "handle_skeleton"
    assert f[0]["evidence"]["reference_role"] == "owner"
    assert f[0]["evidence"]["interacting_accounts"] == [uid(1)]
    assert "I01" in s.fired(uid(0))


def test_lookalike_of_two_unknown_accounts_is_info(tmp_path):
    profs = [acct(0, handle="quantum_sec_labs"), acct(1, handle="quantumseclabs")]
    items = [interaction(src=uid(0)), interaction(src=uid(1))]
    s = run(tmp_path, profs, items)
    f = findings(s, "lookalike")
    assert len(f) == 1 and f[0]["severity"] == "info"


@pytest.mark.parametrize("a, b", [
    ("bob_k1", "bob_k2"),  # short handles one letter apart
    ("anna12345678", "anna12345679"),  # generated digit tails
    ("maple_harbor", "copper_violet"),  # different
    ("quartzfalcon", "quartzfalcxx"),  # unknown accounts: two letters apart
])
def test_lookalike_does_not_fire(tmp_path, a, b):
    profs = [acct(0, handle=a), acct(1, handle=b)]
    items = [interaction(src=uid(0)), interaction(src=uid(1))]
    s = run(tmp_path, profs, items)
    assert findings(s, "lookalike") == []


# -- f. profile outliers ------------------------------------------------------


def outlier_fixture(with_outlier: bool, n: int = 24):
    profs = [acct(k, followers_count=400 + 37 * k, following_count=300 + 11 * k, tweet_count=3000 + 211 * k,
                  created_at=f"20{12 + k % 8}-0{1 + k % 9}-15T00:00:00.000Z") for k in range(n)]
    if with_outlier:
        profs.append(acct(n, followers_count=0, following_count=2500, tweet_count=9000,
                          created_at="2026-10-20T00:00:00.000Z"))
    items = [interaction(src=p.user_id, created_at=at(k * 90)) for k, p in enumerate(profs)]
    return profs, items, []


def test_profile_outlier_is_reported_as_info(tmp_path):
    profs, items, _ = outlier_fixture(True)
    s = run(tmp_path, profs, items)
    f = findings(s, "profile_outliers")
    assert len(f) == 1
    assert f[0]["members"] == [uid(24)] and f[0]["severity"] == "info"
    assert set(f[0]["evidence"]["z_scores"]) >= {"follower_ratio", "account_age"}


def test_no_outlier_in_a_uniform_contact_list(tmp_path):
    profs, items, _ = outlier_fixture(False)
    s = run(tmp_path, profs, items)
    assert findings(s, "profile_outliers") == []


def test_outliers_need_enough_contacts(tmp_path):
    profs, items, _ = outlier_fixture(True, n=10)
    s = run(tmp_path, profs, items)
    assert findings(s, "profile_outliers") == []


def test_robust_z_ignores_the_outlier_for_the_center():
    zs = P._robust_z({"a": 1.0, "b": 1.1, "c": 0.9, "d": 1.05, "e": 9.0})
    assert zs["e"] > 3.5 and abs(zs["a"]) < 1.0
    assert P._robust_z({"a": 1.0, "b": 1.0}) == {}


# -- g. one-way pressure ------------------------------------------------------


def test_many_unanswered_contacts_are_reported(tmp_path):
    items = [interaction(src=uid(0), type="mention" if k % 2 else "reply", created_at=at(k * 120)) for k in range(5)]
    s = run(tmp_path, [acct(0)], items)
    f = findings(s, "one_way")
    assert len(f) == 1 and f[0]["members"] == [uid(0)] and f[0]["severity"] == "info"
    assert f[0]["evidence"]["posts"] == 5


def test_one_answer_from_the_owner_stops_one_way(tmp_path):
    items = [interaction(src=uid(0), created_at=at(k * 120)) for k in range(6)]
    items.append(interaction(src=EGO, tgt=uid(0), created_at=at(900)))
    s = run(tmp_path, [acct(0)], items)
    assert findings(s, "one_way") == []


def test_four_contacts_are_not_one_way(tmp_path):
    items = [interaction(src=uid(0), created_at=at(k * 120)) for k in range(4)]
    s = run(tmp_path, [acct(0)], items)
    assert findings(s, "one_way") == []


# -- h. tight groups ----------------------------------------------------------


def test_mutual_triangle_is_a_tight_group(tmp_path):
    pairs = [(0, 1), (1, 0), (1, 2), (2, 1), (0, 2), (2, 0)]
    items = [interaction(src=uid(a), tgt=uid(b), type="mention", created_at=at(n)) for n, (a, b) in enumerate(pairs)]
    items.append(interaction(src=uid(0)))
    s = run(tmp_path, [acct(k) for k in range(3)], items)
    f = findings(s, "tight_group")
    assert len(f) == 1 and sorted(f[0]["members"]) == [uid(0), uid(1), uid(2)] and f[0]["severity"] == "info"


def test_one_way_mention_triangle_is_not_a_group(tmp_path):
    # A reply thread: X adds every participant to the mention list.
    pairs = [(0, 1), (0, 2), (1, 2)]
    items = [interaction(src=uid(a), tgt=uid(b), type="mention", created_at=at(n)) for n, (a, b) in enumerate(pairs)]
    s = run(tmp_path, [acct(k) for k in range(3)], items)
    assert findings(s, "tight_group") == []


# -- changes from the labeled benchmark (tests/validation, 2026-11-03) ---------
#
# Each test below holds one detector change that the benchmark showed to be
# necessary (background/2026-11-03_detector_validation.md).


def crowd(n: int, start: int = 100, **over):
    """n established contacts with distinct handles (uids 300 + start ...)."""
    return [acct(start + k, handle=f"crowd_{WORDS[k % len(WORDS)]}_{k}", name=f"Crowd {k}", **over) for k in range(n)]


def test_large_burst_of_established_accounts_is_info(tmp_path):
    # Before: 8 or more accounts gave "notable" by size alone, and each popular post fired.
    profs, items, posts = burst_fixture(10, 0.5, young=False)
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "reply_burst")
    assert len(f) == 1 and f[0]["severity"] == "info"
    assert not any("B09" in s.fired(uid(k)) for k in range(10))


def reach_fixture(burst_size: int, neighbor_replies: int, burst_young: int = 3):
    """25 owner posts of a similar reach with ``neighbor_replies`` fast replies each, and one post with a burst.

    6 of the 40 regular contacts are young, so 3 young accounts in 10 are not a
    surprise for the young test alone.
    """
    posts, items = [], []
    profs = crowd(34) + [acct(134 + k, handle=f"newer_{WORDS[k]}_{k}", created_at=days_before(60 + k)) for k in range(6)]
    for p in range(25):
        pid = f"71000{p:04d}"
        posts.append((pid, at(p * 1440), 500 + 10 * p, neighbor_replies))
        for k in range(neighbor_replies):
            u = str(400 + (p * 3 + k) % 40)
            items.append(interaction(src=u, created_at=at(p * 1440 + 2 + k), target_post_id=pid,
                                     text=f"organic {p} {k} {WORDS[k % 30]}"))
    target = "719999999"
    posts.append((target, at(26 * 1440), 520, burst_size))
    burst = [acct(k, handle=f"bought_{WORDS[k]}_{k}",
                  **({"created_at": days_before(40 + 9 * k)} if k < burst_young else {})) for k in range(burst_size)]
    for k in range(burst_size):
        items.append(interaction(src=uid(k), created_at=at(26 * 1440 + 3 + k * 0.5), target_post_id=target,
                                 text=f"burst {k} {WORDS[k]}"))
    return profs + burst, items, posts


def test_burst_large_for_the_reach_of_the_post_is_notable(tmp_path):
    # 10 accounts (3 young) in 5 minutes on a post whose neighbors get 2 replies.
    profs, items, posts = reach_fixture(10, 2)
    s = run(tmp_path, profs, items, posts)
    f = [x for x in findings(s, "reply_burst") if x["evidence"]["anchor_post_id"] == "719999999"]
    assert len(f) == 1 and f[0]["severity"] == "notable"
    assert f[0]["evidence"]["similar_reach_peak"] <= 2.1 and f[0]["evidence"]["peak_accounts"] == 10
    assert f[0]["evidence"]["p_adjusted"] > 0.05  # the young test alone does not fire
    assert "B09" in s.fired(uid(9))  # the size test gives B09 to all members, also the established ones


def test_burst_of_only_established_accounts_is_info_whatever_the_reach(tmp_path):
    # A post that asks for replies gets many organic replies for its reach (benchmark, held-out background).
    profs, items, posts = reach_fixture(10, 2, burst_young=0)
    s = run(tmp_path, profs, items, posts)
    f = [x for x in findings(s, "reply_burst") if x["evidence"]["anchor_post_id"] == "719999999"]
    assert len(f) == 1 and f[0]["severity"] == "info"


def test_burst_normal_for_the_reach_of_the_post_is_info(tmp_path):
    # The neighbors get 6 fast replies each: 10 is less than 2 times 6.
    profs, items, posts = reach_fixture(10, 6)
    s = run(tmp_path, profs, items, posts)
    f = [x for x in findings(s, "reply_burst") if x["evidence"]["anchor_post_id"] == "719999999"]
    assert len(f) == 1 and f[0]["severity"] == "info"


def young_share_fixture(n_young: int, n_old: int):
    """40 established contacts that reply over weeks, then n_young young and n_old old accounts within 10 minutes."""
    profs = crowd(40)
    items = [interaction(src=str(400 + k), created_at=at(-20000 + k * 300), target_post_id="800000001",
                         text=f"spread {k}") for k in range(40)]
    young = [acct(k, created_at=days_before(30 + 20 * k), handle=f"newish_{WORDS[k]}") for k in range(n_young)]
    old = [acct(10 + k, handle=f"steady_{WORDS[k]}") for k in range(n_old)]
    for k, p in enumerate(young + old):
        items.append(interaction(src=p.user_id, created_at=at(2 + k), target_post_id=POST, text=f"now {k}"))
    return profs + young + old, items, [(POST, at(0), 120, n_young + n_old)]


def test_young_accounts_with_organic_replies_are_notable_and_only_young_get_b09(tmp_path):
    profs, items, posts = young_share_fixture(5, 3)
    s = run(tmp_path, profs, items, posts)
    f = [x for x in findings(s, "reply_burst") if x["evidence"]["anchor_post_id"] == POST]
    assert len(f) == 1 and f[0]["severity"] == "notable"
    assert f[0]["evidence"]["young_accounts"] == 5 and f[0]["evidence"]["p_adjusted"] <= 0.05
    assert all("B09" in s.fired(uid(k)) for k in range(5))
    assert not any("B09" in s.fired(uid(10 + k)) for k in range(3))


def test_two_young_accounts_of_five_are_not_a_surprise(tmp_path):
    profs, items, posts = young_share_fixture(2, 3)
    s = run(tmp_path, profs, items, posts)
    f = [x for x in findings(s, "reply_burst") if x["evidence"]["anchor_post_id"] == POST]
    assert len(f) == 1 and f[0]["severity"] == "info"


def test_cluster_in_a_dense_period_is_not_a_surprise(tmp_path):
    # 3 accounts in 20 hours, but 20 other contacts were made in the same months:
    # the local rate makes 3 in 72 hours usual. The uniform rate alone (both local scales off) finds a cluster.
    base = T0 - timedelta(days=200)
    profs = [acct(k, created_at=at(k * 600, base)) for k in range(3)]
    profs += [acct(10 + k, created_at=at((k - 10) * 60 * 24 * 4 + 3000, base)) for k in range(20)]
    items = [interaction(src=p.user_id, created_at=at(k * 300)) for k, p in enumerate(profs)]
    s = run(tmp_path, profs, items)
    assert findings(s, "creation_cluster") == []
    data = with_detector(creation_cluster={"local_days": 0.0001, "short_local_days": 0.0001})
    (tmp_path / "u").mkdir()
    s2 = run(tmp_path / "u", profs, items, rules=data)
    assert len(findings(s2, "creation_cluster")) == 1


def test_formulaic_text_under_40_characters_does_not_group(tmp_path):
    profs = [acct(k) for k in range(4)]
    items = [interaction(src=uid(k), text="Congratulations Ron, well deserved!", created_at=at(k)) for k in range(4)]
    s = run(tmp_path, profs, items)
    assert findings(s, "shared_content") == []


def test_same_link_to_a_common_site_is_info(tmp_path):
    # A news article: 3 accounts on one day, and 2 other accounts linked to the site weeks before.
    profs = [acct(k) for k in range(5)]
    url = "https://news.example/story/2026/a-breach"
    items = [interaction(src=uid(k), text=f"read {WORDS[k]}", urls=[url], created_at=at(k * 30)) for k in range(3)]
    items += [interaction(src=uid(3 + k), text=f"older {WORDS[k]}", urls=[f"https://news.example/story/{k}"],
                          created_at=at(-60 * 24 * 21 - k)) for k in range(2)]
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "link"]
    assert len(f) == 1 and f[0]["severity"] == "info"
    assert f[0]["evidence"]["site_accounts_other_weeks"] == 2
    assert "B02" not in s.fired(uid(0))


KNOWN_C = "390"


def known_fixture(look_handle: str, look_name: str, look_created: str, known_handle: str = "jack_wong_cti",
                  known_name: str = "Jack Wong | CTI"):
    """A top contact (the owner replies to it) and one other account."""
    profs = [acct(90, handle=known_handle, name=known_name, created_at="2016-01-01T00:00:00.000Z"),
             acct(0, handle=look_handle, name=look_name, created_at=look_created)]
    items = [interaction(src=KNOWN_C), interaction(src=EGO, tgt=KNOWN_C), interaction(src=uid(0))]
    return profs, items


@pytest.mark.parametrize("look_handle, look_name, look_created, sev, i02", [
    # A copy of the display name with a Cyrillic small i for the capital I of "CTI".
    ("topaz_citadel", "Jack Wong | CT" + chr(0x456), days_before(40), "high", True),
    # An old account with the same display name: often a second person with that name.
    ("jwong_dfir", "Jack Wong | CTI", "2012-05-01T00:00:00.000Z", "notable", False),
    # A near handle with another display name: chance, not a copy.
    ("jack_wong_ct", "Pixel Zephyr", "2019-05-01T00:00:00.000Z", "info", False),
    ("jack_wong_ct", "Pixel Zephyr", days_before(40), "notable", False),
    # A near handle with the display name copied.
    ("jack_wong_ct", "Jack Wong | CTI", days_before(40), "high", True),
])
def test_lookalike_of_known_account_grades(tmp_path, look_handle, look_name, look_created, sev, i02):
    profs, items = known_fixture(look_handle, look_name, look_created)
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "lookalike") if x["evidence"]["reference"] == KNOWN_C]
    assert len(f) == 1 and f[0]["severity"] == sev
    assert ("I02" in s.fired(uid(0))) is i02


def test_name_with_greek_eta_matches_the_owner_name():
    from visualx.score.skeleton import lookalike_match

    fake = "R" + chr(0x3BF) + chr(0x3B7) + " Dilley"  # omicron and eta
    assert lookalike_match("someone_new", fake, "rondilley", "Ron Dilley", P.DETECTOR_DEFAULTS["lookalike"]) == (
        "name_skeleton", 0)


def test_lookalike_candidates_agree_with_all_pairs():
    from visualx.score.skeleton import lookalike_match

    c = P.DETECTOR_DEFAULTS["lookalike"]
    handles = ["rondilley", "ron_dilley", "rondiley", "r0ndilley", "maple_harbor", "mapleharb0r", "bob_k", "bob_kk",
               "quartzfalcon", "quartzfalcxx", "anna12345678", "anna12345679", "Ron", "jsmith", "ksmith"]
    names = ["Ron Dilley", None, "Maple Harbor", "Bob", "Quartz", "Anna", "Ron Dilley", "J S", "K S"]
    rows = [(str(k), h, names[k % len(names)], None) for k, h in enumerate(handles)]
    brute = {(x, y) for x in range(len(rows)) for y in range(x + 1, len(rows))
             if lookalike_match(rows[x][1], rows[x][2], rows[y][1], rows[y][2], c)}
    fast = {(x, y) for x, y in P._lookalike_candidates(rows, c)
            if lookalike_match(rows[x][1], rows[x][2], rows[y][1], rows[y][2], c)}
    assert fast == brute and len(brute) >= 5


def test_each_profile_feature_gets_a_share_of_the_outlier_list(tmp_path):
    # 12 brand-new accounts have larger z-scores than the one account that follows
    # 5000 with no followers. Ranked by z alone, the new accounts filled the list.
    profs, items, _ = outlier_fixture(False)
    news = [acct(40 + k, created_at=days_before(1 + k * 0.2), followers_count=400 + 37 * k, following_count=300 + 11 * k,
                 tweet_count=10) for k in range(12)]
    ratio = acct(60, followers_count=0, following_count=5000, tweet_count=9000, created_at="2016-03-15T00:00:00.000Z")
    profs += news + [ratio]
    items += [interaction(src=p.user_id, created_at=at(5000 + k)) for k, p in enumerate(news + [ratio])]
    s = run(tmp_path, profs, items, rules=with_detector(profile_outliers={"max_findings": 4}))
    f = findings(s, "profile_outliers")
    assert len(f) == 4
    assert uid(60) in {x["members"][0] for x in f}


def test_group_that_the_owner_never_answers_comes_first(tmp_path):
    # Colleagues (the owner mentions them) in a group of 4, and a group of 3 that the owner never answers.
    colleagues, ring = [0, 1, 2, 3], [5, 6, 7]
    items = []
    for grp in (colleagues, ring):
        for a in grp:
            for b in grp:
                if a != b:
                    items.append(interaction(src=uid(a), tgt=uid(b), type="mention", created_at=at(a * 10 + b)))
    items += [interaction(src=EGO, tgt=uid(a), type="mention", created_at=at(500 + a)) for a in colleagues]
    s = run(tmp_path, [acct(k) for k in colleagues + ring], items, rules=with_detector(tight_group={"max_findings": 1}))
    f = findings(s, "tight_group")
    assert len(f) == 1 and sorted(f[0]["members"]) == [uid(k) for k in ring]
    assert f[0]["evidence"]["owner_answered_share"] == 0.0


# -- stage contract -----------------------------------------------------------


def test_findings_are_replaced_on_each_run_and_counted(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=True)
    s = run(tmp_path, profs, items, posts)
    first = {r["finding_id"] for r in findings(s)}
    assert s.summary["findings"] == {"reply_burst": 1}
    assert s.summary["findings_total"] == 1
    s2 = score(tmp_path, profiles=[], interactions=[], store=s.store)
    rows = findings(s2)
    assert {r["finding_id"] for r in rows} == first  # stable IDs
    assert {r["stage_run_id"] for r in rows} == {s2.summary["stage_run_id"]}


def test_disabled_detector_gives_no_finding(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=True)
    s = run(tmp_path, profs, items, posts, rules=with_detector(reply_burst={"enabled": False}))
    assert findings(s) == []
    assert "B09" not in s.fired(uid(0))


def test_member_rule_severity_limit_is_configurable(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=False)
    data = copy.deepcopy(rules_data())
    data["patterns"]["member_rule_min_severity"] = "info"
    s = run(tmp_path, profs, items, posts, rules=data)
    assert "B09" in s.fired(uid(0))


def all_fixture_runs(tmp_path) -> list[Scored]:
    out = []
    for n, (profs, items, posts) in enumerate([burst_fixture(8, 1, young=True), fast_fixture(4, 4), outlier_fixture(True)]):
        d = tmp_path / str(n)
        d.mkdir()
        out.append(run(d, profs, items, posts))
    d = tmp_path / "x"
    d.mkdir()
    profs = [acct(0, handle="ron_dilley"), *[acct(k) for k in range(1, 4)]]
    items = [interaction(src=uid(k), text=COPY, urls=["https://deals.example/a"], created_at=at(k)) for k in range(4)]
    out.append(run(d, profs, items))
    return out


def test_finding_text_has_no_forbidden_word_and_no_handle(tmp_path):
    word = re.compile("|".join(FORBIDDEN_WORDS), re.IGNORECASE)
    runs = all_fixture_runs(tmp_path)
    detectors = set()
    for s in runs:
        handles = [r["handle"] for r in s.store.q("SELECT handle FROM profiles")]
        for f in findings(s):
            detectors.add(f["detector"])
            text = f["title"] + " " + f["summary"]
            assert not word.search(text), text
            assert not any(h.casefold() in text.casefold() for h in handles), text
            assert f["severity"] in P.SEVERITIES
    assert {"reply_burst", "fast_reply", "profile_outliers", "lookalike", "shared_content"} <= detectors


def test_each_scored_state_has_its_causes(tmp_path):
    # Invariant 5 with the pattern rules.
    for s in all_fixture_runs(tmp_path):
        for user, state in s.states.items():
            if state in ("some_signals", "many_signals"):
                assert any(r["points"] > 0 or r["hard"] for r in s.results.get(user, {}).values()), user


def test_profile_rules_alone_still_never_give_many_signals(tmp_path):
    # FR-35: a burst member with every profile rule gets many_signals only through B09
    # (2 behavior points, the minimum of bands.many_signals_min_family_points).
    profs, items, posts = burst_fixture(4, 2, young=True)
    profs[0] = acct(0, created_at=days_before(3), followers_count=0, following_count=0, tweet_count=100000,
                    description="", url="", location="", handle="user12345678",
                    profile_image_url="https://abs.twimg.com/sticky/default_profile_images/x.png")
    s = run(tmp_path, profs, items, posts, rules=with_detector(reply_burst={"enabled": False}))
    profile_only = {r for r in s.fired(uid(0)) if s.hit(r, uid(0))["family"] == "profile"}
    assert profile_only >= {"P01", "P02", "P03", "P04"}
    assert s.states[uid(0)] == "some_signals"
    (tmp_path / "b").mkdir()
    s2 = run(tmp_path / "b", profs, items, posts)
    assert s2.states[uid(0)] == "many_signals"
    assert "B09" in s2.fired(uid(0))


# -- configuration and TR-06 --------------------------------------------------


def test_patterns_section_loads_from_the_rule_file():
    rs = ruleset_from_data(rules_data())
    assert set(rs.patterns.detectors) == set(P.DETECTOR_DEFAULTS)
    assert rs.patterns.member_rule_min_severity == "notable"


@pytest.mark.parametrize("change, match", [
    ({"detectors": {"no_such_detector": {}}}, "unknown detectors"),
    ({"detectors": {"reply_burst": {"window_hours": 3}}}, "unknown keys"),
    ({"detectors": {"reply_burst": {"min_accounts": "four"}}}, "wrong type"),
    ({"member_rule_min_severity": "severe"}, "severity"),
    ({"colour": 1}, "unknown keys"),
])
def test_bad_patterns_section_is_refused(change, match):
    data = copy.deepcopy(rules_data())
    for k, v in change.items():
        if isinstance(v, dict) and isinstance(data["patterns"].get(k), dict):
            data["patterns"][k].update(v)
        else:
            data["patterns"][k] = v
    with pytest.raises(RuleFileError, match=match):
        ruleset_from_data(data)


@pytest.mark.parametrize("term", ["church.example", "election-news.example", "union.example"])
def test_sensitive_term_in_patterns_section_is_refused(term):
    data = with_detector(shared_content={"ignore_domains": [term]})
    with pytest.raises(RuleFileError, match="sensitive-category"):
        ruleset_from_data(data)


def test_detector_inputs_have_no_sensitive_or_location_or_language_field():
    assert set(P.DETECTOR_INPUTS) == set(P.DETECTOR_DEFAULTS)
    for name, inputs in P.DETECTOR_INPUTS.items():
        for f in (name, *inputs):
            assert sensitive_hits(f) == [], (name, f)
            assert not re.search(r"location|lang|description|country", f), (name, f)


def test_rule_file_patterns_section_has_no_sensitive_term():
    from visualx.score.engine import pattern_policy_errors

    assert pattern_policy_errors(rules_data()["patterns"]) == []


# ---------------------------------------------------------------------------
# Review 20261103: hostile text and large inputs must not stop the score stage
# ---------------------------------------------------------------------------


def test_hostname_with_a_forbidden_substring_does_not_stop_scoring(tmp_path):
    # B-02: "robotics" holds "bot". The host is text from X; it stays in the evidence only.
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"see {WORDS[k]}", urls=[f"https://robotics-club.example/p/{k}"],
                         created_at=at(k * 600)) for k in range(3)]
    s = run(tmp_path, profs, items)
    f = [x for x in findings(s, "shared_content") if x["evidence"]["kind"] == "domain"]
    assert len(f) == 1 and f[0]["evidence"]["domain"] == "robotics-club.example"
    assert "robotics" not in f[0]["summary"]


def test_forbidden_word_check_matches_whole_words_only():
    from visualx.score.engine import forbidden_word_hits

    assert forbidden_word_hits("both accounts at robotics-club.example") == []
    assert forbidden_word_hits("legitimate use") == []
    assert forbidden_word_hits("This account is a bot") == ["bot"]
    assert forbidden_word_hits("Bots replied") == ["bot"]
    assert forbidden_word_hits("RISKY") == ["risky"]


def test_lookalike_bucket_of_one_shared_name_is_linear(tmp_path):
    # B-03: 300 old accounts with one display name. Without the bucket limit
    # this gives 300 * 299 / 2 = 44,850 info findings.
    profs = [acct(k, handle=f"member{k:04d}", name="Night Owl Collective") for k in range(300)]
    items = [interaction(src=uid(k)) for k in range(300)]
    s = run(tmp_path, profs, items)
    f = findings(s, "lookalike")
    assert len(f) <= P.DETECTOR_DEFAULTS["lookalike"]["max_info_findings"]


def test_lookalike_bucket_limit_keeps_pairs_with_the_owner(tmp_path):
    # B-03: a large bucket still compares each member with the owner.
    names = [acct(k, handle=f"member{k:04d}", name="Night Owl Collective") for k in range(80)]
    copy_of_owner = acct(90, handle="ron_dilley", name="Night Owl Collective")
    items = [interaction(src=uid(k)) for k in [*range(80), 90]]
    s = run(tmp_path, [*names, copy_of_owner], items)
    owner_pairs = [x for x in findings(s, "lookalike") if x["evidence"]["reference_role"] == "owner"]
    assert [x["members"] for x in owner_pairs] == [[uid(90), EGO]]


def test_binomial_tail_has_no_overflow_for_large_bursts():
    # B-05: math.comb(1200, 600) is too large for a float.
    assert P._binom_tail(600, 1200, 0.3) == pytest.approx(1.4843e-47, rel=1e-3)
    assert P._binom_tail(3, 10, 0.2) == pytest.approx(0.3222004736)
    assert P._binom_tail(0, 5, 0.1) == 1.0 and P._binom_tail(6, 5, 0.1) == 0.0
    from visualx.score import network as NW

    assert NW._binom_tail is P._binom_tail


def test_hosts_after_drop_text_are_not_one_link(tmp_path):
    # A-11 (review 20261103): after drop_text a row holds hosts only. Three
    # accounts with one link give a link finding; after drop_text the same rows
    # give a same-site finding and no link finding (B02 does not fire).
    profs = [acct(k) for k in range(3)]
    items = [interaction(src=uid(k), text=f"see {WORDS[k]}", urls=["https://deals.example/p/1"],
                         created_at=at(k * 600)) for k in range(3)]
    s = run(tmp_path, profs, items)
    assert [x["evidence"]["kind"] for x in findings(s, "shared_content")] == ["link"]
    s.store.drop_text()
    s2 = score(tmp_path, profiles=[ego_profile(), *profs], interactions=[], store=s.store)
    kinds = [x["evidence"]["kind"] for x in findings(s2, "shared_content")]
    assert kinds == ["domain"]
    assert "B02" not in s2.fired(uid(0))


def test_low_base_burst_needs_the_minimum_of_young_accounts(tmp_path):
    # B-09 (review 20261103): with fewer than 30 contacts of known age, 2 young
    # accounts in a burst of 4 gave a notable finding and B09. The minimum is 3.
    profs, items, posts = burst_fixture(4, 2, young=True)
    profs = [acct(k, created_at=days_before(20 + 15 * k) if k < 2 else "2012-01-01T00:00:00.000Z")
             for k in range(4)]
    s = run(tmp_path, profs, items, posts)
    f = findings(s, "reply_burst")
    assert len(f) == 1 and f[0]["severity"] == "info"
    assert not any("B09" in s.fired(uid(k)) for k in range(4))
    # 3 young accounts of 4 still fire.
    profs3 = [acct(k, created_at=days_before(20 + 15 * k) if k < 3 else "2012-01-01T00:00:00.000Z")
              for k in range(4)]
    s3 = run(tmp_path / "b", profs3, items, posts)
    assert findings(s3, "reply_burst")[0]["severity"] == "notable"


def test_creation_cluster_p_value_comes_from_one_window(tmp_path):
    # B-10 (review 20261103): a chain of windows was merged, and its full count was
    # tested against the expected count of one window. Two groups of 3, 60 hours
    # apart, merge into one span of 6; the p-value must be the one of 3 in a window.
    base = T0 - timedelta(days=40)
    hours = [0, 5, 10, 70, 75, 80]
    profs = [acct(k, created_at=at(h * 60, base)) for k, h in enumerate(hours)]
    profs += [acct(k, created_at=f"20{10 + k}-05-01T00:00:00.000Z") for k in range(6, 12)]
    items = [interaction(src=uid(k), created_at=at(k * 300)) for k in range(12)]
    s = run(tmp_path, profs, items)
    f = findings(s, "creation_cluster")
    assert len(f) == 1 and len(f[0]["members"]) == 6
    n_recent = 6
    span_h, w = 1095 * 24.0, 72.0
    mu = n_recent * w / span_h
    one_window = P._poisson_tail(3, mu) * (span_h / w)
    assert f[0]["evidence"]["p_adjusted"] >= one_window * 0.999
    assert f[0]["evidence"]["p_adjusted"] > P._poisson_tail(6, mu) * (span_h / w)


def test_dense_mutual_group_is_one_finding_not_each_clique(tmp_path):
    # B-13 (review 20261103): the count of maximal cliques can grow exponentially.
    n = 20
    profs = [acct(k) for k in range(n)]
    items = [interaction(src=uid(a), tgt=uid(b), type="mention") for a in range(n) for b in range(n) if a != b]
    s = run(tmp_path, profs, items)
    f = findings(s, "tight_group")
    assert len(f) == 1 and f[0]["evidence"]["core_number"] == n - 1 and len(f[0]["members"]) == n
