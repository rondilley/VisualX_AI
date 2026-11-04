"""Slice 0a: paid X API probe (not product code). Run it ONLY after the owner approves the cost.

    .venv/Scripts/python tools/api_probe.py            # shows the estimate, asks, then calls
    .venv/Scripts/python tools/api_probe.py --estimate # shows the estimate only, no call

Calls (each with max_results = --page, default 20):
  0. GET /2/users/me                     (1 user read)
  1. GET /2/users/:id/tweets             (owner posts, 1 page)
  2. GET /2/users/:id/mentions           (1 page)
  3. GET /2/tweets/:id/quote_tweets      (1 page, first owner post with quote_count > 0)
  4. GET /2/tweets/:id/retweeted_by      (1 page, first owner post with retweet_count > 0)
  5. GET /2/users?ids=...                (1 batch of up to --page user IDs from steps 1-4)

Outputs:
  data/probe/<timestamp>/raw/*.json       full responses (third-party data; gitignored folder)
  tests/fixtures/recorded/*.json          the same responses with IDs, names, handles, text, and
                                          URLs replaced by stable labels; keys and shapes unchanged
  background/<date>_api_probe.md          calls, response field inventory, mapping proof, cost

Before and after the run, record the X Console consumption by hand in the .md file.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from visualx import auth  # noqa: E402
from visualx.collect.xapi import (  # noqa: E402
    POST_PARAMS,
    USER_PARAMS,
    XApiClient,
    XApiError,
    endpoint_template,
    map_post,
    map_retweeted_by,
    map_user,
)
from visualx.config import load  # noqa: E402
from visualx.logs import RunLog, load_hmac_key  # noqa: E402
from visualx.store import Store, now_iso  # noqa: E402

# The billing class of each probe call, for the cost ledger (review 20261103, A-30).
CALL_CLASS = {"users_me": "user_read", "owner_tweets": "owned_read", "mentions": "owned_read",
              "quote_tweets": "post_read", "retweeted_by": "user_read", "users_lookup": "user_read"}

CAP = 5.00


def estimate(cfg, page: int) -> list[tuple[str, int, float, float]]:
    p = cfg.raw["prices"]
    own = p["owned_read"] if cfg.get("billing_assumptions.owned_reads_with_user_token") else p["post_read"]
    rows = [
        ("users/me", 1, p["user_read"], p["user_read"]),
        ("owner posts", page, own, p["post_read"]),
        ("mentions", page, own, p["post_read"]),
        ("quote_tweets", page, p["post_read"], p["post_read"]),
        ("retweeted_by", page, p["user_read"], p["user_read"]),
        ("users lookup", page, p["user_read"], p["user_read"]),
    ]
    return rows


class Pseudo:
    """Stable labels for IDs, handles, names, and text, so fixtures hold no third-party data."""

    TEXT_KEYS = {"text", "description", "name", "location", "username", "url", "expanded_url",
                 "display_url", "unwound_url", "title", "profile_image_url", "pinned_tweet_id", "tag"}
    ID_KEYS = {"id", "author_id", "in_reply_to_user_id", "conversation_id"}

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.n = 0

    def pid(self, v: str) -> str:
        if v not in self.ids:
            self.ids[v] = str(1000000 + len(self.ids))
        return self.ids[v]

    def walk(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key in self.ID_KEYS or key == "next_token" or key.endswith("_id") or key in ("newest_id", "oldest_id"):
                return self.pid(obj)
            if key in self.TEXT_KEYS:
                self.n += 1
                return "" if obj == "" else f"{key}_{self.n}"
        return obj


def field_inventory(body: dict[str, Any]) -> dict[str, list[str]]:
    inv: dict[str, set[str]] = {}

    def add(section: str, items: Any) -> None:
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict):
                inv.setdefault(section, set()).update(it.keys())
                pm = it.get("public_metrics")
                if isinstance(pm, dict):
                    inv.setdefault(section + ".public_metrics", set()).update(pm.keys())

    if "data" in body:
        add("data", body["data"])
    for k, v in (body.get("includes") or {}).items():
        add(f"includes.{k}", v)
    return {k: sorted(v) for k, v in inv.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config"))
    ap.add_argument("--page", type=int, default=20)
    ap.add_argument("--estimate", action="store_true")
    a = ap.parse_args()
    cfg = load(a.config)
    page = max(5, min(a.page, 100))
    rows = estimate(cfg, page)
    worst = sum(n * alt for _, n, _, alt in rows)
    best = sum(n * u for _, n, u, _ in rows)
    print("Slice 0a probe estimate (maximum resources per call):")
    for name, n, u, alt in rows:
        print(f"  {name:14s} {n:4d} x ${u:.3f} = ${n * u:.3f}   (if the billing assumption is wrong: ${n * alt:.3f})")
    print(f"  Total: ${best:.2f} with the configured assumptions, ${worst:.2f} if OQ-01/OQ-02 are wrong. Cap ${CAP:.2f}.")
    print("  Expansion objects ('includes') can add cost if X bills them (OQ-01).")
    if a.estimate:
        return 0
    if worst > CAP:
        print("The worst case is above the cap. Use a smaller --page.")
        return 1
    if input("Type 'yes' to make these paid calls: ").strip().lower() != "yes":
        print("No call made.")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = cfg.data_dir / "probe" / stamp
    (out / "raw").mkdir(parents=True, exist_ok=True)
    fx = ROOT / "tests" / "fixtures" / "recorded"
    fx.mkdir(parents=True, exist_ok=True)
    log = RunLog(out / "log.jsonl", load_hmac_key(cfg.data_dir))
    client = XApiClient(token_provider=lambda: auth.get_access_token(cfg),
                        on_refresh=lambda: auth.refresh_access_token(cfg), log=log)
    pseudo = Pseudo()
    calls: list[dict[str, Any]] = []
    store = Store(cfg.store_path)
    run_id = f"probe_{stamp}"

    def call(name: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # The paid calls of the probe go into the cost ledger too, with a reservation
        # before the call, as in the collect plan (A-30).
        cls = CALL_CLASS[name]
        price = float(cfg.raw["prices"][cls])
        most = int(params.get("max_results") or len(str(params.get("ids", "")).split(",")) or 1)
        lid = store.conn.execute(
            "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,"
            "response_code,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, 0, endpoint_template(path)[0], cls, 0, price, most * price, most * price, None, now_iso()),
        ).lastrowid
        try:
            body, headers = client.get(path, params)
        except XApiError as e:
            if e.status is not None:
                store.conn.execute("UPDATE cost_ledger SET cost=0, response_code=? WHERE id=?", (e.status, lid))
            calls.append({"name": name, "path": path, "error": str(e)})
            print(f"  {name}: error {e}")
            return {}
        (out / "raw" / f"{name}.json").write_text(json.dumps(body, indent=1, ensure_ascii=False), encoding="utf-8")
        (fx / f"{name}.json").write_text(json.dumps(pseudo.walk(body), indent=1), encoding="utf-8")
        count = len(body.get("data") or []) if isinstance(body.get("data"), list) else (1 if body.get("data") else 0)
        calls.append({"name": name, "path": path, "resources": count, "fields": field_inventory(body),
                      "errors": [e.get("title") for e in body.get("errors", [])]})
        store.conn.execute("UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?",
                           (count, count * price, lid))
        print(f"  {name}: {count} objects")
        return body

    try:
        me = call("users_me", "/2/users/me", dict(USER_PARAMS))
        ego = (me.get("data") or {}).get("id")
        if not ego:
            print("No owner ID. Stop.")
            return 1
        start = (datetime.now(UTC).replace(microsecond=0)).isoformat().replace("+00:00", "Z")
        posts = call("owner_tweets", f"/2/users/{ego}/tweets", {**POST_PARAMS, "max_results": max(page, 5)})
        mentions = call("mentions", f"/2/users/{ego}/mentions", {**POST_PARAMS, "max_results": max(page, 5)})
        plist = posts.get("data") or []
        q = next((p for p in plist if (p.get("public_metrics") or {}).get("quote_count", 0) > 0), None)
        r = next((p for p in plist if (p.get("public_metrics") or {}).get("retweet_count", 0) > 0), None)
        quotes = call("quote_tweets", f"/2/tweets/{q['id']}/quote_tweets", {**POST_PARAMS, "max_results": max(page, 10)}) if q else {}
        rts = call("retweeted_by", f"/2/tweets/{r['id']}/retweeted_by", {**USER_PARAMS, "max_results": max(page, 1)}) if r else {}

        mapping: list[str] = []
        users: set[str] = set()
        for name, body in (("owner_tweets", posts), ("mentions", mentions), ("quote_tweets", quotes)):
            for p in body.get("data") or []:
                for it in map_post(p, ego, name, "probe", includes=body.get("includes")):
                    users.update({it.source_user_id, it.target_user_id})
                    missing = sorted(k for k, v in it.field_status.items() if v != "present")
                    mapping.append(f"| {name} | {it.type} | {', '.join(missing) or '(none)'} |")
        if r:
            for it in map_retweeted_by(rts.get("data") or [], r["id"], ego, "retweeted_by", "probe"):
                users.add(it.source_user_id)
                missing = sorted(k for k, v in it.field_status.items() if v != "present")
                mapping.append(f"| retweeted_by | repost | {', '.join(missing)} |")
        users.discard(ego)
        batch = sorted(users)[:page]
        lookup = call("users_lookup", "/2/users", {**USER_PARAMS, "ids": ",".join(batch)}) if batch else {}
        prof_missing: dict[str, int] = {}
        for u in lookup.get("data") or []:
            for k, v in map_user(u, "users_lookup").field_status.items():
                if v != "present":
                    prof_missing[k] = prof_missing.get(k, 0) + 1
    finally:
        client.close()

    md = ROOT / "background" / f"{datetime.now(UTC).strftime('%Y-%m-%d')}_api_probe.md"
    lines = [
        "# Slice 0a: X API probe", "",
        f"- Run: {stamp} (UTC). Page size: {page}. Raw responses: `{out.relative_to(cfg.root) if out.is_relative_to(cfg.root) else out}` (private).",
        f"- Estimate before the run: ${best:.2f} configured, ${worst:.2f} at the maximum. Cap ${CAP:.2f}.",
        "- X Console consumption before the run: (owner: fill in)",
        "- X Console consumption after the run: (owner: fill in)",
        "", "## Calls", "", "| Call | Path | Objects | Errors |", "|---|---|---|---|",
    ]
    for c in calls:
        lines.append(f"| {c['name']} | `{c['path'].replace(str(ego), ':id')}` | {c.get('resources', '-')} | {c.get('error') or ', '.join(filter(None, c.get('errors', []))) or '-'} |")
    lines += ["", "## Response fields", ""]
    for c in calls:
        for sec, keys in (c.get("fields") or {}).items():
            lines.append(f"- {c['name']} `{sec}`: " + ", ".join(f"`{k}`" for k in keys))
    lines += ["", "## Mapping proof (fields with the `absent` status, for each interaction)", "",
              "| Endpoint | Type | Fields with the `absent` status |", "|---|---|---|", *sorted(set(mapping)), "",
              "## Profile fields with the `absent` status (count of users)", "",
              *(f"- {k}: {v}" for k, v in sorted(prof_missing.items())), "",
              "## Answers", "", "- OQ-01 (expansion billing): compare the console numbers with the object counts above.",
              "- OQ-02 (Owned Reads with a user token): same comparison for owner posts and mentions.",
              "- `verified_type` readable for other users: see the profile fields list.", ""]
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
