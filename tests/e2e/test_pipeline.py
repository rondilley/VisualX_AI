"""TR-02: recorded (hand-made) X API responses -> collect -> build -> analyze -> score -> render -> delete.

This proves the wiring of all stages together. It is not a live run (SUCCESS_CRITERIA §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.helpers import make_env
from tests.unit.xapi_test_server import XApiTestServer
from visualx.analyze import run_analyze
from visualx.build import run_build
from visualx.collect.plan import run_collect
from visualx.collect.xapi import XApiClient
from visualx.delete import decode_payloads, delete_ids, find_id
from visualx.render.render import run_render
from visualx.score.engine import run_score

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def _pipeline(cfg, store, log):
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    run_score(cfg, store, log)
    return run_render(cfg, store, log)


def test_recorded_responses_to_html_and_delete(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    with XApiTestServer() as srv:
        client = XApiClient(srv.base_url, token_provider=lambda: "tok", log=log, sleep=lambda s: None,
                            now=lambda: NOW.timestamp())
        res = run_collect(cfg, store, cache, log, client, lambda text: True, dry_run=False, run_id="e2e", now=NOW)
        client.close()
    assert res.complete, res.stopped_reason
    assert res.total_cost <= cfg.get("collect.cap_usd")

    html = _pipeline(cfg, store, log)
    text = html.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text
    (payload,) = decode_payloads(text)
    interacting = {r["source_user_id"] for r in store.q("SELECT source_user_id FROM interactions")}
    assert interacting

    # Each scored account other than no_signals/not_scored has its causes (invariant 5).
    for r in store.q("SELECT user_id, state FROM node_states WHERE state IN ('some_signals','many_signals')"):
        assert store.q1("SELECT COUNT(*) AS n FROM rule_results WHERE user_id=?", (r["user_id"],))["n"] > 0

    # Delete one third-party account everywhere, with a new render (TSC-09, BSC-06).
    victim = "2001"
    assert victim in str(payload)
    assert cache.files_with({victim})

    def rerender() -> str:
        return str(_pipeline(cfg, store, log))

    rep = delete_ids(store, cache, tmp_path / "config", [victim], "user", rerender)
    assert rep.renders_deleted and rep.rerendered
    assert rep.remaining == []
    assert not html.exists()
    assert find_id(store, cache, victim) == []
    new_payload = decode_payloads(open(rep.rerendered, encoding="utf-8").read())
    assert victim not in str(new_payload)
