"""Grok x_search adapter (FR-09). A second source next to the official X API, not the default.

What this source is: the xAI Responses API with the ``x_search`` server-side tool.
Grok searches X, reads the posts, and answers. The answer is model output. The
adapter treats it as a claim and keeps only what it can check:

* Each item must name a post URL with a numeric status ID, and that status ID
  must be in the citations that the xAI server returned for the same response.
  Items that fail the check are dropped and counted ("unverified").
* The post time comes from the post ID itself (an X snowflake ID holds its
  creation time), not from the model text.
* The model's copy of the post text is not stored: ``text`` is ``unreadable``.
* Grok gives handles, not numeric user IDs. A handle maps to the numeric ID of a
  profile already in the store (from the X API); otherwise the user ID is the
  provisional ``h:<handle>``, which is clearly not an X ID. The adapter writes a
  profile with only the handle; all other profile fields are ``absent``, so the
  signal engine gives these accounts ``not_scored`` (rule N01).
* Reposts by other accounts are not collected: x_search has no reposter list.

Cost (verified 2026-11-02, docs.x.ai pricing): $5 per 1k posts fetched and $10 per
1k profiles fetched, without de-duplication, plus tokens. There is no request
parameter that caps the number of posts fetched, so the cap guard reserves a
configured maximum for each request and stops before the next request when the
spend plus that reservation goes above the cap. One request can cost more than
its reservation; the summary reports it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.errors

from .. import auth
from ..cache import Cache, request_hash
from ..config import Config
from ..logs import RunLog
from ..model import INTERACTION_OPTIONAL_FIELDS, PROFILE_OPTIONAL_FIELDS, Interaction, Profile, status_for
from ..store import Store, now_iso

ADAPTER = "grok_x_search"
ADAPTER_VERSION = "0.1"
ENDPOINT = "xai x_search (unverified model output)"
KEYRING_USER_XAI = "xai_api_key"
TWEPOCH_MS = 1288834974657
STATUS_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/(?:(?P<handle>[A-Za-z0-9_]{1,15})|i(?:/web)?)/status/(?P<id>\d{5,25})")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
TYPES = ("reply", "quote", "mention", "repost")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["post_url", "author_handle", "type", "target_handle"],
                "properties": {
                    "post_url": {"type": "string"},
                    "author_handle": {"type": "string"},
                    "type": {"type": "string", "enum": list(TYPES)},
                    "target_handle": {"type": "string"},
                },
            },
        }
    },
}

PROMPTS = {
    "inbound": (
        "Search X for every post from {start} to {end} (end exclusive) by any account other than @{h} "
        "that replies to @{h}, quotes a post by @{h}, or mentions @{h}. For each post, give its full post URL "
        "(https://x.com/<author>/status/<id>), the author handle without @, the type (reply if it replies to "
        "@{h}, else quote if it quotes @{h}, else mention), and target_handle \"{h}\". Include only posts you "
        "fetched with the search tool. Do not guess. Return an empty list if you find none."
    ),
    "outbound": (
        "Search X for every post by @{h} from {start} to {end} (end exclusive). For each post that replies to, "
        "quotes, reposts, or mentions another account, give one item for each such account: the post URL "
        "(https://x.com/{h}/status/<id>), author_handle \"{h}\", the type (repost, reply, quote, or mention; a "
        "reply or quote target is not also a mention), and the other account's handle without @ as "
        "target_handle. Include only posts you fetched with the search tool. Do not guess."
    ),
}


class GrokError(RuntimeError):
    pass


def snowflake_time(post_id: str) -> str | None:
    """Creation time encoded in an X post ID (snowflake IDs, from 2010-11-04)."""
    try:
        ms = (int(post_id) >> 22) + TWEPOCH_MS
    except ValueError:
        return None
    if ms <= TWEPOCH_MS:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------- API key
def save_api_key(key: str) -> None:
    auth._check_keyring()
    if not key or not key.strip():
        raise auth.AuthError("Empty key. Nothing saved.")
    try:
        keyring.set_password(auth.KEYRING_SERVICE, KEYRING_USER_XAI, key.strip())
    except keyring.errors.KeyringError as e:
        raise auth.AuthError(f"The keyring write did not succeed ({type(e).__name__}).") from e


def load_api_key(root: str | Path | None = None) -> str:
    """The xAI API key: ``<root>/xai.key.txt`` first (owner decision 2026-11-02), then the keyring.

    The key file is gitignored (``*.key.txt``). The key is never logged or printed.
    """
    if root is not None:
        p = Path(root) / "xai.key.txt"
        if p.is_file():
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
    auth._check_keyring()
    try:
        key = keyring.get_password(auth.KEYRING_SERVICE, KEYRING_USER_XAI)
    except keyring.errors.KeyringError as e:
        raise auth.AuthError(f"The keyring read did not succeed ({type(e).__name__}).") from e
    if not key:
        raise auth.AuthError("No xAI API key: put it in xai.key.txt in the project folder, or run 'visualx auth grok-key'.")
    return key


def delete_api_key() -> bool:
    auth._check_keyring()
    try:
        keyring.delete_password(auth.KEYRING_SERVICE, KEYRING_USER_XAI)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


# ---------------------------------------------------------------- client
class GrokClient:
    def __init__(self, base_url: str = "https://api.x.ai", key_provider: Callable[[], str] = load_api_key,
                 log: RunLog | None = None, http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.key_provider = key_provider
        self.log = log
        self.http = http or httpx.Client(timeout=httpx.Timeout(600.0, connect=15.0))
        self.sleep = sleep
        self.requests_sent = 0

    def close(self) -> None:
        self.http.close()

    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/v1/responses"
        for attempt in range(4):
            try:
                r = self.http.post(url, json=body, headers={"Authorization": f"Bearer {self.key_provider()}"})
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise GrokError(f"network error: {type(e).__name__}") from e
                self.sleep(2 ** (attempt + 1))
                continue
            self.requests_sent += 1
            if self.log:
                self.log.write("xai_call", endpoint="POST /v1/responses", status=r.status_code)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after", "30") or 30)
                if wait > 3600:
                    raise GrokError("xAI rate limit wait is more than 1 hour")
                self.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 3:
                self.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 401 or r.status_code == 403:
                raise GrokError(f"xAI rejected the API key (HTTP {r.status_code}). Run 'visualx auth grok-key'.")
            if r.status_code != 200:
                raise GrokError(f"xAI HTTP {r.status_code}")
            try:
                return r.json()
            except ValueError as e:
                raise GrokError("xAI response is not JSON") from e
        raise GrokError("xAI request did not succeed after retries")


# ---------------------------------------------------------------- parsing
def response_text(resp: dict[str, Any]) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output") or []:
        for c in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


def cited_status_ids(resp: dict[str, Any]) -> set[str]:
    """Status IDs in the server-side citations and url_citation annotations (not in the model's JSON)."""
    urls: list[str] = [u for u in resp.get("citations") or [] if isinstance(u, str)]
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            for a in (c.get("annotations") or []) if isinstance(c, dict) else []:
                if isinstance(a, dict) and a.get("type") == "url_citation" and isinstance(a.get("url"), str):
                    urls.append(a["url"])
    out = set()
    for u in urls:
        m = STATUS_RE.search(u)
        if m:
            out.add(m.group("id"))
    return out


def usage_cost(resp: dict[str, Any], cfg: Config) -> tuple[float, int, int]:
    """(cost USD, posts fetched, users fetched). Prefer the server's own cost figure."""
    u = resp.get("usage") or {}
    det = u.get("server_side_tool_usage_details") or {}
    posts = int(det.get("x_posts_fetched") or 0)
    users = int(det.get("x_users_fetched") or 0)
    ticks = u.get("cost_in_usd_ticks")
    if isinstance(ticks, (int, float)) and ticks > 0:
        return float(ticks) / 1e10, posts, users
    tin = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
    tout = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
    cost = (posts * float(cfg.get("prices.post_read")) + users * float(cfg.get("prices.user_read"))
            + tin / 1e6 * float(cfg.get("grok.input_per_million")) + tout / 1e6 * float(cfg.get("grok.output_per_million")))
    return cost, posts, users


@dataclass
class Verified:
    post_id: str
    author: str
    type: str
    target: str


def verify_items(resp: dict[str, Any], start: date, end: date, ego_handle: str) -> tuple[list[Verified], dict[str, int]]:
    drop = {"not_json": 0, "no_status_id": 0, "not_cited": 0, "bad_handle": 0, "outside_window": 0, "self": 0,
            "not_owner": 0, "author_mismatch": 0}
    try:
        data = json.loads(response_text(resp) or "{}")
    except ValueError:
        drop["not_json"] += 1
        return [], drop
    cited = cited_status_ids(resp)
    out: dict[tuple[str, str, str], Verified] = {}
    for it in data.get("items") or [] if isinstance(data, dict) else []:
        if not isinstance(it, dict):
            continue
        m = STATUS_RE.search(str(it.get("post_url", "")))
        if not m:
            drop["no_status_id"] += 1
            continue
        pid = m.group("id")
        if pid not in cited:
            drop["not_cited"] += 1
            continue
        author = str(it.get("author_handle", "")).lstrip("@")
        target = str(it.get("target_handle", "")).lstrip("@")
        if not HANDLE_RE.match(author) or not HANDLE_RE.match(target) or it.get("type") not in TYPES:
            drop["bad_handle"] += 1
            continue
        if m.group("handle") and m.group("handle").casefold() != author.casefold():
            drop["author_mismatch"] += 1
            continue
        if author.casefold() == target.casefold():
            drop["self"] += 1
            continue
        if author.casefold() != ego_handle.casefold() and target.casefold() != ego_handle.casefold():
            drop["not_owner"] += 1  # neither side is the owner: out of scope for this query
            continue
        t = snowflake_time(pid)
        if t is None or not (start.isoformat() <= t[:10] < end.isoformat()):
            drop["outside_window"] += 1
            continue
        key = (pid, author.casefold(), target.casefold())
        out[key] = Verified(pid, author, it["type"], target)
    return list(out.values()), drop


# ---------------------------------------------------------------- collection
@dataclass
class GrokResult:
    run_id: str
    requests: int = 0
    cost: float = 0.0
    posts_fetched: int = 0
    users_fetched: int = 0
    interactions_added: int = 0
    accepted: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    stopped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    estimate_text: str = ""
    dry_run: bool = False

    @property
    def complete(self) -> bool:
        return self.stopped_reason is None

    def summary_lines(self) -> list[str]:
        lines = [f"Grok x_search collection {self.run_id}{' (dry run, no API calls)' if self.dry_run else ''}",
                 self.estimate_text]
        if not self.dry_run:
            lines += [
                f"  requests: {self.requests}, posts fetched: {self.posts_fetched}, profiles fetched: {self.users_fetched}",
                f"  cost: ${self.cost:.3f}",
                f"  items accepted after the citation check: {self.accepted}; new interactions: {self.interactions_added}",
                "  items dropped: " + (", ".join(f"{k} {v}" for k, v in self.dropped.items() if v) or "none"),
            ]
        lines += [f"  warning: {w}" for w in self.warnings]
        if self.stopped_reason:
            lines.append(f"  stopped: {self.stopped_reason}")
        return [x for x in lines if x]


def _chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def _user_id(store: Store, handle: str, ego_handle: str, ego_id: str, known: dict[str, str]) -> str:
    h = handle.casefold()
    if h == ego_handle.casefold():
        return ego_id
    return known.get(h) or f"h:{h}"


def run_grok_collect(cfg: Config, store: Store, cache: Cache, log: RunLog, client: GrokClient | None,
                     approve: Callable[[str], bool], dry_run: bool, run_id: str,
                     now: datetime | None = None) -> GrokResult:
    res = GrokResult(run_id=run_id, dry_run=dry_run)
    handle = str(cfg.get("owner.handle", "") or "").lstrip("@")
    if not HANDLE_RE.match(handle):
        res.stopped_reason = "config owner.handle is not set; the Grok source needs the owner handle"
        return res
    now = now or datetime.now(UTC)
    end = (now + timedelta(days=1)).date()
    start = (now - timedelta(days=int(cfg.get("collect.window_days", 90)))).date()
    chunks = _chunks(start, end, int(cfg.get("grok.chunk_days", 7)))
    reserve = float(cfg.get("grok.reserve_per_request_usd", 1.0))
    cap = float(cfg.get("collect.cap_usd"))
    n_req = 2 * len(chunks)
    res.estimate_text = (
        f"Grok x_search plan for @{handle}, {start} to {end} (exclusive): {len(chunks)} windows x 2 queries "
        f"(inbound replies/quotes/mentions, outbound posts) = {n_req} requests to {cfg.get('grok.model')}.\n"
        f"  Prices: ${cfg.get('prices.post_read'):.3f} per post fetched, ${cfg.get('prices.user_read'):.3f} per profile "
        f"fetched (no de-duplication; parent and quoted posts count), plus tokens at "
        f"${cfg.get('grok.input_per_million')}/${cfg.get('grok.output_per_million')} per 1M in/out.\n"
        f"  The API has no cap on posts fetched per request. Reservation: ${reserve:.2f} per request, "
        f"so the planned maximum is ${n_req * reserve:.2f}; one request can cost more. Run cap: ${cap:.2f}.\n"
        f"  Output is model text. Only items whose post ID is in the server citations are kept. "
        f"Reposts by other accounts are not available from x_search."
    )
    if dry_run:
        return res
    if client is None:
        raise GrokError("no Grok client")
    if not approve(res.estimate_text):
        res.stopped_reason = "declined"
        return res

    ego_id = store.ego_id
    if ego_id is None:
        ego_id = f"h:{handle.casefold()}"
        store.set_meta("ego_user_id", ego_id)
        store.set_meta("ego_handle", handle)
    elif (store.get_meta("ego_handle") or "").casefold() not in ("", handle.casefold()):
        res.stopped_reason = "the store belongs to a different owner handle (FR-54); use a new data folder"
        return res
    known = {(p["handle"] or "").casefold(): uid for uid, p in store.profiles().items() if p.get("handle")}
    sid = store.begin_stage("collect", run_id, {}, cfg.hash())
    totals = dict.fromkeys(("not_json", "no_status_id", "not_cited", "bad_handle", "outside_window", "self", "not_owner",
                            "author_mismatch"), 0)
    try:
        for a, b in chunks:
            for kind in ("inbound", "outbound"):
                spent = store.ledger_total(run_id)
                if spent + reserve > cap:
                    res.stopped_reason = f"spend cap: ${spent:.3f} spent + ${reserve:.2f} reservation > ${cap:.2f}"
                    raise StopIteration
                tool: dict[str, Any] = {"type": "x_search", "from_date": a.isoformat(), "to_date": b.isoformat()}
                tool["allowed_x_handles" if kind == "outbound" else "excluded_x_handles"] = [handle]
                body = {
                    "model": cfg.get("grok.model"),
                    "input": [{"role": "user", "content": PROMPTS[kind].format(h=handle, start=a, end=b)}],
                    "tools": [tool],
                    "max_turns": int(cfg.get("grok.max_turns", 3)),
                    "text": {"format": {"type": "json_schema", "name": "x_interactions", "schema": SCHEMA, "strict": True}},
                }
                rh = request_hash("xai /v1/responses", body)
                resp = cache.get(rh)
                cached = resp is not None
                if not cached:
                    resp = client.respond(body)
                    cache.put(rh, "xai /v1/responses", resp)
                cost, posts, users = (0.0, 0, 0) if cached else usage_cost(resp, cfg)
                items, drop = verify_items(resp, a, b, handle)
                for k, v in drop.items():
                    totals[k] += v
                ints, profs = [], {}
                obs = now_iso()
                for v in items:
                    src = _user_id(store, v.author, handle, ego_id, known)
                    tgt = _user_id(store, v.target, handle, ego_id, known)
                    vals = {"source_post_id": v.post_id, "target_post_id": None,
                            "interaction_created_at": snowflake_time(v.post_id), "conversation_id": None,
                            "text": None, "urls": None}
                    fs = status_for(vals, INTERACTION_OPTIONAL_FIELDS, unreadable=frozenset({"text"}))
                    ints.append(Interaction(type=v.type, source_user_id=src, target_user_id=tgt, observed_at=obs,
                                            source_endpoint=ENDPOINT, adapter=ADAPTER, adapter_version=ADAPTER_VERSION,
                                            run_id=run_id, field_status=fs, raw_request_hash=rh, **vals))
                    for hh, uid in ((v.author, src), (v.target, tgt)):
                        if uid.startswith("h:") and uid not in profs:
                            pv = {"handle": hh}
                            profs[uid] = Profile(user_id=uid, fetched_at=obs, source_endpoint=ENDPOINT, adapter=ADAPTER,
                                                 adapter_version=ADAPTER_VERSION,
                                                 field_status=status_for(pv, PROFILE_OPTIONAL_FIELDS), handle=hh)
                existing = set(store.profiles())
                with store.tx() as c:
                    if not cached:
                        c.execute(
                            "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,response_code,at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (run_id, 1 if kind == "inbound" else 2, "xai x_search", "grok", posts + users, 0.0, cost, reserve, 200, obs),
                        )
                    res.interactions_added += store.put_interactions(ints, conn=c)
                    store.put_profiles([p for uid, p in profs.items() if uid not in existing], conn=c)
                res.requests += 0 if cached else 1
                res.cost += cost
                res.posts_fetched += posts
                res.users_fetched += users
                res.accepted += len(items)
                if cost > reserve:
                    res.warnings.append(f"one request cost ${cost:.3f}, more than the ${reserve:.2f} reservation")
                log.write("grok_window", kind=kind, accepted=len(items), dropped=sum(drop.values()), cost=round(cost, 6))
                if store.ledger_total(run_id) > cap:
                    res.stopped_reason = f"spend cap passed after a request: ${store.ledger_total(run_id):.3f} > ${cap:.2f}"
                    raise StopIteration
    except StopIteration:
        pass
    except (GrokError, auth.AuthError) as e:
        res.stopped_reason = str(e)
    res.dropped = totals
    if totals["not_cited"]:
        res.warnings.append(f"{totals['not_cited']} items named a post that is not in the server citations; they were dropped")
    store.finish_stage(sid, {"source": ADAPTER, "window_start": start.isoformat(), "window_end": end.isoformat(),
                             "window_days": int(cfg.get("collect.window_days", 90)), "complete": res.complete,
                             "cost_usd": round(res.cost, 6), "requests": res.requests, "accepted": res.accepted,
                             "dropped": totals})
    return res
