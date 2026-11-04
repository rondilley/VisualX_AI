"""The budgeted collection plan (ARCHITECTURE AD-04, FR-12 to FR-16, IR-02, OR-03).

Five steps for the authenticated owner's account only (FR-54), and an
optional sixth step for the owner's top contacts (FR-07):

1. ``GET /2/users/:id/tweets``          owner posts in the window
2. ``GET /2/users/:id/mentions``        mentions (the endpoint gives at most 800)
3. ``GET /2/tweets/:id/quote_tweets``   only for owner posts with quote_count > 0
4. ``GET /2/tweets/:id/retweeted_by``   only for owner posts with retweet_count > 0
6. ``GET /2/users/:id/tweets``          recent posts of the seed accounts (``collect.seeds`` > 0)
5. ``GET /2/users?ids=``                users in interactions with no profile

Step 6 runs before step 5, so that the profile lookup includes the accounts
that the seed posts add. The step numbers in the ledger and the cursor stay
the same as in stores from before step 6: step 6 is a new number, not a new
order of the old numbers. With ``collect.seeds = 0`` (the default) the plan is
the five-step plan.

Before each paid step the plan shows an estimate and asks ``approve``. Before
each call it reserves the maximum cost of the call (page size x unit price)
and stops if the run total plus the reservation would go above the cap
(TSC-04). Each response goes to the raw cache first; the records of the page,
the cursor, and the ledger row then go into the store in one transaction. A
restart continues from the cursor and reads a cached page again for free.

``dry_run=True`` makes the plan from the store and the configuration and
makes no HTTP call (TSC-03).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..auth import AuthError
from ..cache import Cache, request_hash
from ..config import Config
from ..logs import RunLog
from ..store import Store, new_id, now_iso
from .base import Page, no_self_edges
from .seeds import select_seeds
from .xapi import (
    POST_PARAMS,
    USER_PARAMS,
    XApiClient,
    XApiError,
    count_resources,
    endpoint_template,
    map_post,
    map_retweeted_by,
    map_user,
)

STEP_NAMES: dict[int, str] = {
    0: "owner identity",
    1: "owner posts",
    2: "mentions",
    3: "quote posts",
    4: "reposting users",
    5: "profiles",
    6: "seed posts",
}
STEP_ENDPOINTS: dict[int, str] = {
    0: "/2/users/me",
    1: "/2/users/:id/tweets",
    2: "/2/users/:id/mentions",
    3: "/2/tweets/:id/quote_tweets",
    4: "/2/tweets/:id/retweeted_by",
    5: "/2/users",
    6: "/2/users/:id/tweets",
}
STEP_UNITS: dict[int, str] = {0: "user", 1: "posts", 2: "posts", 3: "posts", 4: "users", 5: "users", 6: "posts"}
# Rough count of "includes" objects for each resource, used only when the
# owner says that X bills expansions (OQ-01). It is not a measured value.
INCLUDES_PER_RESOURCE = 2.0
USERS_PER_LOOKUP = 100
META_WINDOW = "collect_window_start"
META_OPENED = "collect_window_opened"
# Incremental collection (FR-08): the window start of the last complete run.
META_COMPLETE_WINDOW = "collect_complete_window_start"
META_SINCE = "collect_since_json"
# Seed collection (FR-07). The plan of step 6 for the open window: the seeds, the since_id or
# start time of each seed, and the pages done. A resumed run uses the same plan.
META_SEED_PLAN = "collect_seed_plan_json"
# The newest post ID that step 6 read from each seed timeline, for all windows (FR-08 for seeds).
META_SEED_NEWEST = "collect_seed_newest_json"
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"
EPS = 1e-9


class CapReached(Exception):
    """The next call could push the run cost above the cap. The run stops before the call."""

    def __init__(self, step: int, reserve: float, spent: float, cap: float, remaining: list[str]):
        self.step = step
        self.step_name = STEP_NAMES[step]
        self.reserve = reserve
        self.spent = spent
        self.cap = cap
        self.remaining = remaining
        super().__init__(
            f"Spend cap reached before step {step} ({self.step_name}): spent ${spent:.4f}, "
            f"next call reserves ${reserve:.4f}, cap ${cap:.4f}."
        )


class _Stop(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class StepReport:
    step: int
    name: str
    endpoint: str
    billing_class: str
    unit_price: float
    est_resources: int | None = None
    est_kind: str = "unknown"
    actual_resources: int = 0
    actual_cost: float = 0.0
    calls: int = 0
    cached_pages: int = 0
    interactions_added: int = 0
    approved: bool | None = None
    status: str = "pending"  # pending | done | skipped | stopped | not_run

    @property
    def est_cost(self) -> float | None:
        return None if self.est_resources is None else self.est_resources * self.unit_price


@dataclass
class CollectResult:
    run_id: str
    dry_run: bool
    cap_usd: float
    window_start: str
    ego_user_id: str | None = None
    ego_handle: str | None = None
    steps: list[StepReport] = field(default_factory=list)
    estimates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    cap_reached: CapReached | None = None
    remaining_plan: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    requests_sent: int = 0
    stage_run_id: str | None = None
    incremental: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.dry_run and self.stopped_reason is None

    def step(self, n: int) -> StepReport:
        for s in self.steps:
            if s.step == n:
                return s
        raise KeyError(n)

    def summary_lines(self) -> list[str]:
        lines = [f"Run {self.run_id}{' (dry run, no API calls)' if self.dry_run else ''}"
                 f"{' - incremental: steps 1 and 2 ask only for posts newer than the stored ones (FR-08)' if self.incremental else ''}"]
        lines.append(f"Owner: {self.ego_user_id or 'unknown'}   Window start: {self.window_start}   Cap: ${self.cap_usd:.2f}")
        lines.append("Step  Name             Estimate                 Actual")
        for s in self.steps:
            if s.est_resources is None:
                est = "unknown"
            else:
                est = f"{s.est_resources} ({s.est_kind}) ${s.est_cost:.4f}"
            act = f"{s.actual_resources} ${s.actual_cost:.4f}" if not self.dry_run else "-"
            lines.append(f"{s.step:<5} {s.name:<16} {est:<24} {act}  [{s.status}]")
        if not self.dry_run:
            lines.append(f"Logged cost: ${self.total_cost:.4f} of cap ${self.cap_usd:.2f}")
        if self.dry_run and self.estimates:
            lines.append("Estimates (a live run shows each one and asks for approval before the step):")
            for text in self.estimates:
                lines.extend(f"  {x}" for x in text.splitlines())
        for note in self.notes:
            lines.append(f"Note: {note}")
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        if self.stopped_reason:
            lines.append(f"Stopped: {self.stopped_reason}")
            for r in self.remaining_plan:
                lines.append(f"  not done: {r}")
        return lines


@dataclass
class Billing:
    post_read: float
    user_read: float
    owned_read: float
    owned_with_user_token: bool
    expansions_billed: bool

    @classmethod
    def from_config(cls, cfg: Config) -> Billing:
        return cls(
            post_read=float(cfg.get("prices.post_read")),
            user_read=float(cfg.get("prices.user_read")),
            owned_read=float(cfg.get("prices.owned_read")),
            owned_with_user_token=bool(cfg.get("billing_assumptions.owned_reads_with_user_token", True)),
            expansions_billed=bool(cfg.get("billing_assumptions.expansions_billed", False)),
        )

    def for_step(self, step: int) -> tuple[str, float]:
        if step in (1, 2):
            return ("owned_read", self.owned_read) if self.owned_with_user_token else ("post_read", self.post_read)
        if step in (3, 6):
            # Step 6 reads the posts of other accounts: not an owned read.
            return "post_read", self.post_read
        return "user_read", self.user_read

    @staticmethod
    def has_includes(step: int) -> bool:
        return step in (1, 2, 3, 6)

    def factor(self, step: int) -> float:
        return 1.0 + INCLUDES_PER_RESOURCE if (self.expansions_billed and self.has_includes(step)) else 1.0


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class _Plan:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        cache: Cache | None,
        log: RunLog | None,
        client: XApiClient | None,
        approve: Callable[[str], bool],
        dry_run: bool,
        run_id: str,
        now: datetime,
        full: bool = False,
    ):
        self.cfg = cfg
        self.full = full
        self.store = store
        self.cache = cache
        self.log = log
        self.client = client
        self.approve = approve
        self.dry_run = dry_run
        self.run_id = run_id
        self.now = now
        self.billing = Billing.from_config(cfg)
        self.cap = float(cfg.get("collect.cap_usd"))
        self.page_size = int(cfg.get("collect.page_size", 100))
        self.window_days = int(cfg.get("collect.window_days", 90))
        self.mention_limit = int(cfg.get("collect.mention_limit", 800))
        self.owner_post_limit = int(cfg.get("collect.owner_post_limit", 3200))
        # FR-07 seed accounts. 0 = off: the plan is the five-step plan.
        self.seeds_n = max(0, int(cfg.get("collect.seeds", 0) or 0))
        self.seed_window_days = max(1, int(cfg.get("collect.seed_window_days", 30)))
        self.seed_pages = max(1, int(cfg.get("collect.seed_pages", 1)))
        self.ego: str | None = store.ego_id
        self.window_start = (now - timedelta(days=self.window_days)).strftime(TIME_FMT)
        self.result = CollectResult(run_id=run_id, dry_run=dry_run, cap_usd=self.cap, window_start=self.window_start)
        self.since: dict[int, str] = {}  # step -> since_id, set by _plan_incremental
        # Execution order. Step 6 (seed posts) runs before step 5 (profiles).
        order = [1, 2, 3, 4] + ([6] if self.seeds_n > 0 else []) + [5]
        for n in order:
            cls, price = self.billing.for_step(n)
            self.result.steps.append(StepReport(n, STEP_NAMES[n], STEP_ENDPOINTS[n], cls, price))

    # -- logging --------------------------------------------------------
    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, **fields)

    def _warn(self, msg: str) -> None:
        self.result.warnings.append(msg)
        self._log("collect_warning", warning=msg)

    # -- store queries --------------------------------------------------
    def _owner_posts(self) -> list[dict[str, Any]]:
        if not self.ego:
            return []
        rows = self.store.q(
            "SELECT post_id, created_at, public_metrics_json, referenced_json, observed_at FROM posts "
            "WHERE author_id=? AND source_endpoint=? AND (created_at IS NULL OR created_at>=?) ORDER BY post_id",
            (self.ego, STEP_ENDPOINTS[1], self.window_start),
        )
        out = []
        for r in rows:
            refs = json.loads(r["referenced_json"]) if r["referenced_json"] else []
            if any(isinstance(x, dict) and x.get("type") == "retweeted" for x in refs):
                continue  # the owner's own repost: its counts belong to the reposted post
            pm = json.loads(r["public_metrics_json"]) if r["public_metrics_json"] else {}
            out.append({"id": r["post_id"], "public_metrics": pm, "observed_at": r["observed_at"]})
        return out

    def _count_posts(self, step: int) -> tuple[int, str | None]:
        """Posts of a timeline step that this collection window fetched (count, oldest created_at)."""
        opened = self.store.get_meta(META_OPENED) or ""
        row = self.store.q1(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM posts WHERE source_endpoint=? AND created_at>=? "
            "AND observed_at>=?" + (" AND author_id=?" if step == 1 else ""),
            (STEP_ENDPOINTS[step], self.window_start, opened) + ((self.ego,) if step == 1 else ()),
        )
        return (int(row["n"]), row["oldest"]) if row else (0, None)

    def _missing_profiles(self) -> list[str]:
        rows = self.store.q(
            "SELECT uid FROM (SELECT source_user_id AS uid FROM interactions UNION SELECT target_user_id FROM interactions) "
            "WHERE uid NOT IN (SELECT user_id FROM profiles) AND uid NOT IN (SELECT user_id FROM profile_errors) ORDER BY uid"
        )
        return [r["uid"] for r in rows]

    def _newest_post_id(self, step: int) -> str | None:
        args: tuple[Any, ...] = (STEP_ENDPOINTS[step], self.window_start)
        sql = "SELECT post_id FROM posts WHERE source_endpoint=? AND created_at>=?"
        if step == 1:
            sql += " AND author_id=?"
            args += (self.ego,)
        ids = [r["post_id"] for r in self.store.q(sql, args) if str(r["post_id"]).isdigit()]
        return max(ids, key=int) if ids else None

    def _plan_incremental(self) -> None:
        """FR-08: after a complete run, steps 1 and 2 ask only for posts newer than the stored ones.

        A run is incremental only when the last complete run covered this window start
        or an earlier one, so that no older part of the window is left out.
        """
        self.since = {}
        if self.store.get_meta(META_WINDOW):
            # A stopped run resumes with the since IDs it opened with (the cursor tokens belong to them).
            saved = self.store.get_meta(META_SINCE) or ""
            self.since = {int(k): v for k, v in json.loads(saved).items()} if saved else {}
            return
        if self.full or not self.ego:
            return
        done_start = self.store.get_meta(META_COMPLETE_WINDOW) or ""
        if not done_start:
            # Stores from before FR-08: use the newest complete X API collect record.
            for r in self.store.q(
                "SELECT counts_json FROM stage_runs WHERE stage='collect' AND finished_at IS NOT NULL ORDER BY finished_at DESC"
            ):
                c = json.loads(r["counts_json"] or "{}")
                if c.get("complete") and not c.get("source") and c.get("window_start"):
                    done_start = str(c["window_start"])
                    break
        if not done_start or done_start > self.window_start:
            return
        for step in (1, 2):
            newest = self._newest_post_id(step)
            if newest:
                self.since[step] = newest

    def _metric_items(self, metric: str, step: int) -> list[tuple[str, int]]:
        items = []
        opened = self.store.get_meta(META_OPENED) or ""
        new_only = 1 in self.since and not self.dry_run
        for p in self._owner_posts():
            if new_only and p.get("observed_at", "") < opened:
                # Incremental: a stored post was not fetched again, so its counts are the
                # old counts and its quotes and reposts are in the store from the earlier run.
                continue
            v = p["public_metrics"].get(metric)
            if isinstance(v, int) and v > 0:
                if self.dry_run:
                    items.append((p["id"], v))
                    continue
                _tok, done = self.store.get_cursor(step, p["id"])
                if not done:
                    items.append((p["id"], v))
        return items

    # -- estimate text --------------------------------------------------
    def _pos(self, step: int) -> int:
        """Position of a step in the execution order (step 0, the owner identity, is first)."""
        for i, s in enumerate(self.result.steps):
            if s.step == step:
                return i
        return -1

    def _remaining_from(self, step: int) -> list[str]:
        """The plan lines for ``step`` and each later step in the execution order."""
        return self._remaining_lines(self._pos(step) - 1)

    def _remaining_lines(self, after_pos: int) -> list[str]:
        out = []
        for i, s in enumerate(self.result.steps):
            if i <= after_pos or s.status in ("done", "skipped"):
                continue
            if s.est_resources is None:
                out.append(f"Step {s.step} ({s.name}, {s.endpoint}): count known after step {1 if s.step in (3, 4) else 4}")
            else:
                out.append(
                    f"Step {s.step} ({s.name}, {s.endpoint}): {s.est_resources} {STEP_UNITS[s.step]} "
                    f"({s.est_kind}), ${s.est_cost:.4f}"
                )
        return out

    def _estimate_text(self, step: int, extra: list[str] | None = None) -> str:
        s = self.result.step(step)
        b = self.billing
        lines = [f"Step {step} of {len(self.result.steps)}: {s.name} ({s.endpoint})"]
        if step == 6:
            lines.append("Step 6 runs before step 5, so that the profile lookup in step 5 includes the accounts "
                         "that the seed posts add.")
        elif step == 5 and self.seeds_n > 0:
            lines.append("Step 5 runs after step 6 (seed posts).")
        if s.est_resources is None:
            lines.append("Estimate: not known yet.")
        else:
            lines.append(
                f"Estimate: {s.est_resources} {STEP_UNITS[step]} ({s.est_kind}) x ${s.unit_price:.4f} "
                f"({s.billing_class}) = ${s.est_cost:.4f}"
            )
        lines.extend(extra or [])
        lines.append(
            f"Unit prices (config [prices]): post_read ${b.post_read:.4f}, user_read ${b.user_read:.4f}, "
            f"owned_read ${b.owned_read:.4f} per resource."
        )
        n = s.est_resources or 0
        unverified = False
        if step in (1, 2):
            unverified = True
            if b.owned_with_user_token:
                lines.append(
                    "Billing assumption not verified (OQ-02): owner posts and mentions bill at the owned-read price "
                    f"with the user token. If this is wrong: ${n * b.post_read:.4f} at post_read ${b.post_read:.4f}."
                )
            else:
                lines.append(
                    "Billing assumption not verified (OQ-02): configured to bill at post_read. If owned reads apply: "
                    f"${n * b.owned_read:.4f} at owned_read ${b.owned_read:.4f}."
                )
        if b.has_includes(step):
            unverified = True
            if b.expansions_billed:
                lines.append(
                    "Billing assumption not verified (OQ-01): configured that X bills the objects in \"includes\" "
                    f"(rough: {INCLUDES_PER_RESOURCE:g} for each resource). If X does not bill them: ${n * s.unit_price:.4f}."
                )
            else:
                lines.append(
                    "Billing assumption not verified (OQ-01): X does not bill the objects in \"includes\". If this is wrong: "
                    f"up to about ${n * s.unit_price * (1 + INCLUDES_PER_RESOURCE):.4f} "
                    f"(rough: {INCLUDES_PER_RESOURCE:g} included objects for each resource)."
                )
        if not unverified:
            lines.append("No unverified billing assumption applies to this step.")
        spent = 0.0 if self.dry_run else self.store.ledger_total(self.run_id)
        lines.append(
            f"Spent in this run: ${spent:.4f}. Cap: ${self.cap:.2f}. Each call reserves page size x unit price "
            "before it is sent, and the run stops before a call that could go above the cap."
        )
        rem = self._remaining_lines(self._pos(step))
        if rem:
            lines.append("Remaining plan after this step:")
            lines.extend(f"  {r}" for r in rem)
        text = "\n".join(lines)
        self.result.estimates.append(text)
        return text

    def _ask(self, step: int, extra: list[str] | None = None) -> None:
        text = self._estimate_text(step, extra)
        if self.dry_run:
            return
        ok = bool(self.approve(text))
        s = self.result.step(step)
        s.approved = ok
        self._log("collect_approval", step=step, approved=ok, estimate_cost=s.est_cost)
        if not ok:
            s.status = "not_run"
            raise _Stop(f"the owner did not approve step {step} ({s.name})")

    # -- calls ----------------------------------------------------------
    def _reserve(self, step: int, max_resources: int) -> tuple[float, float, str]:
        cls, price = self.billing.for_step(step)
        reserve = max_resources * price * self.billing.factor(step)
        spent = self.store.ledger_total(self.run_id)
        if spent + reserve > self.cap + EPS:
            raise CapReached(step, reserve, spent, self.cap, self._remaining_from(step))
        return reserve, price, cls

    def _call(
        self, step: int, path: str, params: dict[str, Any], max_resources: int, cacheable: bool = True
    ) -> tuple[dict[str, Any], str, int | None, float]:
        """Return (body, request hash, ledger row ID or None for a cached page, unit price)."""
        assert self.client is not None
        template, _ids = endpoint_template(path)
        rhash = request_hash(path, {**params, "_window": self.window_start})
        if cacheable and self.cache is not None:
            cached = self.cache.get(rhash)
            if cached is not None:
                self._log("cache_hit", step=step, endpoint=template)
                if step:
                    self.result.step(step).cached_pages += 1
                return cached, rhash, None, 0.0
        reserve, price, cls = self._reserve(step, max_resources)
        cur = self.store.conn.execute(
            "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,response_code,at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, step, template, cls, 0, price, reserve, reserve, None, now_iso()),
        )
        lid = int(cur.lastrowid or 0)
        try:
            body, _headers = self.client.get(
                path, params, unit_price=price, expansions_billed=self.billing.expansions_billed, step=step
            )
        except XApiError as e:
            # An HTTP error response is not a delivered resource. A network
            # error is unknown, so the reservation stays in the ledger.
            if e.status is not None:
                self.store.conn.execute("UPDATE cost_ledger SET cost=0, response_code=? WHERE id=?", (e.status, lid))
            raise
        if step:
            self.result.step(step).calls += 1
        if cacheable and self.cache is not None:
            self.cache.put(rhash, template, body)
        return body, rhash, lid, price

    def _commit(
        self, step: int, item: str, page: Page, endpoint: str, lid: int | None, price: float, done: bool,
        *, keep_existing_posts: bool = False, extra: Callable[[Any], None] | None = None,
    ) -> None:
        """Write the page records, the cursor, the ledger row, and ``extra`` in one transaction.

        ``keep_existing_posts``: a stored post keeps its row (step 6: a seed post that is
        also a mention stays a mention, so the step 2 since_id does not change).
        """
        cost = page.resources * price if lid is not None else 0.0
        with self.store.tx() as c:
            added = self.store.put_interactions(no_self_edges(page.interactions), conn=c)
            self.store.put_profiles(page.profiles, conn=c)
            for p in page.posts:
                if keep_existing_posts and c.execute("SELECT 1 FROM posts WHERE post_id=?", (str(p["id"]),)).fetchone():
                    continue
                self.store.put_post(p, endpoint, self.run_id, conn=c)
            for uid, err in page.profile_errors.items():
                self.store.put_profile_error(uid, err, conn=c)
            self.store.set_cursor(step, item, page.next_token, done, self.run_id, conn=c)
            if extra is not None:
                extra(c)
            if lid is not None:
                c.execute(
                    "UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?",
                    (page.resources, cost, lid),
                )
        s = self.result.step(step)
        s.interactions_added += added
        if lid is not None:
            s.actual_resources += page.resources
            s.actual_cost += cost
            row = self.store.q1("SELECT reserved FROM cost_ledger WHERE id=?", (lid,))
            if row is not None and cost > float(row["reserved"]) + EPS:
                self._warn(f"step {step}: a call cost ${cost:.4f}, more than its reservation ${row['reserved']:.4f}")
        if self.store.ledger_total(self.run_id) > self.cap + EPS:
            raise _Stop("the logged cost is above the cap after a call that cost more than its reservation")

    # -- page handlers --------------------------------------------------
    def _posts_page(
        self, body: dict[str, Any], endpoint: str, rhash: str, keep_posts: bool, owner_ids: tuple[str, ...] = ()
    ) -> Page:
        assert self.ego is not None
        observed = now_iso()
        page = Page(resources=count_resources(body, self.billing.expansions_billed))
        includes = body.get("includes") if isinstance(body.get("includes"), dict) else {}
        for p in body.get("data") or []:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            if keep_posts:
                page.posts.append(p)
            page.interactions.extend(
                map_post(p, self.ego, endpoint, self.run_id, includes=includes, owner_post_ids=owner_ids,
                         observed_at=observed, raw_request_hash=rhash)
            )
        for u in includes.get("users") or []:
            if isinstance(u, dict) and u.get("id"):
                page.profiles.append(map_user(u, f"{endpoint}#includes", observed))
        return page

    def _paged(
        self,
        step: int,
        item: str,
        path: str,
        base_params: dict[str, Any],
        lo: int,
        handler: Callable[[dict[str, Any], str], Page],
        limit: Callable[[], int | None] | None = None,
    ) -> None:
        token, done = self.store.get_cursor(step, item)
        if done:
            return
        endpoint = STEP_ENDPOINTS[step]
        while True:
            size = self.page_size
            if limit is not None:
                left = limit()
                if left is not None and left <= 0:
                    self.store.set_cursor(step, item, token, True, self.run_id)
                    return
                if left is not None:
                    size = min(size, left)
            size = _clamp(size, lo, 100)
            params = {**base_params, "max_results": size}
            if token:
                params["pagination_token"] = token
            body, rhash, lid, price = self._call(step, path, params, size)
            page = handler(body, rhash)
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            nxt = meta.get("next_token")
            page.next_token = nxt if isinstance(nxt, str) and nxt else None
            is_done = page.next_token is None
            self._commit(step, item, page, endpoint, lid, price, is_done)
            if is_done:
                return
            token = page.next_token

    # -- steps ----------------------------------------------------------
    def _identify_owner(self) -> None:
        assert self.client is not None
        reserve, price, cls = self._reserve(0, 1)
        cur = self.store.conn.execute(
            "INSERT INTO cost_ledger(run_id,step,endpoint,billing_class,resources,unit_price,cost,reserved,response_code,at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, 0, STEP_ENDPOINTS[0], cls, 0, price, reserve, reserve, None, now_iso()),
        )
        lid = int(cur.lastrowid or 0)
        try:
            body, _h = self.client.get("/2/users/me", dict(USER_PARAMS), unit_price=price, step=0)
        except XApiError as e:
            if e.status is not None:
                self.store.conn.execute("UPDATE cost_ledger SET cost=0, response_code=? WHERE id=?", (e.status, lid))
            raise
        data = body.get("data") if isinstance(body.get("data"), dict) else None
        if not data or not data.get("id"):
            raise _Stop("GET /2/users/me returned no user ID")
        n = count_resources(body)
        uid = str(data["id"])
        expected = str(self.cfg.get("owner.handle", "") or "").lstrip("@").casefold()
        got = str(data.get("username", "")).casefold()
        if expected and got != expected:
            self.store.conn.execute("UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?", (n, n * price, lid))
            raise _Stop(f"the signed-in account is not @{self.cfg.get('owner.handle')} from config owner.handle (FR-54)")
        known = self.store.ego_id
        if known is not None and known.startswith("h:") and known[2:] == got:
            # The Grok source stored the owner under the provisional ID h:<handle>.
            self.store.rekey_user(known, uid)
            known = uid
        with self.store.tx() as c:
            c.execute("UPDATE cost_ledger SET resources=?, cost=?, response_code=200 WHERE id=?", (n, n * price, lid))
            if known is None or known == uid:
                self.store.put_profiles([map_user(data, STEP_ENDPOINTS[0])], conn=c)
                c.execute("INSERT INTO meta(key,value) VALUES('ego_user_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (uid,))
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('ego_handle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(data.get("username", "")),),
                )
        if known is not None and known != uid:
            raise _Stop("the signed-in account is not the owner account of this store (FR-54); use a new data folder")
        self.ego = uid
        self.result.ego_user_id = uid
        self.result.ego_handle = str(data.get("username", "")) or None

    def _step_timeline(self, step: int) -> None:
        assert self.ego is not None
        kind = "tweets" if step == 1 else "mentions"
        cap_n = self.owner_post_limit if step == 1 else self.mention_limit
        path = f"/2/users/{self.ego}/{kind}"
        if step in self.since:
            params = {**POST_PARAMS, "since_id": self.since[step]}
        else:
            params = {**POST_PARAMS, "start_time": self.window_start}
        endpoint = STEP_ENDPOINTS[step]

        def limit() -> int:
            return cap_n - self._count_posts(step)[0]

        self._paged(
            step, "owner", path, params, 5,
            lambda body, rhash: self._posts_page(body, endpoint, rhash, keep_posts=True),
            limit,
        )
        n, oldest = self._count_posts(step)
        if n >= cap_n:
            if step == 2:
                self._warn(
                    f"the {cap_n}-mention limit cut the window: the oldest mention collected is from {oldest}, "
                    f"the window starts {self.window_start} (IR-02). Use a shorter window or run more often."
                )
            else:
                self._warn(f"the owner post limit ({cap_n}) cut the window: the oldest owner post collected is from {oldest}.")
        if n == 0:
            if step in self.since:
                self._log("collect_no_new_posts", step=step)
                self.result.notes.append(f"step {step} ({STEP_NAMES[step]}): no new posts since the last run.")
            else:
                self._warn(f"step {step} ({STEP_NAMES[step]}) returned no data for the window.")

    def _step_quotes(self, items: list[tuple[str, int]]) -> None:
        endpoint = STEP_ENDPOINTS[3]
        for pid, _n in items:
            self._paged(
                3, pid, f"/2/tweets/{pid}/quote_tweets", dict(POST_PARAMS), 10,
                lambda body, rhash, pid=pid: self._posts_page(body, endpoint, rhash, keep_posts=False, owner_ids=(pid,)),
            )

    def _step_reposts(self, items: list[tuple[str, int]]) -> None:
        assert self.ego is not None
        endpoint = STEP_ENDPOINTS[4]

        def handler(body: dict[str, Any], rhash: str, pid: str) -> Page:
            assert self.ego is not None
            observed = now_iso()
            users = [u for u in body.get("data") or [] if isinstance(u, dict) and u.get("id")]
            page = Page(resources=count_resources(body, self.billing.expansions_billed))
            page.interactions = map_retweeted_by(users, pid, self.ego, endpoint, self.run_id, observed_at=observed, raw_request_hash=rhash)
            page.profiles = [map_user(u, endpoint, observed) for u in users]
            return page

        for pid, _n in items:
            self._paged(4, pid, f"/2/tweets/{pid}/retweeted_by", dict(USER_PARAMS), 1,
                        lambda body, rhash, pid=pid: handler(body, rhash, pid))

    def _step_profiles(self, missing: list[str]) -> None:
        endpoint = STEP_ENDPOINTS[5]
        for i in range(0, len(missing), USERS_PER_LOOKUP):
            batch = missing[i : i + USERS_PER_LOOKUP]
            params = {"ids": ",".join(batch), **USER_PARAMS}
            body, rhash, lid, price = self._call(5, "/2/users", params, len(batch))
            observed = now_iso()
            page = Page(resources=count_resources(body, self.billing.expansions_billed))
            got: set[str] = set()
            for u in body.get("data") or []:
                if isinstance(u, dict) and u.get("id"):
                    page.profiles.append(map_user(u, endpoint, observed))
                    got.add(str(u["id"]))
            for e in body.get("errors") or []:
                if not isinstance(e, dict):
                    continue
                rid = e.get("resource_id") or e.get("value")
                if rid and str(rid) in batch and str(rid) not in got:
                    page.profile_errors[str(rid)] = str(e.get("title") or e.get("type") or "error")[:100]
            for uid in batch:
                if uid not in got and uid not in page.profile_errors:
                    page.profile_errors[uid] = "not returned"
            last = i + USERS_PER_LOOKUP >= len(missing)
            self._commit(5, "profiles", page, endpoint, lid, price, last)
        failed = len(self.store.profile_errors())
        if failed:
            self._warn(f"{failed} profile lookups did not give a profile; those nodes show as missing profiles (FR-22).")

    # -- step 6: seed posts (FR-07) ------------------------------------
    def _seed_size(self) -> int:
        return _clamp(self.page_size, 5, 100)

    def _seed_start(self) -> str:
        """The later of the window start and ``collect.seed_window_days`` before now."""
        return max(self.window_start, (self.now - timedelta(days=self.seed_window_days)).strftime(TIME_FMT))

    def _seed_newest(self) -> dict[str, str]:
        raw = self.store.get_meta(META_SEED_NEWEST) or ""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        # A deletion (AD-10) replaces a deleted ID with "[deleted]"; such an entry is not used.
        return {str(k): str(v) for k, v in data.items() if str(k).isdigit() and str(v).isdigit()}

    def _seed_since(self, uid: str, start: str) -> str | None:
        """FR-08 for seeds: on an incremental run, the newest post that step 6 read from this seed.

        None (read the full seed window) when the run is not incremental, when the seed is new,
        or when that post is not in the store or is older than the seed window start.
        """
        if not self.since:
            return None
        newest = self._seed_newest().get(uid)
        if not newest:
            return None
        row = self.store.q1("SELECT created_at FROM posts WHERE post_id=?", (newest,))
        if row is None or not row["created_at"] or str(row["created_at"]) < start:
            return None
        return newest

    def _saved_seed_plan(self) -> dict[str, Any] | None:
        raw = self.store.get_meta(META_SEED_PLAN) or ""
        if not raw:
            return None
        try:
            plan = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(plan, dict) or not isinstance(plan.get("seeds"), list):
            return None
        return plan

    def _new_seed_plan(self) -> dict[str, Any]:
        ranked = select_seeds(self.cfg, self.store, self.ego, 10**9)
        chosen = ranked[: self.seeds_n]
        start = self._seed_start()
        return {
            "asked": self.seeds_n,
            "eligible": len(ranked),
            "start": start,
            "pages": self.seed_pages,
            "seeds": [
                {"id": s.user_id, "weight": round(s.weight, 4), "since": self._seed_since(s.user_id, start), "pages_done": 0}
                for s in chosen
            ],
        }

    def _seed_left(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """The seeds of the plan that have pages left."""
        out = []
        for e in plan["seeds"]:
            if not isinstance(e, dict) or not str(e.get("id", "")).isdigit():
                continue
            if not self.dry_run and self.store.get_cursor(6, str(e["id"]))[1]:
                continue
            if int(e.get("pages_done", 0)) >= int(plan.get("pages", self.seed_pages)):
                continue
            out.append(e)
        return out

    def _seed_estimate(self, plan: dict[str, Any]) -> list[str]:
        """Set the step 6 estimate from the plan. Return the extra lines for the approval text."""
        s6 = self.result.step(6)
        pages = int(plan.get("pages", self.seed_pages))
        size = self._seed_size()
        left = self._seed_left(plan)
        s6.est_resources = sum(pages - int(e.get("pages_done", 0)) for e in left) * size
        s6.est_kind = "maximum"
        n_since = sum(1 for e in left if e.get("since"))
        handles = {r["user_id"]: r["handle"] for r in self.store.q("SELECT user_id, handle FROM profiles")}
        n_plan = len(plan["seeds"])
        lines = [
            f"Seeds: {n_plan} accounts, the top {n_plan} of {plan.get('eligible', '?')} contacts by tie weight "
            f"to the owner (collect.seeds = {plan.get('asked', self.seeds_n)}). Not seeds: the owner, IDs that are "
            "not X user IDs, protected accounts, and accounts with a failed profile lookup. "
            "Allowlisted contacts can be seeds.",
            f"For each seed: GET /2/users/:id/tweets, up to {pages} page(s) of {size} posts, posts from "
            f"{plan['start']} (the later of the window start and {self.seed_window_days} days before now).",
            f"Maximum: {len(left)} seeds x {pages} page(s) x {size} posts x ${self.billing.post_read:.4f} "
            f"(post_read) = ${s6.est_cost or 0.0:.4f}. A seed with fewer posts in the seed window costs less.",
        ]
        if (s6.est_cost or 0.0) > self.cap + EPS:
            lines.append(
                f"The maximum is above the cap (${self.cap:.2f}). The run stops before the call that could go above "
                "the cap. To read all seeds, give a larger --cap or a smaller --seeds."
            )
        if self.since:
            lines.append(
                f"Incremental: {n_since} seeds ask only for posts newer than the newest post read from them "
                f"before (since_id); {len(left) - n_since} new seeds read the full seed window."
            )
        if len(left) < n_plan:
            lines.append(f"{n_plan - len(left)} seeds are complete from an earlier run of this window.")
        if left:
            shown = ", ".join(
                f"@{handles.get(str(e['id'])) or str(e['id'])} {float(e.get('weight', 0)):.2f}" for e in left
            )
            lines.append(f"Seeds (tie weight): {shown}")
        return lines

    def _step_seeds(self, plan: dict[str, Any]) -> None:
        assert self.ego is not None
        endpoint = STEP_ENDPOINTS[6]
        size = self._seed_size()
        pages = int(plan.get("pages", self.seed_pages))
        for entry in self._seed_left(plan):
            uid = str(entry["id"])
            token, _done = self.store.get_cursor(6, uid)
            params: dict[str, Any] = {**POST_PARAMS}
            since = entry.get("since")
            if since and str(since).isdigit():
                params["since_id"] = str(since)
            else:
                params["start_time"] = plan["start"]
            while True:
                q = {**params, "max_results": size}
                if token:
                    q["pagination_token"] = token
                try:
                    body, rhash, lid, price = self._call(6, f"/2/users/{uid}/tweets", q, size)
                except XApiError as e:
                    if e.status in (400, 403, 404):
                        # One seed that X does not serve (for example a suspended account) does
                        # not stop the run. The ledger row of the call has cost 0.
                        self._warn(f"step 6: a seed timeline returned HTTP {e.status}; that seed is skipped.")
                        self.store.set_cursor(6, uid, None, True, self.run_id)
                        break
                    raise
                page = self._posts_page(body, endpoint, rhash, keep_posts=True)
                meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
                nxt = meta.get("next_token")
                page.next_token = nxt if isinstance(nxt, str) and nxt else None
                entry["pages_done"] = int(entry.get("pages_done", 0)) + 1
                is_done = page.next_token is None or entry["pages_done"] >= pages
                ids = [str(p["id"]) for p in page.posts if str(p.get("id", "")).isdigit() and str(p.get("author_id")) == uid]
                newest_here = max(ids, key=int) if ids else None

                def extra(c: Any, newest_here: str | None = newest_here, uid: str = uid) -> None:
                    c.execute(
                        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (META_SEED_PLAN, json.dumps(plan)),
                    )
                    if newest_here:
                        cur = self._seed_newest()
                        if uid not in cur or int(newest_here) > int(cur[uid]):
                            cur[uid] = newest_here
                            c.execute(
                                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (META_SEED_NEWEST, json.dumps(cur, sort_keys=True)),
                            )

                self._commit(6, uid, page, endpoint, lid, price, is_done, keep_existing_posts=True, extra=extra)
                if is_done:
                    break
                token = page.next_token

    # -- main -----------------------------------------------------------
    def _estimates_3_to_5(self) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        q_items = self._metric_items("quote_count", 3)
        r_items = self._metric_items("retweet_count", 4)
        have_posts = bool(self._owner_posts())
        s3, s4, s5 = self.result.step(3), self.result.step(4), self.result.step(5)
        if self.dry_run and 1 in self.since:
            # Incremental: steps 3 and 4 cover only the new posts from step 1, which a dry run does not fetch.
            s5.est_resources, s5.est_kind = len(self._missing_profiles()), "now, more after steps 1 to 4"
            if self.seeds_n > 0:
                s5.est_kind = "now, more after steps 1 to 4 and 6"
            return [], []
        if have_posts or not self.dry_run:
            s3.est_resources, s3.est_kind = sum(n for _p, n in q_items), "exact from quote_count"
            s4.est_resources, s4.est_kind = sum(n for _p, n in r_items), "exact from retweet_count"
        missing = len(self._missing_profiles())
        if s3.est_resources is not None:
            s5.est_resources, s5.est_kind = missing + s3.est_resources, "maximum"
        else:
            s5.est_resources, s5.est_kind = missing, "now, more after steps 1 to 3"
        if self.seeds_n > 0:
            s5.est_kind += "; step 6 can add more"
        return q_items, r_items

    def run(self) -> CollectResult:
        res = self.result
        res.ego_user_id = self.ego
        res.ego_handle = self.store.get_meta("ego_handle")
        s1, s2 = res.step(1), res.step(2)
        s1.est_resources, s1.est_kind = self.owner_post_limit, "maximum"
        s2.est_resources, s2.est_kind = self.mention_limit, "maximum"
        self._plan_incremental()
        for step in self.since:
            res.step(step).est_kind = f"maximum, only posts newer than {self.since[step]}"
        if self.since:
            self._log("collect_incremental", steps=sorted(self.since))
        elif self.ego and not self.full and (self.store.get_meta(META_COMPLETE_WINDOW) or "") > self.window_start:
            res.warnings.append("The window starts before the last complete run's window, so this run collects the full window.")
        res.incremental = bool(self.since)

        if self.dry_run:
            if not self.ego:
                res.warnings.append(
                    "The owner ID is not known (no ego_user_id in the store). Run 'visualx auth status' "
                    "or a live collect first. Steps 1 and 2 show the configuration maximums."
                )
            self._estimates_3_to_5()
            seed_lines: list[str] = []
            if self.seeds_n > 0:
                seed_lines = self._seed_estimate(self._new_seed_plan())
                if not self.ego:
                    seed_lines.append("The owner ID is not known, so the store gives no seeds now.")
            for s in res.steps:
                self._estimate_text(s.step, seed_lines if s.step == 6 else None)
            return res

        assert self.client is not None
        open_window = self.store.get_meta(META_WINDOW)
        if open_window:
            self.window_start = res.window_start = open_window
            self._log("collect_resume", window_start=open_window)
        else:
            self.store.reset_cursor()
            self.store.set_meta(META_WINDOW, self.window_start)
            self.store.set_meta(META_OPENED, now_iso())
            self.store.set_meta(META_SINCE, json.dumps({str(k): v for k, v in self.since.items()}))
            self.store.set_meta(META_SEED_PLAN, "")
        if self.cache is not None:
            self.cache.purge_older_than(int(self.cfg.get("data.cache_retention_days", 30)))
        res.stage_run_id = self.store.begin_stage("collect", self.run_id, {}, self.cfg.hash())
        self._log("collect_start", window_start=self.window_start, cap_usd=self.cap, page_size=self.page_size)
        current = 1
        try:
            me_line = f"Before step 1: one GET /2/users/me call to identify the owner, 1 user at ${self.billing.user_read:.4f}."
            if self.store.get_cursor(1, "owner")[1]:
                s1.status = "done"
                s1.est_resources, s1.est_kind = 0, "already collected (cursor)"
                self._ask(1, [me_line, "Step 1 is complete from an earlier run of this window; only GET /2/users/me runs."])
            else:
                self._ask(1, [me_line])
            self._identify_owner()
            if s1.status != "done":
                self._step_timeline(1)
                s1.status = "done"

            current = 2
            if self.store.get_cursor(2, "owner")[1]:
                s2.status = "done"
            else:
                self._ask(2)
                self._step_timeline(2)
                s2.status = "done"

            q_items, r_items = self._estimates_3_to_5()
            for step, items, fn, metric in ((3, q_items, self._step_quotes, "quote_count"), (4, r_items, self._step_reposts, "retweet_count")):
                current = step
                s = res.step(step)
                if not items:
                    s.status = "skipped"
                    self._log("collect_step_skipped", step=step, why=f"no owner post with {metric} > 0")
                    continue
                self._ask(step, [f"Posts with {metric} > 0: {len(items)}. Posts with a count of zero are skipped."])
                fn(items)
                s.status = "done"

            if self.seeds_n > 0:
                current = 6
                self._run_step_seeds()

            current = 5
            s5 = res.step(5)
            first_max = s5.est_resources
            missing = self._missing_profiles()
            s5.est_resources, s5.est_kind = len(missing), "exact"
            if not missing:
                s5.status = "skipped"
                self.store.set_cursor(5, "profiles", None, True, self.run_id)
            else:
                self._ask(5, [
                    f"This is the second approval for step 5. The first estimate was a maximum of {first_max} users; "
                    f"after steps {'3, 4, and 6' if self.seeds_n > 0 else '3 and 4'} the exact count is {len(missing)} users "
                    f"in {math.ceil(len(missing) / USERS_PER_LOOKUP)} calls."
                ])
                self._step_profiles(missing)
                s5.status = "done"
            self.store.set_meta(META_WINDOW, "")
            self.store.set_meta(META_SEED_PLAN, "")
            prev = self.store.get_meta(META_COMPLETE_WINDOW) or ""
            # An incremental run keeps the start of the window that it extends.
            self.store.set_meta(META_COMPLETE_WINDOW, min(prev, self.window_start) if (prev and self.since) else self.window_start)
        except CapReached as e:
            res.cap_reached = e
            res.stopped_reason = str(e)
            res.remaining_plan = e.remaining
            self._mark_stopped(current)
        except _Stop as e:
            res.stopped_reason = e.reason
            res.remaining_plan = self._remaining_from(current)
            self._mark_stopped(current)
        except (XApiError, AuthError) as e:
            res.stopped_reason = f"{type(e).__name__}: {e}"
            res.remaining_plan = self._remaining_from(current)
            self._mark_stopped(current)
        except Exception as e:
            res.stopped_reason = f"unexpected error: {type(e).__name__}"
            res.remaining_plan = self._remaining_from(current)
            self._mark_stopped(current)
            raise
        finally:
            res.total_cost = self.store.ledger_total(self.run_id)
            res.requests_sent = self.client.requests_sent
            self._finish()
        return res

    def _run_step_seeds(self) -> None:
        """Step 6 (FR-07): select the seeds after steps 1 to 4, ask, and read their timelines."""
        s6 = self.result.step(6)
        saved = self._saved_seed_plan()
        plan = saved or self._new_seed_plan()
        extra = self._seed_estimate(plan)
        if saved is not None:
            extra.append("This run continues the seed plan of the open window (the same seeds).")
        if not plan["seeds"]:
            s6.status = "skipped"
            self._log("collect_step_skipped", step=6, why="no contact can be a seed")
            self.result.notes.append("step 6 (seed posts): no contact in the store can be a seed.")
            return
        if not self._seed_left(plan):
            s6.status = "done"
            return
        self._ask(6, extra)
        if saved is None:
            self.store.set_meta(META_SEED_PLAN, json.dumps(plan))
        self._step_seeds(plan)
        s6.status = "done"

    def _mark_stopped(self, current: int) -> None:
        pos = self._pos(current)
        for i, s in enumerate(self.result.steps):
            if s.step == current and s.status == "pending":
                s.status = "stopped"
            elif i > pos and s.status == "pending":
                s.status = "not_run"
        self._log("collect_stopped", step=current, reason=self.result.stopped_reason)

    def _finish(self) -> None:
        res = self.result
        counts = {
            "complete": res.complete,
            "stopped_reason": res.stopped_reason,
            "window_start": res.window_start,
            "window_end": now_iso(),
            "window_days": int(self.cfg.get("collect.window_days", 0)),
            "cost_usd": round(res.total_cost, 6),
            "cap_usd": self.cap,
            "interactions_added": sum(s.interactions_added for s in res.steps),
            "interactions_total": int(self.store.q1("SELECT COUNT(*) AS n FROM interactions")["n"]),
            "profiles_total": int(self.store.q1("SELECT COUNT(*) AS n FROM profiles")["n"]),
            "profile_errors_total": int(self.store.q1("SELECT COUNT(*) AS n FROM profile_errors")["n"]),
            "posts_total": int(self.store.q1("SELECT COUNT(*) AS n FROM posts")["n"]),
            "steps": {
                str(s.step): {
                    "status": s.status, "est_resources": s.est_resources, "est_kind": s.est_kind,
                    "est_cost": None if s.est_cost is None else round(s.est_cost, 6),
                    "actual_resources": s.actual_resources, "actual_cost": round(s.actual_cost, 6),
                    "calls": s.calls, "cached_pages": s.cached_pages,
                }
                for s in res.steps
            },
            "warnings": len(res.warnings),
        }
        if res.stage_run_id:
            self.store.finish_stage(res.stage_run_id, counts)
        self._log("collect_end", complete=res.complete, cost=round(res.total_cost, 6), interactions_added=counts["interactions_added"])


def run_collect(
    cfg: Config,
    store: Store,
    cache: Cache | None,
    log: RunLog | None,
    client: XApiClient | None,
    approve: Callable[[str], bool],
    dry_run: bool = False,
    run_id: str | None = None,
    *,
    now: datetime | None = None,
    full: bool = False,
) -> CollectResult:
    """Run the plan for the authenticated owner's account (FR-54): five steps, and step 6 with seeds.

    There is no parameter for a target account. On a live run the owner is
    the account of ``GET /2/users/me``, saved to meta ``ego_user_id`` and
    ``ego_handle``. With ``dry_run=True`` the function makes no HTTP call;
    ``client`` can be None.
    """
    if not dry_run and client is None:
        raise ValueError("a live collect needs an XApiClient")
    plan = _Plan(
        cfg, store, cache, log, None if dry_run else client, approve, dry_run,
        run_id or new_id("run"), now or datetime.now(UTC), full=full,
    )
    return plan.run()
