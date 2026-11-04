"""A local HTTP server on 127.0.0.1 that plays X API v2 responses from the hand-made fixtures.

The tests point ``XApiClient`` and the auth functions at this server. No test
contacts api.x.com or x.com.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "handmade"


def fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)


Reply = tuple[int, dict[str, str], Any]


def default_router(method: str, path: str, query: dict[str, str], form: dict[str, str]) -> Reply | None:
    if method != "GET":
        return None
    if path == "/2/users/me":
        return 200, {}, fixture("users_me.json")
    if path == "/2/users/1000/tweets":
        name = "owner_tweets_p2.json" if query.get("pagination_token") == "p2token" else "owner_tweets_p1.json"
        return 200, {}, fixture(name)
    if path == "/2/users/1000/mentions":
        return 200, {}, fixture("mentions_p1.json")
    if path == "/2/tweets/3001/quote_tweets":
        return 200, {}, fixture("quote_tweets_3001.json")
    if path == "/2/tweets/3001/retweeted_by":
        return 200, {}, fixture("retweeted_by_3001.json")
    # Seed timelines (FR-07, plan step 6). Bob (2002) has two pages and a page for since_id
    # requests; Erin (2005) has one page; each other account has no posts in the window.
    if path == "/2/users/2002/tweets":
        if "since_id" in query:
            return 200, {}, fixture("seed_tweets_2002_since.json")
        name = "seed_tweets_2002_p2.json" if query.get("pagination_token") == "s2002p2" else "seed_tweets_2002.json"
        return 200, {}, fixture(name)
    if path == "/2/users/2005/tweets":
        if "since_id" in query:
            return 200, {}, {"meta": {"result_count": 0}}
        return 200, {}, fixture("seed_tweets_2005.json")
    if path.startswith("/2/users/") and path.endswith("/tweets"):
        return 200, {}, {"meta": {"result_count": 0}}
    if path == "/2/users":
        ids = set(query.get("ids", "").split(","))
        body = fixture("users_lookup.json")
        out: dict[str, Any] = {}
        data = [u for u in body["data"] if u["id"] in ids]
        errs = [e for e in body["errors"] if e["resource_id"] in ids]
        if data:
            out["data"] = data
        if errs:
            out["errors"] = errs
        return 200, {}, out
    return None


class XApiTestServer:
    """Serve fixture responses. ``once`` queues one-time replies for a path (for 429, 5xx, 401)."""

    def __init__(self, router: Callable[[str, str, dict[str, str], dict[str, str]], Reply | None] = default_router):
        self.router = router
        self.requests: list[dict[str, Any]] = []
        self._once: dict[tuple[str, str], list[Reply]] = {}
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

            def _serve(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                form: dict[str, str] = {}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    raw = self.rfile.read(n).decode("utf-8")
                    form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                with outer._lock:
                    outer.requests.append(
                        {"method": self.command, "path": parts.path, "query": query, "form": form,
                         "authorization": self.headers.get("Authorization")}
                    )
                    queued = outer._once.get((self.command, parts.path))
                    reply = queued.pop(0) if queued else None
                if reply is None:
                    reply = outer.router(self.command, parts.path, query, form)
                if reply is None:
                    reply = (404, {}, {"title": "Not Found Error"})
                status, headers, body = reply
                data = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = _serve
            do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def once(self, method: str, path: str, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        with self._lock:
            self._once.setdefault((method, path), []).append((status, headers or {}, body if body is not None else {}))

    def count(self, path: str, method: str = "GET") -> int:
        with self._lock:
            return sum(1 for r in self.requests if r["path"] == path and r["method"] == method)

    def __enter__(self) -> XApiTestServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
