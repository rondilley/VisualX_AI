"""Synthetic store data for the renderer tests (unit, browser, perf).

The helpers write the stage tables directly (nodes, edges, node_states,
rule_results, profiles, meta, stage_runs), in the same shape that build,
analyze, and score write. This module has no tests of its own.
"""

from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from tests.helpers import interaction, mark_stage, profile
from visualx.store import Store

HOSTILE_NAME = "<img src=x onerror=alert(1)>"
HOSTILE_BIO = "bio </script><script>alert(2)</script> end"
HOSTILE_URL = "javascript:alert(1)"
# Built with chr() so that the source file stays ASCII.
BIDI_NAME = "abc" + chr(0x202E) + "dcba" + chr(0x202C) + chr(0x2066) + "x" + chr(0x2069)
ZW_NAME = "zero" + chr(0x200B) + "width" + chr(0x200D) + chr(0xFEFF) + "name"
LONG_BIO = ("L" * 99_991) + "-LONG-END"
ENTITY_BIO = "&lt;b&gt;bold&lt;/b&gt; &amp; &#60;script&#62; &quot;q&quot;"
GOOD_URL = "https://example.com/profile"
DATA_URL = "data:text/html,<b>x</b>"


def _node(uid: str, handle: str | None, name: str | None, x: float, y: float, *, status: str = "ok",
          ego: bool = False, wd: float = 1.0) -> dict[str, Any]:
    return {"user_id": uid, "handle": handle, "name": name, "profile_status": status, "is_ego": int(ego),
            "in_degree": 1, "out_degree": 1, "degree": 2, "weighted_degree": wd, "ego_weight": wd,
            "pagerank": 0.01, "betweenness": 0.0, "x": x, "y": y}


def _edge(s: str, t: str, *, reply: int = 0, quote: int = 0, mention: int = 0, repost: int = 0,
          weight: float, pair_weight: float = 0.0, times: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {"source": s, "target": t, "reply_count": reply, "quote_count": quote, "mention_count": mention,
            "repost_count": repost, "timestamps_json": json.dumps(times or {}), "weight": weight,
            "pair_weight": pair_weight}


def fill_store(store: Store, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], *,
               profiles: list[Any] = (), states: dict[str, tuple[str, float, str | None]] | None = None,
               rules: list[tuple[str, str, str, float, str, int]] = (), ego_id: str | None = None,
               ego_handle: str | None = None, findings: list[dict[str, Any]] = (),
               interactions: list[Any] = ()) -> None:
    """Write stage outputs and mark collect, build, analyze, and score as finished."""
    mark_stage(store, "collect")
    build = mark_stage(store, "build")
    analyze = mark_stage(store, "analyze")
    score = mark_stage(store, "score")
    with store.tx() as c:
        c.executemany(
            "INSERT INTO nodes(user_id, handle, name, profile_status, is_ego, in_degree, out_degree, degree, "
            "weighted_degree, ego_weight, pagerank, betweenness, x, y, stage_run_id) "
            "VALUES (:user_id,:handle,:name,:profile_status,:is_ego,:in_degree,:out_degree,:degree,"
            ":weighted_degree,:ego_weight,:pagerank,:betweenness,:x,:y,'" + analyze + "')",
            nodes,
        )
        c.executemany(
            "INSERT INTO edges(source, target, reply_count, quote_count, mention_count, repost_count, "
            "timestamps_json, weight, pair_weight, stage_run_id) VALUES (:source,:target,:reply_count,"
            ":quote_count,:mention_count,:repost_count,:timestamps_json,:weight,:pair_weight,'" + build + "')",
            edges,
        )
        for uid, (state, pts, note) in (states or {}).items():
            c.execute("INSERT INTO node_states VALUES (?,?,?,?,?)", (uid, state, pts, note, score))
        for uid, rid, fam, pts, cause, hard in rules:
            c.execute("INSERT INTO rule_results VALUES (?,?,?,?,?,?,?,?)", (uid, rid, fam, pts, "null", cause, hard, score))
        for f in findings:
            c.execute(
                "INSERT INTO findings(finding_id, detector, title, severity, summary, members_json, evidence_json, "
                "stage_run_id) VALUES (?,?,?,?,?,?,?,?)",
                (f["id"], f["detector"], f["title"], f["severity"], f["summary"], json.dumps(f["members"]),
                 json.dumps(f.get("evidence", {})), score),
            )
    if interactions:
        store.put_interactions(interactions)
    if profiles:
        store.put_profiles(profiles)
    if ego_id:
        store.set_meta("ego_user_id", ego_id)
    if ego_handle:
        store.set_meta("ego_handle", ego_handle)


FIRST_TIME = "2026-10-01T10:00:00.000Z"
# A finding member that is not a node in the graph. render_members must still hold it (AD-10).
FINDING_ONLY_ID = "7777"

SMALL_IDS = {"ego": "1000", "alice": "1001", "bob": "1002", "carol": "1003", "dave": "1004", "frank": "1006",
             "missing": "1099", "h_img": "2001", "h_bio": "2002", "h_url": "2003", "h_bidi": "2004",
             "h_zw": "2005", "h_long": "2006", "h_ent": "2007", "good_url": "2008"}


def small_graph(store: Store) -> dict[str, str]:
    """A hand-made graph with a known weighted shortest path and the TSC-20 hostile values.

    Path from alice (1001) to bob (1002), all types shown: alice - carol - bob
    (distance 1/6 + 1/6). With the quote type hidden: alice - owner - bob
    (distance 1/3 + 1/1). frank (1006) has no edges. 1099 has no profile.
    """
    ids = SMALL_IDS
    nodes = [
        _node(ids["ego"], "owner", "Owner", 0, 0, ego=True, wd=20),
        _node(ids["alice"], "alice", "Alice A", -10, 5, wd=9),
        _node(ids["bob"], "bob", "Bob B", 10, 5, wd=7),
        _node(ids["carol"], "carol", "Carol C", 0, 12, wd=12),
        _node(ids["dave"], "dave", "Dave D", 5, -10, wd=1.5),
        _node(ids["frank"], "frank", "Frank F", -15, -15, wd=0),
        _node(ids["missing"], None, None, 15, -15, status="missing", wd=1),
        _node(ids["h_img"], "h_img", HOSTILE_NAME, -20, 10, wd=1),
        _node(ids["h_bio"], "h_bio", "Bio Tag", -20, 0, wd=1),
        _node(ids["h_url"], "h_url", "Url Js", -20, -10, wd=1),
        _node(ids["h_bidi"], "h_bidi", BIDI_NAME, 20, 10, wd=1),
        _node(ids["h_zw"], "h_zw", ZW_NAME, 20, 0, wd=1),
        _node(ids["h_long"], "h_long", "Long Bio", 20, -5, wd=1),
        _node(ids["h_ent"], "h_ent", "Entity Bio", 25, 5, wd=1),
        _node(ids["good_url"], "good_url", "Good Url", 25, -5, wd=1),
    ]
    # Link times for playback (FIRST_TIME is the earliest). The repost has no time (retweeted_by gives none).
    edges = [
        _edge(ids["alice"], ids["ego"], reply=2, weight=3.0, times={"reply": [FIRST_TIME, "2026-10-02T09:00:00.000Z"]}),
        _edge(ids["ego"], ids["bob"], mention=1, weight=1.0, times={"mention": ["2026-10-05T12:00:00.000Z"]}),
        _edge(ids["alice"], ids["carol"], reply=3, weight=6.0, times={"reply": ["2026-10-10T08:30:00.000Z"]}),
        _edge(ids["carol"], ids["bob"], quote=4, weight=6.0, times={"quote": ["2026-10-20T18:00:00.000Z"]}),
        _edge(ids["dave"], ids["ego"], mention=1, weight=1.5, times={"mention": ["2026-10-25T07:15:00.000Z"]}),
        _edge(ids["missing"], ids["ego"], repost=1, weight=1.0),
    ]
    for n, k in enumerate(("h_img", "h_bio", "h_url", "h_bidi", "h_zw", "h_long", "h_ent", "good_url")):
        edges.append(_edge(ids[k], ids["ego"], mention=1, weight=1.5,
                           times={"mention": [f"2026-10-{26 + n // 2:02d}T1{n}:00:00.000Z"]}))
    acts = [
        interaction("reply", ids["alice"], ids["ego"], spost="9001", at=FIRST_TIME),
        interaction("reply", ids["alice"], ids["ego"], spost="9002", at="2026-10-02T09:00:00.000Z"),
        interaction("quote", ids["carol"], ids["bob"], spost="9003", at="2026-10-20T18:00:00.000Z"),
        interaction("repost", ids["missing"], ids["ego"], spost="9004"),
    ]
    findings = [
        {"id": "F-hostile", "detector": "test_detector", "title": HOSTILE_NAME, "severity": "high",
         "summary": HOSTILE_BIO, "members": [ids["h_img"], ids["carol"], ids["bob"], FINDING_ONLY_ID],
         "evidence": {"note": HOSTILE_BIO, "<b>key</b>": [1, 2], "ids": [ids["carol"]]}},
        {"id": "F-plain", "detector": "test_detector", "title": "Two accounts reply at the same time",
         "severity": "notable", "summary": "Plain summary.", "members": [ids["alice"], ids["dave"]]},
    ]
    profs = [
        profile(ids["ego"], handle="owner", name="Owner", followers_count=500),
        profile(ids["alice"], handle="alice", name="Alice A", description="Alice bio", followers_count=100,
                following_count=50, tweet_count=1000, listed_count=2, created_at="2020-01-01T00:00:00Z"),
        profile(ids["bob"], handle="bob", name="Bob B", description="Bob bio", followers_count=10),
        profile(ids["carol"], handle="carol", name="Carol C", followers_count=3000),
        profile(ids["dave"], handle="dave", name="Dave D", followers_count=5),
        profile(ids["frank"], handle="frank", name="Frank F", followers_count=1),
        profile(ids["h_img"], handle="h_img", name=HOSTILE_NAME, description=HOSTILE_BIO),
        profile(ids["h_bio"], handle="h_bio", name="Bio Tag", description=HOSTILE_BIO),
        profile(ids["h_url"], handle="h_url", name="Url Js", url=HOSTILE_URL, description="url test"),
        profile(ids["h_bidi"], handle="h_bidi", name=BIDI_NAME, description=BIDI_NAME),
        profile(ids["h_zw"], handle="h_zw", name=ZW_NAME, description=ZW_NAME),
        profile(ids["h_long"], handle="h_long", name="Long Bio", description=LONG_BIO),
        profile(ids["h_ent"], handle="h_ent", name="Entity Bio", description=ENTITY_BIO, url=DATA_URL),
        profile(ids["good_url"], handle="good_url", name="Good Url", url=GOOD_URL),
    ]
    states = {
        ids["ego"]: ("not_scored", 0.0, None),
        ids["alice"]: ("no_signals", 0.0, None),
        ids["bob"]: ("some_signals", 2.0, None),
        ids["carol"]: ("many_signals", 6.0, "Owner note <b>checked</b>"),
        ids["dave"]: ("no_signals", 0.0, None),
        ids["frank"]: ("no_signals", 0.0, None),
        ids["missing"]: ("not_scored", 0.0, None),
        ids["h_img"]: ("some_signals", 2.0, None),
    }
    rules = [
        (ids["bob"], "P01", "profile", 2.0, "Default profile values <i>seen</i>", 0),
        (ids["carol"], "B01", "behavior", 4.0, "Many replies in a short time", 0),
        (ids["carol"], "I01", "impersonation", 2.0, "Name is close to a known handle", 1),
        (ids["h_img"], "P03", "profile", 2.0, HOSTILE_NAME, 0),
    ]
    fill_store(store, nodes, edges, profiles=profs, states=states, rules=rules, ego_id=ids["ego"], ego_handle="owner",
               findings=findings, interactions=acts)
    return ids


def synthetic(store: Store, n_nodes: int, n_edges: int, seed: int = 7, missing_share: float = 0.02) -> None:
    """A random graph with realistic text sizes (bios up to 160 characters, handles, names)."""
    rng = random.Random(seed)
    ego = "9" * 18
    ids = [ego] + [str(10**17 + i * 7919) for i in range(1, n_nodes)]
    nodes, profs, states, rules = [], [], {}, []
    letters = "abcdefghijklmnopqrstuvwxyz0123456789_"
    words = ["security", "data", "music", "news", "art", "cloud", "open", "source", "travel", "photo", "dev", "ops"]
    for i, uid in enumerate(ids):
        r = 0 if i == 0 else 100 * math.sqrt(rng.random())
        a = rng.random() * 2 * math.pi
        handle = "".join(rng.choice(letters) for _ in range(rng.randint(5, 15)))
        name = " ".join(rng.choice(words).title() for _ in range(rng.randint(1, 3)))
        status = "missing" if i and rng.random() < missing_share else "ok"
        nodes.append(_node(uid, handle if status == "ok" else None, name if status == "ok" else None,
                           r * math.cos(a), r * math.sin(a), status=status, ego=i == 0, wd=rng.random() * 50))
        if status == "ok":
            bio = " ".join(rng.choice(words) for _ in range(rng.randint(3, 25)))[:160]
            profs.append(profile(uid, handle=handle, name=name, description=bio,
                                 url=f"https://example.com/{handle}", location="Somewhere",
                                 created_at="2015-06-01T00:00:00Z", followers_count=rng.randint(0, 100000),
                                 following_count=rng.randint(0, 5000), tweet_count=rng.randint(0, 50000),
                                 listed_count=rng.randint(0, 100)))
            st = rng.choice(["no_signals", "no_signals", "no_signals", "some_signals", "many_signals"])
            states[uid] = (st, 0.0 if st == "no_signals" else 3.0, None)
            if st != "no_signals":
                rules.append((uid, "B01", "behavior", 3.0, "Many replies in a short time window", 0))
        else:
            states[uid] = ("not_scored", 0.0, None)
    seen: set[tuple[int, int]] = set()
    edges = []
    base = datetime(2026, 10, 31, tzinfo=UTC)
    while len(edges) < n_edges:
        s = 0 if rng.random() < 0.3 else rng.randrange(1, n_nodes)
        t = 0 if s != 0 and rng.random() < 0.5 else rng.randrange(1, n_nodes)
        if s == t or (s, t) in seen:
            continue
        seen.add((s, t))
        c = [rng.choice([0, 0, 1, 2, 5]) for _ in range(4)]
        if not any(c):
            c[2] = 1
        # Times over 30 days for the types that have a time; reposts have none (retweeted_by).
        times = {}
        for k, name in enumerate(("reply", "quote", "mention")):
            if c[k]:
                times[name] = sorted((base - timedelta(minutes=rng.randrange(0, 43200))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                                     for _ in range(min(c[k], 2)))
        edges.append(_edge(ids[s], ids[t], reply=c[0], quote=c[1], mention=c[2], repost=c[3],
                           weight=round(rng.random() * 10 + 0.5, 3), pair_weight=0.0, times=times))
    fill_store(store, nodes, edges, profiles=profs, states=states, rules=rules, ego_id=ego, ego_handle="owner")
