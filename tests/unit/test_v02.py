"""v0.2 features: Leiden groups, hubs and bridges, topics, recency decay, exports, --redact, B06.

FR-20, FR-26 to FR-28, FR-30, FR-51, FR-56 (B06), TSC-11, threat T7, AD-10.
"""

from __future__ import annotations

import json
import random
import xml.etree.ElementTree as ET

import igraph as ig
import pytest

from tests.helpers import interaction, make_env, profile
from tests.unit.test_render_support import small_graph
from visualx import groups as G
from visualx.analyze import run_analyze
from visualx.build import _count_term, decayed_counts, edge_weight, run_build
from visualx.config import ConfigError
from visualx.config import load as load_config
from visualx.delete import decode_payloads, delete_ids, find_id
from visualx.export import gexf_text, run_export, xnm_docs
from visualx.render import run_render
from visualx.score.engine import RuleFileError, load_feed, load_rules
from visualx.score.signals import SIGNALS, Account, Inputs, MissingInput

EGO = "1"


def two_groups():
    """Owner 1 links to all. Group A (10-15) and group B (20-25) are cliques. 30 links into both."""
    nodes = [EGO] + [str(u) for u in [*range(10, 16), *range(20, 26), 30]]
    edges = []

    def add(s, t, w=2.0):
        edges.append({"source": s, "target": t, "weight": w, "pair_weight": w})

    for grp in (range(10, 16), range(20, 26)):
        for a in grp:
            for b in grp:
                if a < b:
                    add(str(a), str(b))
    for u in ("10", "11", "20", "21"):
        add("30", u, 1.0)
    for u in nodes[1:]:
        add(EGO, u, 5.0)
    return nodes, edges


# -- groups (FR-26, FR-30, TSC-11) ---------------------------------------------------------------


def test_leiden_communities_stability_hubs_and_bridges():
    nodes, edges = two_groups()
    out, counts = G.run_groups(nodes, edges, EGO, {}, {}, {"hub_min_degree": 3})
    assert counts["leiden_runs"] == 20 and counts["communities"] == 2
    alters = [u for u in nodes if u != EGO]
    # TSC-11: a stability value for each account (the owner is not in the ego-removed graph).
    assert all(out[u]["stability"] is not None for u in alters)
    assert out[EGO]["community"] is None and out[EGO]["stability"] is None
    assert {out[u]["community"] for u in map(str, range(10, 16))} == {out["10"]["community"]}
    assert out["10"]["community"] != out["20"]["community"]
    assert out["30"]["is_bridge"] == 1 and "betweenness" in out["30"]["bridge_reason"]
    assert out["30"]["bridge_communities"] >= 1
    assert sum(r["is_hub"] for r in out.values()) >= 1
    assert out["10"]["community_size"] >= 6


def test_stability_is_low_on_a_graph_with_no_structure():
    random.seed(3)
    g = ig.Graph.Erdos_Renyi(n=300, m=900)
    g.es["weight"] = [1.0] * g.ecount()
    parts = G.leiden_runs(g, 1.0, 20, 1)
    best = max(range(20), key=lambda k: parts[k][1])
    s = G.stability(parts[best][0], [m for k, (m, _) in enumerate(parts) if k != best])
    assert sum(s) / len(s) < 0.6
    assert G.stability([0, 0, 1], [[0, 0, 1], [1, 1, 0]]) == [1.0, 1.0, 1.0]


def test_fewer_than_20_runs_is_refused():
    nodes, edges = two_groups()
    with pytest.raises(ValueError, match="20"):
        G.run_groups(nodes, edges, EGO, {}, {}, {"runs": 5})


# -- topics (FR-28, TR-06) -----------------------------------------------------------------------


def test_topics_match_whole_words_and_propagate():
    pats = G.check_topics({"ai": ["ai", "machine learning"], "security": ["infosec", "re:\\bsec(ops)?\\b"]})
    t = G.topics_for({"a": "I said hello", "b": "AI and InfoSec person", "c": "SecOps lead", "d": None}, pats)
    assert t == {"b": ["ai", "security"], "c": ["security"]}
    nodes, edges = two_groups()
    desc = {u: "infosec" for u in ("10", "11", "12", "13")}
    out, counts = G.run_groups(nodes, edges, EGO, desc, {"security": ["infosec"]})
    assert out["10"]["topics"] == {"direct": ["security"], "inferred": None}
    assert out["14"]["topics"]["inferred"] == "security"  # four of its five labeled neighbors
    assert out["20"]["topics"]["inferred"] is None  # no labeled neighbor
    assert counts["topic_direct"] == 4 and counts["topic_inferred"] >= 2


@pytest.mark.parametrize("topics", [
    {"politics": ["vote"]},
    {"faith_group": ["church"]},
    {"tech": ["election"]},
    {"tech": ["re:religio"]},
    {"Bad Name": ["x"]},
    {"tech": []},
])
def test_sensitive_or_bad_topics_are_refused(topics):
    with pytest.raises(G.TopicError):
        G.check_topics(topics)


def test_config_load_refuses_a_sensitive_topic(tmp_path):
    cfg, *_ = make_env(tmp_path)
    (cfg.config_dir / "topics.yaml").write_text("topics:\n  health_news: [doctor]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TR-06"):
        load_config(cfg.config_dir, root=tmp_path)


def test_the_project_topics_file_loads():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[2] / "config")
    assert "security" in cfg.topics and G.check_topics(cfg.topics)


# -- recency decay (FR-20) -----------------------------------------------------------------------


def test_decay_off_is_the_v01_formula_and_on_reduces_old_links():
    alpha = {"reply": 3.0, "quote": 2.0, "mention": 1.5, "repost": 1.0}
    e = {"counts": {"reply": 2, "quote": 0, "mention": 0, "repost": 1},
         "times": {"reply": ["2026-11-01T00:00:00.000Z", "2026-08-03T00:00:00.000Z"]}}
    from datetime import datetime

    ref = datetime.fromisoformat("2026-11-01T00:00:00.000Z")
    off = decayed_counts(e, ref, 0)
    assert off == {"reply": 2.0, "quote": 0.0, "mention": 0.0, "repost": 1.0}
    assert edge_weight(off, alpha) == pytest.approx(3.0 * (1 + 0.6931471805599453) + 1.0)
    on = decayed_counts(e, ref, 90)
    assert on["reply"] == pytest.approx(1.5)  # one new, one 90 days old
    assert on["repost"] == 1.0  # no time: counts 1 (DR-02)
    assert _count_term(1.0) == 1.0 and _count_term(0.5) == 0.5


def test_build_with_a_half_life_lowers_the_weight_of_an_old_tie(tmp_path):
    def weights(half_life):
        cfg, store, cache, log = make_env(tmp_path / str(half_life), f"[weights]\nhalf_life_days = {half_life}\n")
        store.set_meta("ego_user_id", EGO)
        store.put_profiles([profile(u, handle="h" + u) for u in (EGO, "2", "3")])
        store.put_interactions([
            interaction("reply", "2", EGO, spost="p1", at="2026-11-01T00:00:00.000Z"),
            interaction("reply", "3", EGO, spost="p2", at="2026-05-01T00:00:00.000Z"),
        ])
        sid = store.begin_stage("collect", None, {})
        store.finish_stage(sid, {})
        run_build(cfg, store, log)
        return {r["source"]: r["weight"] for r in store.q("SELECT source, weight FROM edges")}

    off, on = weights(0), weights(30)
    assert off["2"] == off["3"] == 3.0
    assert on["2"] == 3.0 and on["3"] < 0.1


# -- pipeline: analyze writes node_groups, render shows them --------------------------------------


def pipeline(tmp_path, extra: str = ""):
    cfg, store, cache, log = make_env(tmp_path, extra)
    cfg.topics = {"security": ["infosec"]}
    store.set_meta("ego_user_id", EGO)
    nodes, edges = two_groups()
    store.put_profiles([profile(u, handle="acct" + u, name="Name " + u,
                                description="infosec" if u in ("10", "11") else "hello") for u in nodes])
    items = []
    for k, e in enumerate(edges):
        items.append(interaction("reply", e["source"], e["target"], spost=f"9{k:03d}",
                                 at="2026-10-01T00:00:00.000Z"))
    store.put_interactions(items)
    sid = store.begin_stage("collect", None, {})
    store.finish_stage(sid, {})
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    from visualx.score.engine import run_score

    run_score(cfg, store, log)
    return cfg, store, cache, log


def test_analyze_records_groups_and_the_page_gets_them(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    rows = {r["user_id"]: r for r in store.q("SELECT * FROM node_groups")}
    assert len(rows) == 14 and rows[EGO]["stability"] is None
    assert all(r["stability"] is not None for u, r in rows.items() if u != EGO)
    path = run_render(cfg, store, log)
    d = decode_payloads(path.read_text(encoding="utf-8"))[0]
    i = d["n"]["id"].index("10")
    assert d["n"]["cm"][i] is not None and d["n"]["tc"][i] == ["security"]
    assert d["cfg"]["topics"] == ["security"]
    assert d["meta"]["groups"]["leiden_runs"] == 20 and d["meta"]["groups"]["communities"] == 2
    # Deletion removes the group row (invariant 11).
    delete_ids(store, cache, cfg.config_dir, ["10"], "user")
    assert store.q("SELECT * FROM node_groups WHERE user_id='10'") == []


# -- exports (FR-51) -----------------------------------------------------------------------------


def test_exports_have_no_signal_state_and_deletion_removes_them(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    text, ids = gexf_text(store)
    root = ET.fromstring(text)
    ns = {"g": "http://gexf.net/1.3"}
    assert len(root.findall(".//g:node", ns)) == 14 and len(root.findall(".//g:edge", ns)) > 30
    titles = {a.get("title") for a in root.findall(".//g:attribute", ns)}
    assert "community" in titles and "hub" in titles
    for word in ("signals", "state", "rule", "finding", "points"):
        assert word not in text.casefold()
    inter, prof, _ = xnm_docs(store)
    assert set(inter[0]) == {"from", "to", "type", "count", "last"}
    assert prof["acct10"] == {"name": "Name 10", "bio": "infosec", "followers": None, "img": None}
    paths = run_export(cfg, store, log, "gexf") + run_export(cfg, store, log, "xnm")
    assert len(paths) == 3 and all(p.exists() for p in paths)
    assert any(h.startswith("export:") for h in find_id(store, cache, "20"))
    rep = delete_ids(store, cache, cfg.config_dir, ["20"], "user")
    assert len(rep.exports_deleted) == 3 and not any(p.exists() for p in paths)
    assert rep.remaining == []


def test_export_strips_xml_control_characters(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    store.conn.execute("UPDATE nodes SET name=? WHERE user_id='10'", ("bad" + chr(1) + "<&>",))
    text, _ = gexf_text(store)
    ET.fromstring(text)
    assert chr(1) not in text


# -- --redact render (threat T7) -----------------------------------------------------------------


def test_redacted_render_holds_no_identity_or_signal(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    ids = small_graph(store)
    path = run_render(cfg, store, log, redact=True)
    assert path.name.endswith("_redacted.html")
    html = path.read_text(encoding="utf-8")
    d = decode_payloads(html)[0]
    blob = json.dumps(d)
    for uid in ids.values():
        assert f'"{uid}"' not in blob
    assert all(h is None for h in d["n"]["h"])
    assert all(v is None for col in d["p"].values() for v in col)
    assert d["r"] == [] and d["f"] == [] and d["ov"] == {} and set(d["n"]["st"]) == {0}
    assert d["meta"]["redacted"] is True and d["meta"]["ego_handle"] is None
    assert len(d["e"]["s"]) > 0
    # The render is in the index, so a deletion of a user deletes the redacted file too (AD-10).
    rep = delete_ids(store, cache, cfg.config_dir, [ids["bob"]], "user")
    assert str(path.resolve()) in rep.renders_deleted and not path.exists()


# -- B06 threat feed (FR-56) ---------------------------------------------------------------------


def b06_rule(tmp_path, text: str | None, terms: str = "CC0, checked 2026-11-03", limit: int = 100):
    rs = load_rules(make_env(tmp_path)[0].config_dir / "rules.yaml")
    rule = next(r for r in rs.rules if r.id == "B06")
    rule.params.update({"feed_terms": terms, "max_domains": limit})
    rule.params.pop("domains", None)
    rule.params.pop("feed_missing", None)
    if text is not None:
        (tmp_path / "threat_domains.txt").write_text(text, encoding="utf-8")
    load_feed(rule, tmp_path)
    return rule


def run_b06(rule, urls, prof_url=None):
    sdef = SIGNALS["threat_feed_domain"]
    own = [{"type": "reply", "urls": urls, "field_status": {"urls": "present"}}]
    acc = Account(user_id="5", profile={"url": prof_url, "field_status": {"url": "present"}}, own=own, from_ego=[])
    return sdef.fn(Inputs(acc, sdef), rule.params)


def test_b06_reads_the_feed_and_matches_subdomains(tmp_path):
    rule = b06_rule(tmp_path, "# feed\nevil.example\n0.0.0.0 bad.test\nhttps://www.worse.test/path\nnot a host!\n")
    assert rule.params["domains"] == {"evil.example", "bad.test", "worse.test"}
    assert run_b06(rule, ["https://cdn.evil.example/x"]).value is True
    assert run_b06(rule, ["https://notevil.example/x"]).value is False
    assert run_b06(rule, ["https://ok.test"], prof_url="https://bad.test").value is False  # profile is not read


def test_b06_without_a_feed_is_off_and_the_real_rule_file_loads(tmp_path):
    rule = b06_rule(tmp_path, None)
    assert rule.params.get("feed_missing") is True
    with pytest.raises(MissingInput):
        run_b06(rule, ["https://x.test"])
    with pytest.raises(RuleFileError, match="feed_terms"):
        b06_rule(tmp_path / "t", "a.test\n", terms="")
    with pytest.raises(RuleFileError, match="more than"):
        b06_rule(tmp_path / "s", "".join(f"h{k}.test\n" for k in range(5)), limit=3)


def test_b06_fires_in_the_engine_and_is_a_hard_rule(tmp_path):
    from tests.unit.test_rules_kit import ACC, score

    s0 = score(tmp_path, interactions=[])
    assert "B06" not in s0.fired()
    rs = load_rules(make_env(tmp_path / "e")[0].config_dir / "rules.yaml")
    rule = next(r for r in rs.rules if r.id == "B06")
    rule.params.pop("feed_missing", None)
    rule.params["domains"] = frozenset({"evil.example"})
    from tests.unit.test_rules_kit import interaction as kit_interaction

    s = score(tmp_path / "x", ruleset=rs, interactions=[kit_interaction(urls=["https://evil.example/a"])])
    assert "B06" in s.fired() and s.states[ACC] == "many_signals"
