"""Sweep for stored posts that are no longer on X (DR-05, ARCHITECTURE AD-10).

The X Developer Policy says that stored content must stay in sync with X: a
post that is deleted on X must also go from the local copy. The sweep lists
every post ID that the store holds (``posts.post_id``,
``interactions.source_post_id``, ``interactions.target_post_id``) and checks
them with ``GET /2/tweets?ids=`` (at most 100 IDs for each call).

Classification of each requested ID:

- In ``data``: the post is on X. It stays.
- In ``errors`` with the type ``.../resource-not-found`` (title
  ``Not Found Error``): the post is gone. It is deleted locally with
  ``delete.delete_ids(kind="post")``, which covers the store, the raw cache,
  and the rendered HTML files (AD-10, invariant 11).
- Any other error (for example ``Authorization Error`` for a protected or
  suspended account), or no answer for the ID: the result is ambiguous. The
  post stays, and the summary counts the reason. The sweep never deletes on an
  ambiguous answer.

Cost control is the same as in the collector (AD-04, invariant 3): the sweep
shows an estimate and asks for approval before the first paid call, confirms
the owner account with ``GET /2/users/me`` (FR-54), reserves the maximum cost
of each call in ``cost_ledger`` before the call, and stops before a call when
the run spend plus the reservation is above the cap. The estimate and the
ledger count each requested ID as one billed post read (worst case): X can
bill only the posts that it returns.

Order: oldest first (the lowest X post ID; X post IDs increase with time).
Old posts are the most likely to be deleted, and the newest posts were just
seen by ``collect``. With ``--limit N``, the meta key ``sweep_cursor_post``
keeps the last checked post that is still in the store, and the next sweep
starts after it. Thus repeated limited sweeps cover all stored posts in turn.
A sweep that checks all IDs clears the cursor.

The sweep sends only the ``ids`` parameter. It stores nothing from the
response and writes no raw cache file, so a check adds no new third-party
data to the data folder.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..auth import AuthError
from ..cache import Cache
from ..config import Config
from ..delete import DeleteReport, delete_ids
from ..logs import RunLog
from ..store import Store, new_id, now_iso
from .plan import EPS, Billing
from .xapi import USER_PARAMS, XApiClient, XApiError

STEP = 7  # ledger step number of the sweep (the collector uses 0 to 6)
OWNER_STEP = 0  # ledger step of GET /2/users/me, as in the collector
ENDPOINT = "/2/tweets"
OWNER_ENDPOINT = "/2/users/me"
IDS_PER_LOOKUP = 100
META_CURSOR = "sweep_cursor_post"
NOT_FOUND_TYPE = "/problems/resource-not-found"
NOT_FOUND_TITLE = "Not Found Error"


class _Stop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class SweepResult:
    run_id: str
    dry_run: bool
    cap_usd: float
    local_ids: int = 0
    in_posts: int = 0
    selected: int = 0
    calls_planned: int = 0
    est_cost: float = 0.0
    estimate: str = ""
    checked: int = 0
    on_x: int = 0
    gone: list[str] = field(default_factory=list)
    kept: Counter[str] = field(default_factory=Counter)
    calls: int = 0
    requests_sent: int = 0
    total_cost: float = 0.0
    stopped_reason: str | None = None
    approved: bool | None = None
    delete_report: DeleteReport | None = None
    stage_run_id: str | None = None

    @property
    def complete(self) -> bool:
        return not self.dry_run and self.stopped_reason is None

    def summary_lines(self) -> list[str]:
        lines = [f"Sweep {self.run_id}{' (dry run, no API calls)' if self.dry_run else ''}"]
        if self.dry_run:
            lines.append(self.estimate)
            return lines
        lines.append(f"  posts checked:          {self.checked} of {self.selected} selected ({self.local_ids} post IDs in the store)")
        lines.append(f"  still on X:             {self.on_x}")
        lines.append(f"  gone and deleted:       {len(self.gone)}")
        kept = sum(self.kept.values())
        lines.append(f"  not deleted (unclear):  {kept}" + (
            " (" + ", ".join(f"{k} {v}" for k, v in sorted(self.kept.items())) + ")" if kept else ""))
        rep = self.delete_report
        if rep is not None:
            lines.append(
                f"  deletion:               {rep.store_rows} store rows, {rep.redacted_cells} redacted cells, "
                f"{rep.cache_files_scrubbed} cache files rewritten, {len(rep.renders_deleted)} HTML files deleted"
                + (f", new render {rep.rerendered}" if rep.rerendered else "")
            )
        lines.append(f"  calls to {ENDPOINT}:      {self.calls} of {self.calls_planned}")
        lines.append(f"  logged cost (USD):      {self.total_cost:.4f} of cap {self.cap_usd:.2f}")
        if self.stopped_reason:
            lines.append(f"Stopped: {self.stopped_reason}")
        return lines


def local_post_ids(store: Store) -> tuple[list[str], int]:
    """All post IDs in the store, oldest first, and the count that are in the ``posts`` table.

    Only numeric IDs are X post IDs. A redacted value (``[deleted]``) or a
    provisional ID is not sent to X.
    """
    rows = store.q(
        "SELECT post_id FROM posts UNION SELECT source_post_id FROM interactions WHERE source_post_id IS NOT NULL "
        "UNION SELECT target_post_id FROM interactions WHERE target_post_id IS NOT NULL"
    )
    ids = {str(r[0]) for r in rows if r[0] is not None and str(r[0]).isdigit()}
    in_posts = {str(r[0]) for r in store.q("SELECT post_id FROM posts")} & ids
    return sorted(ids, key=int), len(in_posts)


def select_ids(ids: list[str], cursor: str | None, limit: int | None) -> list[str]:
    """Rotate ``ids`` (oldest first) to start after ``cursor``, then take at most ``limit``."""
    if cursor and cursor.isdigit():
        c = int(cursor)
        ids = [i for i in ids if int(i) > c] + [i for i in ids if int(i) <= c]
    return list(ids) if limit is None else list(ids[:limit])


def classify(body: dict[str, Any], batch: list[str]) -> tuple[set[str], set[str], dict[str, str]]:
    """Return (on X, gone, unclear ID -> reason) for one ``GET /2/tweets?ids=`` response."""
    wanted = set(batch)
    on_x: set[str] = set()
    for p in body.get("data") or []:
        if isinstance(p, dict) and str(p.get("id", "")) in wanted:
            on_x.add(str(p["id"]))
    gone: set[str] = set()
    unclear: dict[str, str] = {}
    for e in body.get("errors") or []:
        if not isinstance(e, dict):
            continue
        rid = str(e.get("resource_id") or e.get("value") or "")
        if rid not in wanted or rid in on_x:
            continue
        etype = str(e.get("type") or "")
        title = str(e.get("title") or "")
        rtype = e.get("resource_type")
        if (etype.endswith(NOT_FOUND_TYPE) or title == NOT_FOUND_TITLE) and rtype in (None, "tweet"):
            gone.add(rid)
            unclear.pop(rid, None)
        elif rid not in gone:
            unclear[rid] = (title or etype.rsplit("/", 1)[-1] or "error")[:60]
    for rid in batch:
        if rid not in on_x and rid not in gone and rid not in unclear:
            unclear[rid] = "not returned"
    return on_x, gone, unclear


class _Sweep:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        cache: Cache,
        log: RunLog | None,
        client: XApiClient | None,
        approve: Callable[[str], bool],
        dry_run: bool,
        run_id: str,
        cap: float,
        limit: int | None,
        config_dir: Path,
        rerender: Callable[[], str | None] | None,
        orphans: Callable[[], list[Path]] | None,
    ):
        self.cfg = cfg
        self.store = store
        self.cache = cache
        self.log = log
        self.client = client
        self.approve = approve
        self.dry_run = dry_run
        self.run_id = run_id
        self.cap = cap
        self.limit = limit
        self.config_dir = config_dir
        self.rerender = rerender
        self.orphans = orphans
        self.billing = Billing.from_config(cfg)
        self.result = SweepResult(run_id=run_id, dry_run=dry_run, cap_usd=cap)
        self.checked: list[str] = []

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, **fields)

    # -- estimate -------------------------------------------------------
    def _estimate(self, selected: list[str]) -> str:
        res = self.result
        b = self.billing
        n = len(selected)
        posts_cost = n * b.post_read
        res.est_cost = posts_cost + b.user_read
        order = "oldest first"
        cursor = self.store.get_meta(META_CURSOR) or ""
        if cursor.isdigit() and self.limit is not None:
            order += ", from after the last post that the previous limited sweep checked"
        lines = [
            f"Sweep (DR-05): check stored post IDs with GET {ENDPOINT}?ids= (up to {IDS_PER_LOOKUP} IDs for each call).",
            f"Post IDs in the store: {res.local_ids} ({res.in_posts} in the posts table, "
            f"{res.local_ids - res.in_posts} only in interactions).",
            f"This sweep checks: {n} ({order}{f', --limit {self.limit}' if self.limit is not None else ''}), "
            f"in {res.calls_planned} calls.",
            f"Before the check: one GET {OWNER_ENDPOINT} call to identify the owner (FR-54), 1 user at ${b.user_read:.4f}.",
            f"Maximum: {n} posts x ${b.post_read:.4f} (post_read) = ${posts_cost:.4f}, plus ${b.user_read:.4f} = "
            f"${res.est_cost:.4f}. The estimate counts each requested ID as one billed post read (worst case); "
            "X can bill only the posts that it returns.",
            f"Cap: ${self.cap:.2f}. Each call reserves its maximum cost before it is sent, and the sweep stops "
            "before a call that could go above the cap.",
        ]
        if res.est_cost > self.cap + EPS:
            room = max(0.0, self.cap - b.user_read)
            can = int((room + EPS) // (IDS_PER_LOOKUP * b.post_read)) * IDS_PER_LOOKUP if b.post_read > 0 else n
            lines.append(
                f"The maximum is above the cap. At this cap the sweep checks about {min(can, n)} posts. "
                "Give a larger --cap or a smaller --limit."
            )
        lines.append(
            f"A post that X reports as \"{NOT_FOUND_TITLE}\" (resource-not-found) is deleted from the store, the raw "
            "cache, and the rendered HTML files (AD-10). Other errors (for example \"Authorization Error\" for a "
            "protected or suspended account) delete nothing; the summary counts them."
        )
        return "\n".join(lines)

    # -- paid calls -----------------------------------------------------
    def _reserve(self, step: int, endpoint: str, cls: str, resources: int, price: float) -> int:
        """Reserve the maximum cost of one call in the ledger. Raise _Stop above the cap."""
        reserve = resources * price
        spent = self.store.ledger_total(self.run_id)
        if spent + reserve > self.cap + EPS:
            raise _Stop(
                f"Spend cap reached before a call to {endpoint}: spent ${spent:.4f}, next call reserves "
                f"${reserve:.4f}, cap ${self.cap:.4f}."
            )
        cur = self.store.conn.execute(
            "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,response_code,at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, step, endpoint, cls, 0, price, reserve, reserve, None, now_iso()),
        )
        return int(cur.lastrowid or 0)

    def _get(self, step: int, path: str, params: dict[str, Any], lid: int, price: float) -> dict[str, Any]:
        assert self.client is not None
        try:
            body, _h = self.client.get(path, params, unit_price=price, step=step)
        except XApiError as e:
            # An HTTP error response is not a delivered resource. A network
            # error is unknown, so the reservation stays in the ledger.
            if e.status is not None:
                self.store.conn.execute("UPDATE cost_ledger SET cost=0, response_code=? WHERE id=?", (e.status, lid))
            raise
        return body

    def _identify_owner(self) -> None:
        """FR-54: the signed-in account must be the owner of config and of this store."""
        price = self.billing.user_read
        lid = self._reserve(OWNER_STEP, OWNER_ENDPOINT, "user_read", 1, price)
        body = self._get(OWNER_STEP, OWNER_ENDPOINT, dict(USER_PARAMS), lid, price)
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        self.store.conn.execute(
            "UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?", (1, price, lid)
        )
        if not data or not data.get("id"):
            raise _Stop(f"GET {OWNER_ENDPOINT} returned no user ID")
        uid = str(data["id"])
        got = str(data.get("username", "")).casefold()
        expected = str(self.cfg.get("owner.handle", "") or "").lstrip("@").casefold()
        if expected and got != expected:
            raise _Stop(f"the signed-in account is not @{self.cfg.get('owner.handle')} from config owner.handle (FR-54)")
        known = self.store.ego_id
        if known is not None and known != uid and not (known.startswith("h:") and known[2:].casefold() == got):
            raise _Stop("the signed-in account is not the owner account of this store (FR-54)")

    def _check(self, batch: list[str]) -> None:
        price = self.billing.post_read
        lid = self._reserve(STEP, ENDPOINT, "post_read", len(batch), price)
        body = self._get(STEP, ENDPOINT, {"ids": ",".join(batch)}, lid, price)
        on_x, gone, unclear = classify(body, batch)
        # Worst case (see the module text): each requested ID is a billed post read.
        self.store.conn.execute(
            "UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?",
            (len(batch), len(batch) * price, lid),
        )
        res = self.result
        res.calls += 1
        res.checked += len(batch)
        res.on_x += len(on_x)
        res.gone.extend(sorted(gone, key=int))
        res.kept.update(unclear.values())
        self.checked.extend(batch)

    # -- main -----------------------------------------------------------
    def run(self) -> SweepResult:
        res = self.result
        ids, res.in_posts = local_post_ids(self.store)
        res.local_ids = len(ids)
        selected = select_ids(ids, self.store.get_meta(META_CURSOR), self.limit)
        res.selected = len(selected)
        res.calls_planned = math.ceil(len(selected) / IDS_PER_LOOKUP)
        res.estimate = self._estimate(selected)
        if self.dry_run:
            return res
        if not selected:
            res.stopped_reason = "the store holds no post IDs to check"
            return res
        assert self.client is not None
        ok = bool(self.approve(res.estimate))
        res.approved = ok
        self._log("sweep_approval", approved=ok, estimate_cost=round(res.est_cost, 6), posts=len(selected))
        if not ok:
            res.stopped_reason = "the owner did not approve the sweep"
            return res
        res.stage_run_id = self.store.begin_stage("sweep", self.run_id, {}, self.cfg.hash())
        self._log("sweep_start", posts=len(selected), cap_usd=self.cap)
        try:
            self._identify_owner()
            for i in range(0, len(selected), IDS_PER_LOOKUP):
                self._check(selected[i : i + IDS_PER_LOOKUP])
        except _Stop as e:
            res.stopped_reason = e.reason
        except (XApiError, AuthError) as e:
            res.stopped_reason = f"{type(e).__name__}: {e}"
        finally:
            res.requests_sent = self.client.requests_sent
        try:
            if res.gone:
                res.delete_report = delete_ids(
                    self.store, self.cache, self.config_dir, list(res.gone), "post", self.rerender,
                    extra_files=self.orphans() if self.orphans is not None else None,
                )
            self._save_cursor(ids, selected)
        finally:
            res.total_cost = self.store.ledger_total(self.run_id)
            self._finish()
        return res

    def _save_cursor(self, ids: list[str], selected: list[str]) -> None:
        if self.limit is None or len(selected) >= len(ids):
            if len(self.checked) == len(selected):
                self.store.set_meta(META_CURSOR, "")
                return
        gone = set(self.result.gone)
        # The last checked post that is still in the store. A gone post is never the cursor,
        # so the meta table holds no deleted ID. If every checked post was gone, the old
        # cursor stays: the deleted posts are no longer candidates, so the next sweep moves on.
        for pid in reversed(self.checked):
            if pid not in gone:
                self.store.set_meta(META_CURSOR, pid)
                return

    def _finish(self) -> None:
        res = self.result
        rep = res.delete_report
        counts = {
            "complete": res.complete,
            "stopped_reason": res.stopped_reason,
            "local_ids": res.local_ids,
            "selected": res.selected,
            "checked": res.checked,
            "on_x": res.on_x,
            "gone_deleted": len(res.gone),
            "not_deleted": dict(sorted(res.kept.items())),
            "calls": res.calls,
            "cost_usd": round(res.total_cost, 6),
            "cap_usd": self.cap,
            "remaining_locations": len(rep.remaining) if rep is not None else 0,
        }
        if res.stage_run_id:
            self.store.finish_stage(res.stage_run_id, counts)
        self._log("sweep_end", complete=res.complete, checked=res.checked, gone=len(res.gone),
                  not_deleted=sum(res.kept.values()), cost=round(res.total_cost, 6))


def run_sweep(
    cfg: Config,
    store: Store,
    cache: Cache,
    log: RunLog | None,
    client: XApiClient | None,
    approve: Callable[[str], bool],
    *,
    dry_run: bool = False,
    run_id: str | None = None,
    cap: float | None = None,
    limit: int | None = None,
    config_dir: Path | None = None,
    rerender: Callable[[], str | None] | None = None,
    orphans: Callable[[], list[Path]] | None = None,
) -> SweepResult:
    """Check the stored post IDs on X and delete the gone posts locally (DR-05).

    ``cap`` defaults to ``collect.cap_usd``. ``limit`` checks at most that many
    posts. ``rerender`` and ``orphans`` are the same callables that the
    ``delete`` command gives to ``delete_ids``. With ``dry_run=True`` the
    function makes no HTTP call; ``client`` can be None.
    """
    if not dry_run and client is None:
        raise ValueError("a live sweep needs an XApiClient")
    if limit is not None and limit < 1:
        raise ValueError("limit must be 1 or more")
    cap_usd = float(cfg.get("collect.cap_usd")) if cap is None else float(cap)
    if cap_usd <= 0:
        raise ValueError("the cap must be more than 0")
    sweep = _Sweep(
        cfg, store, cache, log, None if dry_run else client, approve, dry_run, run_id or new_id("run"),
        cap_usd, limit, Path(config_dir) if config_dir is not None else cfg.root / "config", rerender, orphans,
    )
    return sweep.run()
