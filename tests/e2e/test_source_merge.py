"""Grok data (provisional h:<handle> IDs) followed by an X API run: the IDs merge (AD-02)."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.helpers import interaction, make_env, profile
from tests.unit.xapi_test_server import XApiTestServer
from visualx.build import run_build
from visualx.collect.plan import run_collect
from visualx.collect.xapi import XApiClient

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def test_grok_ids_merge_after_x_api_run(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    # State after a Grok run: owner and others under provisional IDs.
    store.set_meta("ego_user_id", "h:owner")
    store.set_meta("ego_handle", "owner")
    store.put_profiles([profile("h:owner", handle="owner"), profile("h:alice", handle="alice"),
                        profile("h:zed", handle="zed")])
    store.put_interactions([
        interaction("reply", "h:alice", "h:owner", spost="5001", at="2026-10-20T00:00:00.000Z"),
        interaction("mention", "h:zed", "h:owner", spost="5002", at="2026-10-21T00:00:00.000Z"),
    ])
    with XApiTestServer() as srv:
        client = XApiClient(srv.base_url, token_provider=lambda: "tok", log=log, sleep=lambda s: None,
                            now=lambda: NOW.timestamp())
        res = run_collect(cfg, store, cache, log, client, lambda t: True, dry_run=False, run_id="x1", now=NOW)
        client.close()
    assert res.complete, res.stopped_reason
    assert store.ego_id == "1000"
    users = {u for i in store.interactions() for u in (i["source_user_id"], i["target_user_id"])}
    assert "h:owner" not in users

    run_build(cfg, store, log)
    users = {u for i in store.interactions() for u in (i["source_user_id"], i["target_user_id"])}
    assert "h:alice" not in users and "2001" in users  # alice has an X profile at this time
    assert "h:zed" in users  # no X profile for zed: the provisional ID stays
    assert "h:alice" not in store.profiles() and "h:owner" not in store.profiles()
    assert store.q1("SELECT COUNT(*) AS n FROM nodes WHERE user_id LIKE 'h:owner'")["n"] == 0
