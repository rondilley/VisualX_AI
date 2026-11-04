"""Grok x_search adapter (FR-09) against a real local HTTP server. Hand-made xAI responses, no network."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import make_env
from visualx.analyze import run_analyze
from visualx.build import run_build
from visualx.collect.grok import (
    ENDPOINT,
    TWEPOCH_MS,
    GrokClient,
    run_grok_collect,
    snowflake_time,
    verify_items,
)
from visualx.render.render import run_render
from visualx.score.engine import run_score

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def sid(when: datetime, seq: int = 0) -> str:
    return str(((int(when.timestamp() * 1000) - TWEPOCH_MS) << 22) + seq)


IN_POST = sid(datetime(2026, 10, 30, 9, 0, tzinfo=UTC), 1)
OUT_POST = sid(datetime(2026, 10, 31, 9, 0, tzinfo=UTC), 2)
FAKE_POST = sid(datetime(2026, 10, 30, 10, 0, tzinfo=UTC), 3)


def xai_response(items: list[dict], cited: list[str], posts: int = 10, users: int = 2, ticks: int | None = None) -> dict:
    usage = {"input_tokens": 1000, "output_tokens": 200,
             "server_side_tool_usage_details": {"x_search_calls": 1, "x_posts_fetched": posts, "x_users_fetched": users}}
    if ticks is not None:
        usage["cost_in_usd_ticks"] = ticks
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"items": items}),
                                                     "annotations": []}]}],
        "citations": cited,
        "usage": usage,
    }


def router(body: dict) -> dict:
    tool = body["tools"][0]
    if "allowed_x_handles" in tool:  # outbound: posts by the owner
        if tool["from_date"] <= "2026-10-31" < tool["to_date"]:
            return xai_response(
                [{"post_url": f"https://x.com/Ron_Dilley/status/{OUT_POST}", "author_handle": "Ron_Dilley",
                  "type": "reply", "target_handle": "alice"}],
                [f"https://x.com/Ron_Dilley/status/{OUT_POST}"])
        return xai_response([], [])
    if tool["from_date"] <= "2026-10-30" < tool["to_date"]:
        return xai_response(
            [{"post_url": f"https://x.com/bob/status/{IN_POST}", "author_handle": "bob", "type": "mention",
              "target_handle": "Ron_Dilley"},
             # not in the citations: the model made it up or did not fetch it
             {"post_url": f"https://x.com/eve/status/{FAKE_POST}", "author_handle": "eve", "type": "reply",
              "target_handle": "Ron_Dilley"}],
            [f"https://x.com/i/status/{IN_POST}"])
    return xai_response([], [])


class Server:
    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return None

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n))
                outer.requests.append({"path": self.path, "auth": self.headers.get("authorization"), "body": body})
                out = json.dumps(router(body)).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


def _env(tmp_path, cap=5.0, reserve=1.0):
    extra = (f'[owner]\nhandle = "Ron_Dilley"\n[collect]\nwindow_days = 7\ncap_usd = {cap}\n'
             f'[grok]\nreserve_per_request_usd = {reserve}\n')
    cfg, store, cache, log = make_env(tmp_path)
    (tmp_path / "config" / "visualx.local.toml").write_text(
        (tmp_path / "config" / "visualx.local.toml").read_text(encoding="utf-8").replace('[owner]\nhandle = ""\n', "") + extra,
        encoding="utf-8")
    from visualx.config import load
    cfg = load(tmp_path / "config", root=tmp_path)
    return cfg, store, cache, log


def test_snowflake_time():
    assert snowflake_time(IN_POST) == "2026-10-30T09:00:00.000Z"
    assert snowflake_time("abc") is None


def test_verify_drops_uncited_and_checks_author():
    resp = router({"tools": [{"type": "x_search", "excluded_x_handles": ["Ron_Dilley"],
                              "from_date": "2026-10-27", "to_date": "2026-11-03"}]})
    items, drop = verify_items(resp, date(2026, 10, 27), date(2026, 11, 3), "Ron_Dilley")
    assert [(v.post_id, v.author, v.type) for v in items] == [(IN_POST, "bob", "mention")]
    assert drop["not_cited"] == 1
    resp2 = xai_response([{"post_url": f"https://x.com/mallory/status/{IN_POST}", "author_handle": "bob",
                           "type": "reply", "target_handle": "Ron_Dilley"}], [f"https://x.com/i/status/{IN_POST}"])
    items2, drop2 = verify_items(resp2, date(2026, 10, 27), date(2026, 11, 3), "Ron_Dilley")
    assert items2 == [] and drop2["author_mismatch"] == 1
    items3, drop3 = verify_items(resp, date(2026, 10, 1), date(2026, 10, 2), "Ron_Dilley")
    assert items3 == [] and drop3["outside_window"] == 1


def test_dry_run_makes_no_request(tmp_path):
    cfg, store, cache, log = _env(tmp_path)
    with Server() as srv:
        res = run_grok_collect(cfg, store, cache, log, None, lambda t: True, True, "g0", now=NOW)
        assert srv.requests == []
    assert res.dry_run and "2 queries" in res.estimate_text and "no cap on posts fetched" in res.estimate_text


def test_collect_then_pipeline(tmp_path):
    cfg, store, cache, log = _env(tmp_path)
    with Server() as srv:
        client = GrokClient(srv.url, key_provider=lambda: "k-test", log=log)
        res = run_grok_collect(cfg, store, cache, log, client, lambda t: True, False, "g1", now=NOW)
        client.close()
        assert all(r["auth"] == "Bearer k-test" for r in srv.requests)
        assert all(r["body"]["text"]["format"]["type"] == "json_schema" for r in srv.requests)
    assert res.complete, res.stopped_reason
    assert res.requests == 4  # 8 days -> 2 windows x 2 queries
    assert res.accepted == 2 and res.dropped["not_cited"] == 1
    rows = {(r["type"], r["source_user_id"], r["target_user_id"]): r for r in store.interactions()}
    assert set(rows) == {("mention", "h:bob", "h:ron_dilley"), ("reply", "h:ron_dilley", "h:alice")}
    r = rows[("mention", "h:bob", "h:ron_dilley")]
    assert r["source_endpoint"] == ENDPOINT and r["adapter"] == "grok_x_search"
    assert r["field_status"]["text"] == "unreadable" and r["text"] is None
    assert r["interaction_created_at"] == "2026-10-30T09:00:00.000Z"
    # cost: 4 requests x (10 posts x 0.005 + 2 users x 0.01 + 1000/1e6 x 2 + 200/1e6 x 6)
    per = 10 * 0.005 + 2 * 0.01 + 0.002 + 0.0012
    assert abs(res.cost - 4 * per) < 1e-9 and abs(store.ledger_total("g1") - 4 * per) < 1e-9
    assert store.profiles()["h:bob"]["field_status"]["created_at"] == "absent"

    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    run_score(cfg, store, log)
    states = {r["user_id"]: r["state"] for r in store.q("SELECT user_id, state FROM node_states")}
    assert states["h:bob"] == "not_scored"  # N01: no profile data
    html = run_render(cfg, store, log)
    assert html.exists()

    # A second run reads the cache and pays nothing.
    with Server() as srv:
        client = GrokClient(srv.url, key_provider=lambda: "k-test", log=log)
        res2 = run_grok_collect(cfg, store, cache, log, client, lambda t: True, False, "g2", now=NOW)
        client.close()
        assert srv.requests == []
    assert res2.cost == 0 and res2.interactions_added == 0


def test_cap_stops_before_next_request(tmp_path):
    cfg, store, cache, log = _env(tmp_path, cap=1.05, reserve=1.0)
    with Server() as srv:
        client = GrokClient(srv.url, key_provider=lambda: "k", log=log)
        res = run_grok_collect(cfg, store, cache, log, client, lambda t: True, False, "g3", now=NOW)
        client.close()
        assert len(srv.requests) == 1
    assert "spend cap" in res.stopped_reason
    assert store.ledger_total("g3") <= 1.05


def test_declined_and_missing_handle(tmp_path):
    cfg, store, cache, log = _env(tmp_path)
    with Server() as srv:
        client = GrokClient(srv.url, key_provider=lambda: "k", log=log)
        res = run_grok_collect(cfg, store, cache, log, client, lambda t: False, False, "g4", now=NOW)
        client.close()
        assert srv.requests == []
    assert res.stopped_reason == "declined"
    cfg.raw["owner"]["handle"] = ""
    res = run_grok_collect(cfg, store, cache, log, None, lambda t: True, False, "g5", now=NOW)
    assert "owner.handle" in res.stopped_reason


def test_key_file_first_then_keyring(tmp_path):
    from visualx.collect.grok import load_api_key

    (tmp_path / "xai.key.txt").write_text("  key-from-file \n", encoding="utf-8")
    assert load_api_key(tmp_path) == "key-from-file"
