"""Network-level pattern detectors (score stage, table ``findings``).

The rules in ``signals.py`` look at one account at a time. The detectors in
this module compare accounts with each other: many accounts that reply in the
same minutes, accounts made in the same days, the same text from different
accounts, two names that look alike. Each detector gives findings. A finding
has a plain title and summary, the member user IDs, and evidence (counts,
times, post IDs, domains; never long text from X).

Design points:

- Thresholds are in ``config/rules.yaml`` under ``patterns:`` (MR-03). The
  defaults below are engineering decisions, explained in each docstring.
- A finding describes signals in the data, not persons (FR-31). Titles and
  summaries never hold a handle or text from X, so the deletion of a user ID
  (AD-10) leaves no copy of the user in a finding.
- No detector reads a sensitive-category input, the location, the bio, or a
  language (TR-06, research section 6.3). ``DETECTOR_INPUTS`` lists what each
  detector reads, and a test checks it.
- Ego-only data: the collector sees only the interactions with the owner. A
  campaign that targets many accounts shows here only as the part that
  touched the owner, so the coordination detectors have low recall by design.
  They are tuned for precision: a short list that a person reads.
- The coordination detectors (``reply_burst``, ``creation_cluster``,
  ``fast_reply``, ``shared_content``) also give per-account rule input
  (rules B02, B09 to B12) through ``PatternResult.members``, for findings at or
  above ``member_rule_min_severity``.

The score stage runs the detectors first, because rules B02 and B09 to B12
read their output. It writes the findings in the same transaction as the rule
results and the stage record.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..model import dumps, normalize_text
from .skeleton import lookalike_grade, lookalike_match, loose_key, loose_keys

SEVERITIES: tuple[str, ...] = ("info", "notable", "high")
INBOUND_TYPES: tuple[str, ...] = ("reply", "quote", "mention")


class PatternConfigError(ValueError):
    """The ``patterns`` section of the rule file is not valid."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOP_DEFAULTS: dict[str, Any] = {
    "count": 10,
    "require_owner_interaction": True,
}

DETECTOR_DEFAULTS: dict[str, dict[str, Any]] = {
    "reply_burst": {
        "enabled": True,
        "min_accounts": 4,
        "window_minutes": 15,
        "young_account_days": 180,
        "max_p": 0.05,
        "min_base_accounts": 30,
        "min_young_accounts": 3,
        "reach_neighbors": 20,
        "base_days": 14,
    },
    "creation_cluster": {
        "enabled": True,
        "min_accounts": 3,
        "window_hours": 72,
        "lookback_days": 1095,
        "max_p": 0.05,
        "young_account_days": 90,
        "local_days": 90,
        "short_local_days": 15,
    },
    "fast_reply": {
        "enabled": True,
        "min_replies": 3,
        "max_median_seconds": 60,
        "high_median_seconds": 15,
        "only_original_posts": True,
    },
    "shared_content": {
        "enabled": True,
        "min_accounts": 3,
        "min_text_chars": 40,
        "domain_window_days": 7,
        "common_site_accounts": 2,
        "ignore_domains": [],
    },
    "lookalike": {
        "enabled": True,
        "min_handle_length": 5,
        "long_handle_min_length": 9,
        "max_handle_edit_distance": 2,
        "min_name_key_length": 8,
        "young_account_days": 365,
        # A display name is text from X, so many accounts can share one key. A
        # bucket larger than this compares each member with the owner and the
        # known accounts only (review 20261103, B-03).
        "max_bucket_size": 50,
        "max_info_findings": 200,
    },
    "profile_outliers": {
        "enabled": True,
        "min_accounts": 20,
        "z": 3.5,
        "max_findings": 10,
    },
    "one_way": {
        "enabled": True,
        "min_inbound": 5,
        "min_posts": 3,
    },
    "tight_group": {
        "enabled": True,
        "min_size": 3,
        "max_findings": 10,
        # The count of maximal cliques can grow exponentially with the core number.
        # Above this core number the detector reports the densest core as one group
        # (review 20261103, B-13).
        "max_core": 15,
    },
}

# Detectors in network.py (owner request 2026-11-03). Each docstring there gives the reasons.
NETWORK_DEFAULTS: dict[str, dict[str, Any]] = {
    "co_action": {
        "enabled": True,
        "window_minutes": 10,
        "min_sync": 3,
        "max_p": 0.01,
        "min_q": 0.002,
        "max_actors": 300,
        "max_findings": 20,
    },
    "profile_change": {
        "enabled": True,
        "churn_min": 3,
    },
    "mass_tagging": {
        "enabled": True,
        "min_fanout": 4,
        "min_posts": 3,
        "min_tagged": 10,
        "max_retag_share": 0.5,
        "max_linked_share": 0.2,
    },
    "community_anomaly": {
        "enabled": True,
        "min_size": 8,
        "window_days": 30,
        "min_in_window": 4,
        "local_days": 180,
        "max_p": 0.01,
        "random_seed": 7,
        "max_findings": 10,
    },
}

NETWORK_INPUTS: dict[str, tuple[str, ...]] = {
    "co_action": ("interactions.type", "interactions.source_user_id", "interactions.target_post_id",
                  "interactions.interaction_created_at", "posts.created_at", "posts.public_metrics"),
    "profile_change": ("profile_history.handle", "profile_history.name", "profile_history.fetched_at",
                       "profiles.created_at"),
    "mass_tagging": ("interactions.type", "interactions.source_user_id", "interactions.target_user_id",
                     "interactions.source_post_id", "interactions.source_endpoint"),
    "community_anomaly": ("interactions.source_user_id", "interactions.target_user_id", "profiles.created_at",
                          "profiles.followers_count", "profiles.following_count", "profiles.profile_image_url",
                          "profiles.fetched_at"),
}

DETECTOR_DEFAULTS.update(NETWORK_DEFAULTS)

# What each detector reads (TR-06 test). "interactions.*" and "profiles.*" are
# store columns; "posts.*" are owner posts.
DETECTOR_INPUTS: dict[str, tuple[str, ...]] = {
    "reply_burst": ("interactions.type", "interactions.source_user_id", "interactions.target_post_id",
                    "interactions.interaction_created_at", "profiles.created_at", "posts.public_metrics"),
    "creation_cluster": ("interactions.source_user_id", "profiles.created_at", "profiles.fetched_at"),
    "fast_reply": ("interactions.type", "interactions.target_post_id", "interactions.interaction_created_at",
                   "posts.created_at", "posts.referenced"),
    "shared_content": ("interactions.text", "interactions.urls", "interactions.interaction_created_at"),
    "lookalike": ("profiles.handle", "profiles.name", "profiles.created_at"),
    "profile_outliers": ("profiles.followers_count", "profiles.following_count", "profiles.tweet_count",
                         "profiles.created_at", "profiles.fetched_at"),
    "one_way": ("interactions.type", "interactions.source_user_id", "interactions.target_user_id",
                "interactions.source_post_id", "interactions.interaction_created_at"),
    "tight_group": ("interactions.type", "interactions.source_user_id", "interactions.target_user_id"),
}

COORDINATION: frozenset[str] = frozenset({"reply_burst", "creation_cluster", "fast_reply", "shared_content"})
DETECTOR_INPUTS.update(NETWORK_INPUTS)
# Detectors whose findings give a per-account rule (B02, B09 to B14, I04).
RULE_DETECTORS: frozenset[str] = COORDINATION | frozenset({"co_action", "mass_tagging", "profile_change"})


@dataclass(frozen=True)
class PatternConfig:
    detectors: dict[str, dict[str, Any]]
    top_contacts: dict[str, Any]
    member_rule_min_severity: str


def pattern_config(raw: Any) -> PatternConfig:
    """Check the ``patterns`` section and merge it with the defaults."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PatternConfigError("patterns: must be a mapping")
    extra = set(raw) - {"detectors", "top_contacts", "member_rule_min_severity"}
    if extra:
        raise PatternConfigError(f"patterns: unknown keys {sorted(extra)}")
    sev = str(raw.get("member_rule_min_severity", "notable"))
    if sev not in SEVERITIES:
        raise PatternConfigError(f"patterns.member_rule_min_severity: {sev!r} is not one of {SEVERITIES}")
    top = dict(TOP_DEFAULTS)
    tc = raw.get("top_contacts") or {}
    if not isinstance(tc, dict) or set(tc) - set(TOP_DEFAULTS):
        raise PatternConfigError(f"patterns.top_contacts: keys can be only {sorted(TOP_DEFAULTS)}")
    top.update(tc)
    dets_raw = raw.get("detectors") or {}
    if not isinstance(dets_raw, dict):
        raise PatternConfigError("patterns.detectors: must be a mapping")
    unknown = set(dets_raw) - set(DETECTOR_DEFAULTS)
    if unknown:
        raise PatternConfigError(f"patterns.detectors: unknown detectors {sorted(unknown)}")
    dets: dict[str, dict[str, Any]] = {}
    for name, defaults in DETECTOR_DEFAULTS.items():
        given = dets_raw.get(name) or {}
        if not isinstance(given, dict):
            raise PatternConfigError(f"patterns.detectors.{name}: must be a mapping")
        bad = set(given) - set(defaults)
        if bad:
            raise PatternConfigError(f"patterns.detectors.{name}: unknown keys {sorted(bad)}")
        merged = {**defaults, **given}
        for k, v in merged.items():
            dv = defaults[k]
            if isinstance(dv, bool):
                ok = isinstance(v, bool)
            elif isinstance(dv, (int, float)):
                ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
            elif isinstance(dv, list):
                ok = isinstance(v, list)
            else:
                ok = True
            if not ok:
                raise PatternConfigError(f"patterns.detectors.{name}.{k}: wrong type or negative ({v!r})")
        dets[name] = merged
    return PatternConfig(dets, top, sev)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class PatternData:
    ego_id: str | None
    ego_handle: str | None
    interactions: list[dict[str, Any]]
    profiles: dict[str, dict[str, Any]]
    ego_posts: dict[str, dict[str, Any]]
    allowlist: frozenset[str]
    tie_weight: dict[str, float]
    profile_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class Finding:
    detector: str
    title: str
    severity: str
    summary: str
    members: list[str]
    evidence: dict[str, Any]
    anchor: str = ""
    # The members that get the per-account rule (B09 to B12). None means all members.
    rule_members: list[str] | None = None

    @property
    def finding_id(self) -> str:
        key = f"{self.detector}|{self.anchor}|{','.join(sorted(self.members))}"
        return "f_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class PatternResult:
    findings: list[Finding] = field(default_factory=list)
    # rule key -> user ID -> cause fields (for B02, B09 to B12)
    members: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    known_accounts: tuple[dict[str, Any], ...] = ()
    ego_posts: dict[str, dict[str, Any]] = field(default_factory=dict)  # owner posts (rule B08)

    def by_detector(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.detector] = out.get(f.detector, 0) + 1
        return dict(sorted(out.items()))


def _t(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pf(p: dict[str, Any] | None, name: str) -> Any:
    """A profile value if its field_status is 'present', else None (DR-02: never a made-up value)."""
    if p is None or p.get("field_status", {}).get(name) != "present":
        return None
    return p.get(name)


def _if(i: dict[str, Any], name: str) -> Any:
    if i.get("field_status", {}).get(name) != "present":
        return None
    return i.get(name)


def _rank(sev: str) -> int:
    return SEVERITIES.index(sev)


def _ego_posts(store: Any, ego: str | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not ego:
        return out
    for r in store.q("SELECT post_id, created_at, public_metrics_json, referenced_json FROM posts WHERE author_id=?", (ego,)):
        m = json.loads(r["public_metrics_json"]) if r["public_metrics_json"] else {}
        refs = json.loads(r["referenced_json"]) if r["referenced_json"] else []
        out[r["post_id"]] = {
            "created_at": r["created_at"],
            "impressions": m.get("impression_count"),
            "replies": m.get("reply_count"),
            "original": not any(isinstance(x, dict) and x.get("type") == "replied_to" for x in refs),
        }
    return out


def _tie_weight(store: Any, ego: str | None, interactions: list[dict[str, Any]]) -> dict[str, float]:
    """Tie weight of each account with the owner: ``nodes.ego_weight`` from the build stage.

    If the build stage wrote no weights (a store from tests), the weight is the
    count of interactions between the account and the owner.
    """
    w: dict[str, float] = {}
    for r in store.q("SELECT user_id, ego_weight FROM nodes WHERE is_ego=0 AND ego_weight IS NOT NULL"):
        if r["ego_weight"] > 0:
            w[r["user_id"]] = float(r["ego_weight"])
    if w or not ego:
        return w
    for i in interactions:
        s, t = i["source_user_id"], i["target_user_id"]
        if s == ego and t != ego:
            w[t] = w.get(t, 0.0) + 1.0
        elif t == ego and s != ego:
            w[s] = w.get(s, 0.0) + 1.0
    return w


def load_pattern_data(store: Any, allowlist: frozenset[str]) -> PatternData:
    ego = store.ego_id
    items = store.interactions()
    return PatternData(
        ego_id=ego,
        ego_handle=store.get_meta("ego_handle"),
        interactions=items,
        profiles=store.profiles(),
        ego_posts=_ego_posts(store, ego),
        allowlist=allowlist,
        tie_weight=_tie_weight(store, ego, items),
        profile_history=store.profile_history(),
    )


def known_accounts(d: PatternData, top: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Allowlist accounts and the owner's top contacts by tie weight (rule I02, detector lookalike).

    A top contact must have an interaction from the owner (reply, quote, or
    mention) when ``require_owner_interaction`` is set: a close contact is an
    account that the owner talks to, not only an account that talks to the owner.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for uid in sorted(d.allowlist):
        p = d.profiles.get(uid)
        if uid == d.ego_id or _pf(p, "handle") is None:
            continue
        out.append({"user_id": uid, "handle": _pf(p, "handle"), "name": _pf(p, "name"),
                    "created_at": _pf(p, "created_at"), "role": "allowlist"})
        seen.add(uid)
    owner_to: set[str] = set()
    for i in d.interactions:
        if i["source_user_id"] == d.ego_id and i["type"] in INBOUND_TYPES:
            owner_to.add(i["target_user_id"])
    ranked = sorted(d.tie_weight.items(), key=lambda kv: (-kv[1], kv[0]))
    n = 0
    for uid, _w in ranked:
        if n >= int(top["count"]):
            break
        if uid in seen or uid == d.ego_id:
            continue
        if top["require_owner_interaction"] and uid not in owner_to:
            continue
        p = d.profiles.get(uid)
        if _pf(p, "handle") is None:
            continue
        out.append({"user_id": uid, "handle": _pf(p, "handle"), "name": _pf(p, "name"),
                    "created_at": _pf(p, "created_at"), "role": "contact"})
        seen.add(uid)
        n += 1
    return tuple(out)


def _excluded(d: PatternData) -> set[str]:
    out = set(d.allowlist)
    if d.ego_id:
        out.add(d.ego_id)
    return out


def _inbound(d: PatternData) -> list[dict[str, Any]]:
    skip = _excluded(d)
    return [i for i in d.interactions
            if i["target_user_id"] == d.ego_id and i["source_user_id"] not in skip and i["type"] in INBOUND_TYPES]


def _age_days(d: PatternData, uid: str, at: datetime | None = None) -> float | None:
    p = d.profiles.get(uid)
    created = _t(_pf(p, "created_at"))
    ref = at or (_t(p.get("fetched_at")) if p else None)
    if created is None or ref is None:
        return None
    return (ref - created).total_seconds() / 86400.0


def _windows(times: list[datetime], width_seconds: float) -> list[tuple[int, int]]:
    """For each start i (sorted times), the end j (exclusive) of the window [t_i, t_i + width]."""
    out: list[tuple[int, int]] = []
    j = 0
    for i, t in enumerate(times):
        j = max(j, i)
        while j < len(times) and (times[j] - t).total_seconds() <= width_seconds:
            j += 1
        out.append((i, j))
    return out


def _merge_qualifying(spans: list[tuple[int, int]], ok: Callable[[int, int], bool]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for i, j in spans:
        if not ok(i, j):
            continue
        if merged and i < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], j)
        else:
            merged.append([i, j])
    return [(a, b) for a, b in merged]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---------------------------------------------------------------------------
# a. Coordinated burst
# ---------------------------------------------------------------------------


def _binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Shared with the network detectors.

    The terms use ``lgamma`` in log space. ``math.comb(n, j) * p ** j`` gives an
    integer too large for a float above about n = 1030, and that stopped the
    score stage (review 20261103, B-05).
    """
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    lg_n = math.lgamma(n + 1)
    total = 0.0
    for j in range(k, n + 1):
        total += math.exp(lg_n - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * log_p + (n - j) * log_q)
    return min(1.0, total)


def _young_events(d: PatternData, young_days: float) -> list[tuple[datetime, str, bool]]:
    """(time, account, young at that time) for each timed inbound interaction with a known account age, by time."""
    out: list[tuple[datetime, str, bool]] = []
    for i in _inbound(d):
        t = _t(_if(i, "interaction_created_at"))
        if t is None:
            continue
        a = _age_days(d, i["source_user_id"], t)
        if a is not None:
            out.append((t, i["source_user_id"], a < young_days))
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def _young_rate(d: PatternData, young_days: float) -> tuple[float, int]:
    """(fraction, count): of the accounts that contacted the owner with a known age, the fraction that
    were younger than ``young_days`` at their first timed contact."""
    first: dict[str, datetime] = {}
    for i in _inbound(d):
        t = _t(_if(i, "interaction_created_at"))
        u = i["source_user_id"]
        if t is not None and (u not in first or t < first[u]):
            first[u] = t
    ages = [_age_days(d, u, t) for u, t in first.items()]
    known = [a for a in ages if a is not None]
    if not known:
        return 0.0, 0
    return sum(1 for a in known if a < young_days) / len(known), len(known)


def _peak_window(evs: list[tuple[datetime, str, dict[str, Any]]], width: float) -> tuple[int, int, int]:
    """(count, i, j): the largest count of different accounts in one window of ``width`` seconds (sorted events)."""
    best = (0, 0, 0)
    for i, j in _windows([e[0] for e in evs], width):
        n = len({e[1] for e in evs[i:j]})
        if n > best[0]:
            best = (n, i, j)
    return best


def _peak(evs: list[tuple[datetime, str, dict[str, Any]]], width: float) -> int:
    return _peak_window(evs, width)[0]


def detect_reply_burst(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """``min_accounts`` or more different accounts reply to, quote, or mention one owner post within ``window_minutes``.

    The anchor is the owner post (``target_post_id`` of an owner post). An
    interaction with no owner post as its target (for example a mention in
    another thread) has the anchor "owner". Overlapping windows merge into one
    finding.

    Default 4 accounts in 15 minutes. A post with reach gets fast organic
    replies from many accounts, so the size of a burst alone is not a signal:
    the benchmark (tests/validation) showed bursts of 8 to 50 established
    accounts in the first hour of each popular post. A burst is ``notable``
    if one of two tests finds a surprise:

    - Young accounts. The count of members younger than ``young_account_days``
      in one window, against the young share of the owner's contacts (the
      larger of all contacts and the contacts within ``base_days`` of the
      burst, at least 1 %): the binomial tail P(Y >= y), times the count of bursts
      (Bonferroni), at or below ``max_p``, with ``min_young_accounts`` or
      more young members (3: a group, as in the other detectors). 4
      young accounts alone pass; 3 young accounts of 5 do not when 10 % of
      the contacts are young (the benchmark showed that case by chance). With
      fewer than ``min_base_accounts`` contacts of known age, the share has no
      meaning, and the test is: half or more of the members were young.
    - Size for the reach. The peak count of accounts in one window is
      2 * ``min_accounts`` or more, and 2 times or more the highest peak of
      the ``reach_neighbors`` owner posts with the nearest impression count,
      and the peak window has ``min_young_accounts`` or more young members and
      2 times or more the young share. A post that asks for replies (an
      announcement) gets many organic replies for its reach, but not more
      young accounts (benchmark, held-out background).
      Each neighbor peak is first scaled up by the ratio of the impressions
      (linear, never down), so the post with the most reach is not a surprise
      only because its neighbors have less. The margin is a factor, not a
      count, because organic peaks vary in proportion to their size (in the
      benchmark the variance was up to 4 times the mean). This finds a large
      mixed group of young and established accounts on a post that normally
      gets few replies. It needs impressions and ``reach_neighbors`` other
      owner posts with impressions. A group of only established accounts is
      not found: in the owner's data it looks the same as organic replies.

    ``high`` if both tests find a surprise, or if the young test finds twice
    ``min_accounts`` young members. Else ``info``.

    Only ``notable`` and ``high`` bursts give rule B09: to the young members
    for the young test (an established account that replied in the same
    minutes is usually organic), and to all members for the size test.
    """
    width = float(c["window_minutes"]) * 60.0
    young_days = float(c["young_account_days"])
    n_min = int(c["min_accounts"])
    k_near = int(c["reach_neighbors"])
    groups: dict[str, list[tuple[datetime, str, dict[str, Any]]]] = defaultdict(list)
    for i in _inbound(d):
        t = _t(_if(i, "interaction_created_at"))
        if t is None:
            continue
        tp = _if(i, "target_post_id")
        anchor = tp if tp and tp in d.ego_posts else "owner"
        groups[anchor].append((t, i["source_user_id"], i))
    for evs in groups.values():
        evs.sort(key=lambda e: (e[0], e[1]))
    base_global, base_n = _young_rate(d, young_days)
    use_base = base_n >= int(c["min_base_accounts"])
    yev = _young_events(d, young_days)
    ytimes = [e[0] for e in yev]
    near_s = float(c["base_days"]) * 86400.0

    def base_at(t: datetime, exclude: set[str]) -> float:
        """The young share of the other accounts that contacted the owner within ``base_days`` of t.

        New accounts join over time, so late in the data more contacts are
        young. The share is the larger of this local value and the share of
        all contacts, at least 1 %.
        """
        lo = bisect.bisect_left(ytimes, t - timedelta(seconds=near_s))
        hi = bisect.bisect_right(ytimes, t + timedelta(seconds=near_s))
        seen: dict[str, bool] = {}
        for _, u, young_then in yev[lo:hi]:
            if u not in exclude and u not in seen:
                seen[u] = young_then
        local = sum(seen.values()) / len(seen) if len(seen) >= int(c["min_base_accounts"]) else 0.0
        return max(base_global, local, 0.01)
    # Peak count of each owner post with impressions (posts with no reply have 0).
    reach: list[tuple[float, str]] = []
    peaks: dict[str, int] = {}
    for pid, post in d.ego_posts.items():
        imp = post.get("impressions")
        if isinstance(imp, (int, float)) and not isinstance(imp, bool) and imp > 0 and post.get("original"):
            reach.append((math.log(float(imp)), pid))
            peaks[pid] = _peak(groups.get(pid, []), width)
    bursts: list[tuple[str, list[tuple[datetime, str, dict[str, Any]]]]] = []
    for anchor, evs in sorted(groups.items()):
        times = [e[0] for e in evs]

        def ok(i: int, j: int, evs: list[tuple[datetime, str, dict[str, Any]]] = evs) -> bool:
            return len({e[1] for e in evs[i:j]}) >= n_min

        for a, b in _merge_qualifying(_windows(times, width), ok):
            bursts.append((anchor, evs[a:b]))

    log_reach = {q: x for x, q in reach}

    def neighbor_peak(pid: str) -> float | None:
        """The highest peak of the nearest-reach posts, each scaled up to this post's reach (never down)."""
        if pid not in peaks or len(reach) - 1 < k_near:
            return None
        mine = log_reach[pid]
        near = sorted((abs(x - mine), q) for x, q in reach if q != pid)[:k_near]
        return max(peaks[q] * max(1.0, math.exp(mine - log_reach[q])) for _, q in near)

    out: list[Finding] = []
    for anchor, whole in bursts:
        all_members = sorted({e[1] for e in whole})
        ages = {u: _age_days(d, u, whole[0][0]) for u in all_members}
        young_all = {u for u, x in ages.items() if x is not None and x < young_days}
        known_age = sum(1 for x in ages.values() if x is not None)
        # Young test on the window with the strongest young count.
        p_adj: float | None = None
        young_hit = False
        young_win = (0, len(whole))
        base = base_at(whole[0][0], set(all_members)) if use_base else base_global
        if use_base:
            best = 1.0
            tested: set[frozenset[str]] = set()
            for i, j in _windows([e[0] for e in whole], width):
                us = {e[1] for e in whole[i:j]}
                y = len(us & young_all)
                if y >= int(c["min_young_accounts"]):
                    tested.add(frozenset(us))
                    pv = _binom_tail(y, len(us), base)
                    if pv < best:
                        best, young_win = pv, (i, j)
            # Bonferroni over the bursts and over the different account sets that
            # were tested in this burst: the smallest p of many windows is not a
            # p-value of one test (review 20261103, B-10).
            p_adj = min(1.0, best * len(bursts) * max(1, len(tested)))
            young_hit = p_adj <= float(c["max_p"])
        else:
            # The low-base branch also needs the minimum count of young accounts:
            # 2 young accounts in a burst of 4 are not a finding (review 20261103, B-09).
            young_hit = (bool(known_age) and len(young_all) >= int(c["min_young_accounts"])
                         and len(young_all) * 2 >= len(all_members))
        # Size test against posts with a similar reach, with a surplus of young accounts.
        peak, pi, pj = _peak_window(whole, width)
        near_peak = neighbor_peak(anchor) if anchor != "owner" else None
        peak_young = len({e[1] for e in whole[pi:pj]} & young_all)
        size_hit = (near_peak is not None and peak >= 2 * n_min and peak >= 2 * near_peak
                    and peak_young >= int(c["min_young_accounts"]) and peak_young >= 2 * base * peak)
        # A finding that fires covers only the window that fired: the merged
        # burst on a busy post also holds organic replies from other minutes.
        part = whole
        if young_hit and use_base:
            part = whole[young_win[0]:young_win[1]]
        elif size_hit:
            part = whole[pi:pj]
        members = sorted({e[1] for e in part})
        young_ids = sorted(u for u in members if u in young_all)
        young = len(young_ids)
        sev = "info"
        rule_members: list[str] | None = None
        if young_hit or size_hit:
            sev = "high" if (young_hit and size_hit) or (young_hit and young >= 2 * n_min) else "notable"
            rule_members = None if size_hit else young_ids
        span = (part[-1][0] - part[0][0]).total_seconds()
        minutes = max(1, math.ceil(span / 60.0))
        types = sorted({e[2]["type"] for e in part})
        post = d.ego_posts.get(anchor) if anchor != "owner" else None
        where = "one of your posts" if post is not None else "you"
        verb = "replied to, quoted, or mentioned"
        title = f"{len(members)} accounts {verb} {where} within {_plural(minutes, 'minute')}"
        summary = (
            f"{len(members)} different accounts sent {len(part)} {('/'.join(types))} interactions to {where} "
            f"between {_iso(part[0][0])} and {_iso(part[-1][0])}. "
            f"{young} of them were younger than {int(young_days)} days at that time"
            + (f" (about {base * len(members):.1f} expected from your usual contacts)" if use_base else "")
            + "."
            + (f" At most about {near_peak:.0f} accounts came in {int(c['window_minutes'])} minutes on your "
               f"posts with a similar reach; this burst had {peak}." if near_peak is not None else "")
            + " Fast replies from many accounts can be organic on a popular post; "
            "compare with the reach of the post."
        )
        ev: dict[str, Any] = {
            "anchor_post_id": None if anchor == "owner" else anchor,
            "first": _iso(part[0][0]), "last": _iso(part[-1][0]), "span_seconds": int(span),
            "interactions": len(part), "accounts": len(members), "young_accounts": young,
            "young_members": young_ids, "young_share_of_contacts": round(base, 4) if use_base else None,
            "p_adjusted": None if p_adj is None else round(p_adj, 6),
            "peak_accounts": peak, "similar_reach_peak": None if near_peak is None else round(near_peak, 1),
            "source_post_ids": sorted({e[2]["source_post_id"] for e in part if e[2].get("source_post_id")}),
        }
        if post is not None:
            ev["post_impressions"] = post.get("impressions")
            ev["post_created_at"] = post.get("created_at")
        out.append(Finding("reply_burst", title, sev, summary, members, ev,
                           anchor=f"{anchor}|{_iso(part[0][0])}", rule_members=rule_members))
    return out


# ---------------------------------------------------------------------------
# b. Account-creation cluster
# ---------------------------------------------------------------------------


def _poisson_tail(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    term = math.exp(-mu)
    cdf = term
    for n in range(1, k):
        term *= mu / n
        cdf += term
    return max(0.0, 1.0 - cdf)


def detect_creation_cluster(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """``min_accounts`` or more accounts that contacted the owner were created within ``window_hours``.

    Only accounts created in the last ``lookback_days`` count: old accounts
    from the same sign-up wave say nothing. A cluster must also be a surprise:
    with n accounts in the look-back period, the expected count in one window
    is mu = n * window / lookback. The Poisson tail P(X >= k), times the count
    of windows in the look-back period (a Bonferroni correction), must be at or
    below ``max_p``. So a large contact list needs a larger cluster: with 30
    recent accounts, 3 in 72 hours is enough; with 100, it takes 5.

    Account creation times are not uniform over the look-back period: a
    contact list has more accounts from some months than from others. So mu is
    the larger of the uniform value and the local value: the accounts created
    within ``local_days`` before and after the window (not in it), per hour,
    times the window. The benchmark (tests/validation) showed false clusters
    in each contact list of 1200 or more with the uniform value alone. The
    local value is calculated two times, with ``local_days`` and with
    ``short_local_days``, and the larger value counts. The number of new
    accounts grows toward the present, so near the end of the data the wide
    neighborhood mixes in older, sparser months and gives a rate that is too
    low (benchmark with seed timelines, 2026-11-03: 9 new real accounts in 58
    hours in a list of 1200).

    Severity ``notable``; ``high`` if each member is younger than
    ``young_account_days`` or the cluster has twice the minimum.
    """
    created: dict[str, datetime] = {}
    ref: datetime | None = None
    for i in _inbound(d):
        u = i["source_user_id"]
        p = d.profiles.get(u)
        t = _t(_pf(p, "created_at"))
        f = _t(p.get("fetched_at")) if p else None
        if t is None:
            continue
        created[u] = t
        if f is not None and (ref is None or f > ref):
            ref = f
    if ref is None:
        return []
    lookback = float(c["lookback_days"])
    recent = sorted((t, u) for u, t in created.items() if (ref - t).total_seconds() / 86400.0 <= lookback)
    n = len(recent)
    n_min = int(c["min_accounts"])
    if n < n_min:
        return []
    width_h = float(c["window_hours"])
    span_h = lookback * 24.0
    mu = n * width_h / span_h
    n_windows = max(1.0, span_h / width_h)
    times = [t for t, _ in recent]
    scales_h = (float(c["local_days"]) * 24.0, float(c["short_local_days"]) * 24.0)

    first_t = ref - timedelta(days=lookback)

    def local_rate(i: int, j: int, local_h: float) -> float:
        lo_t = max(first_t, times[i] - timedelta(hours=local_h))
        hi_t = min(ref, times[j - 1] + timedelta(hours=local_h))
        lo = bisect.bisect_left(times, lo_t)
        hi = bisect.bisect_right(times, hi_t)
        near = (hi - lo) - (j - i)
        # The hours of the neighborhood: clipped to the look-back period and
        # to the reference time (no account is newer than the data), without
        # the window itself.
        hours = ((hi_t - lo_t) - (times[j - 1] - times[i])).total_seconds() / 3600.0
        return near / max(hours, width_h) * width_h

    def mu_at(i: int, j: int) -> float:
        return max(mu, *(local_rate(i, j, h) for h in scales_h if h > 0))

    def ok(i: int, j: int) -> bool:
        k = j - i
        return k >= n_min and _poisson_tail(k, mu_at(i, j)) * n_windows <= float(c["max_p"])

    out: list[Finding] = []
    spans = _windows(times, width_h * 3600.0)
    for a, b in _merge_qualifying(spans, ok):
        part = recent[a:b]
        members = sorted(u for _, u in part)
        k = len(members)
        # The p-value comes from the best single window in the merged span. The
        # count of the merged span against the expected count of one window gave
        # a p-value that was too small (review 20261103, B-10).
        k_w, mu_w = max(((j - i, mu_at(i, j)) for i, j in spans if a <= i and j <= b and ok(i, j)),
                        key=lambda x: (-_poisson_tail(x[0], x[1]), x[0]))
        p_adj = min(1.0, _poisson_tail(k_w, mu_w) * n_windows)
        ages = [(ref - t).total_seconds() / 86400.0 for t, _ in part]
        sev = "notable"
        if all(x < float(c["young_account_days"]) for x in ages) or k >= 2 * n_min:
            sev = "high"
        hours = max(1, math.ceil((part[-1][0] - part[0][0]).total_seconds() / 3600.0))
        title = f"{k} accounts that contacted you were created within {_plural(hours, 'hour')}"
        summary = (
            f"{k} of the {n} accounts made in the last {int(lookback)} days that contacted you were created "
            f"between {_iso(part[0][0])} and {_iso(part[-1][0])}. "
            f"By chance, about {mu_w:.2f} would be in one window of {int(width_h)} hours "
            f"(adjusted p = {p_adj:.3g}). Accounts made together are a common sign of a batch of accounts."
        )
        ev = {
            "first_created": _iso(part[0][0]), "last_created": _iso(part[-1][0]),
            "accounts": k, "recent_accounts": n, "expected_in_window": round(mu_w, 4),
            "p_adjusted": round(p_adj, 6), "youngest_age_days": int(min(ages)), "oldest_age_days": int(max(ages)),
        }
        out.append(Finding("creation_cluster", title, sev, summary, members, ev, anchor=_iso(part[0][0])))
    return out


# ---------------------------------------------------------------------------
# c. Fast-reply timing
# ---------------------------------------------------------------------------


def detect_fast_reply(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """An account whose replies to owner posts arrive within ``max_median_seconds`` (median, ``min_replies`` or more).

    The latency is the reply time minus the ``created_at`` of the owner post.
    With ``only_original_posts`` only replies to the owner's original posts
    count, not replies to the owner's replies: in a live conversation a fast
    answer is normal. A person who reads a notification and types a reply
    seldom does it in under a minute, three times or more. Automation that
    watches an account does. Severity ``notable``; ``high`` at or below
    ``high_median_seconds``.
    """
    lat: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for i in _inbound(d):
        if i["type"] != "reply":
            continue
        tp = _if(i, "target_post_id")
        post = d.ego_posts.get(tp) if tp else None
        if post is None or (c["only_original_posts"] and not post.get("original")):
            continue
        t = _t(_if(i, "interaction_created_at"))
        pt = _t(post.get("created_at"))
        if t is None or pt is None:
            continue
        s = (t - pt).total_seconds()
        if s < 0:
            continue  # clock or data error; never a made-up latency
        lat[i["source_user_id"]].append((s, tp))
    out: list[Finding] = []
    for u, xs in sorted(lat.items()):
        # One latency per owner post: the first reply.
        first: dict[str, float] = {}
        for s, tp in xs:
            first[tp] = min(s, first.get(tp, s))
        vals = sorted(first.values())
        if len(vals) < int(c["min_replies"]):
            continue
        med = statistics.median(vals)
        if med > float(c["max_median_seconds"]):
            continue
        sev = "high" if med <= float(c["high_median_seconds"]) else "notable"
        title = f"An account replies to your posts a median of {int(round(med))} seconds after you post"
        summary = (
            f"{len(vals)} replies to your original posts came a median of {int(round(med))} seconds "
            f"(fastest {int(vals[0])}, slowest {int(vals[-1])}) after the post. "
            "A person seldom replies this fast again and again; a program that watches your posts can."
        )
        ev = {"replies": len(vals), "median_seconds": round(med, 1), "min_seconds": round(vals[0], 1),
              "max_seconds": round(vals[-1], 1), "post_ids": sorted(first)}
        out.append(Finding("fast_reply", title, sev, summary, [u], ev, anchor=u))
    return out


# ---------------------------------------------------------------------------
# d. Shared text and shared links
# ---------------------------------------------------------------------------

DEFAULT_IGNORED_DOMAINS: frozenset[str] = frozenset({
    "x.com", "twitter.com", "t.co", "pic.twitter.com", "mobile.twitter.com",
    "youtube.com", "youtu.be", "github.com", "linkedin.com", "lnkd.in", "medium.com", "substack.com",
    "wikipedia.org", "en.wikipedia.org", "google.com", "docs.google.com", "reddit.com", "instagram.com",
    "facebook.com", "tiktok.com", "bsky.app", "threads.net", "arxiv.org", "nytimes.com", "bbc.co.uk",
})


def _norm_url(u: str) -> tuple[str, str] | None:
    """(normalized URL, host) or None. Lower-case host without ``www.``, no fragment, no ``utm_*`` parameters."""
    try:
        parts = urlsplit(u.strip() if "://" in u else "http://" + u.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/")
    norm = host + path + (("?" + urlencode(q)) if q else "")
    return norm, host


def _ignored(host: str, ignore: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in ignore)


def detect_shared_content(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """The same normalized text, the same link, or the same less common site, from ``min_accounts`` or more accounts.

    This is the cross-account side of rule B02 (research: fox8 coordination).
    Text: ``normalize_text`` (no URLs, no @handles, no punctuation) of
    ``min_text_chars`` or more characters, so short replies such as "great
    post" do not group. Link: the full normalized URL. Site: the host, without
    the common sites in ``DEFAULT_IGNORED_DOMAINS`` and ``ignore_domains``.

    Text and link groups are ``notable`` (``high`` at twice the minimum) and
    give rule B02. Two exceptions came from the benchmark (tests/validation):

    - ``min_text_chars`` is 40. Formulaic replies ("Congratulations Ron, well
      deserved!") are identical from many real persons and are shorter.
    - A link group is ``info`` if ``common_site_accounts`` or more other
      accounts linked to the same site more than ``domain_window_days``
      before or after the group. Such a site is common in the network (a news
      article that several contacts send to the owner on the day it appears).
      A campaign site is seldom linked in other weeks.
 A site group is ``notable`` and gives rule B12 only if all
    its posts are within ``domain_window_days``; else it is ``info``.
    The evidence holds the text hash, never the text.
    """
    n_min = int(c["min_accounts"])
    ignore = DEFAULT_IGNORED_DOMAINS | frozenset(str(x).lower() for x in c["ignore_domains"])
    skip = _excluded(d)
    by_text: dict[str, dict[str, Any]] = {}
    by_link: dict[str, dict[str, Any]] = {}
    by_host: dict[str, dict[str, Any]] = {}

    host_events: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    def add(table: dict[str, dict[str, Any]], key: str, i: dict[str, Any], host: str = "") -> None:
        g = table.setdefault(key, {"users": set(), "posts": set(), "times": [], "host": host})
        g["users"].add(i["source_user_id"])
        if i.get("source_post_id"):
            g["posts"].add(i["source_post_id"])
        t = _t(_if(i, "interaction_created_at"))
        if t is not None:
            g["times"].append(t)

    for i in d.interactions:
        if i["source_user_id"] in skip or i["type"] not in INBOUND_TYPES:
            continue
        text = _if(i, "text")
        if text:
            norm = normalize_text(str(text))
            if len(norm) >= int(c["min_text_chars"]):
                add(by_text, hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16], i)
        urls = _if(i, "urls") or []
        # After drop_text the row holds host names only: a host is not a link (A-11).
        hosts_only = i.get("field_status", {}).get("url_paths") == "absent"
        seen_hosts: set[str] = set()
        for u in urls:
            nu = _norm_url(str(u))
            if nu is None or _ignored(nu[1], ignore):
                continue
            if not hosts_only:
                add(by_link, nu[0], i, nu[1])
            if nu[1] not in seen_hosts:
                seen_hosts.add(nu[1])
                add(by_host, nu[1], i)
                t = _t(_if(i, "interaction_created_at"))
                if t is not None:
                    host_events[nu[1]].append((t, i["source_user_id"]))

    window = float(c["domain_window_days"]) * 86400.0

    def site_users_elsewhere(host: str, members: list[str], times: list[datetime]) -> int:
        """Other accounts that linked to ``host`` more than ``domain_window_days`` before or after the group."""
        if not times:
            return 0
        lo, hi = times[0], times[-1]
        mem = set(members)
        return len({u for t, u in host_events.get(host, ())
                    if u not in mem and ((lo - t).total_seconds() > window or (t - hi).total_seconds() > window)})

    out: list[Finding] = []
    linked_users: set[frozenset[str]] = set()
    for kind, table in (("text", by_text), ("link", by_link)):
        for key, g in sorted(table.items()):
            members = sorted(g["users"])
            if len(members) < n_min:
                continue
            sev = "high" if len(members) >= 2 * n_min else "notable"
            times = sorted(g["times"])
            elsewhere = site_users_elsewhere(g["host"], members, times) if kind == "link" else 0
            common = kind == "link" and elsewhere >= int(c["common_site_accounts"])
            if common:
                sev = "info"
            what = "the same text" if kind == "text" else "the same link"
            title = f"{len(members)} accounts sent {what}"
            summary = (
                f"{len(members)} different accounts sent {what} in {len(g['posts'])} posts"
                + (f" between {_iso(times[0])} and {_iso(times[-1])}" if times else "")
                + ". Copied text or links from many accounts is the strongest sign of coordination in the research."
                + (f" But {elsewhere} other accounts linked to the same site in other weeks, so the site is common "
                   "in your network (for example a news article that many persons share)." if common else "")
            )
            ev: dict[str, Any] = {"kind": kind, "accounts": len(members), "posts": sorted(g["posts"])}
            if kind == "text":
                ev["text_hash"] = key
            else:
                # The path stays (the owner needs to see which link), the query does not:
                # a query can hold an identifier of the person who shared it (B-16).
                ev["link"] = key.split("?", 1)[0][:200]
                ev["site_accounts_other_weeks"] = elsewhere
                linked_users.add(frozenset(members))
            if times:
                ev["first"], ev["last"] = _iso(times[0]), _iso(times[-1])
            out.append(Finding("shared_content", title, sev, summary, members, ev, anchor=f"{kind}|{key}"))
    for host, g in sorted(by_host.items()):
        members = sorted(g["users"])
        if len(members) < n_min:
            continue
        if frozenset(members) in linked_users:
            continue  # the same group already has a link finding
        times = sorted(g["times"])
        tight = bool(times) and (times[-1] - times[0]).total_seconds() <= window
        sev = "notable" if tight else "info"
        title = f"{len(members)} accounts linked to the same site"
        summary = (
            # The host is text from X: it stays in the evidence, not in the summary
            # (FR-31 check, review 20261103, B-02).
            f"{len(members)} different accounts linked to the same site in {len(g['posts'])} posts"
            + (f" between {_iso(times[0])} and {_iso(times[-1])}" if times else "")
            + "."
            + (f" All links came within {int(c['domain_window_days'])} days." if tight else "")
        )
        ev = {"kind": "domain", "domain": host[:120], "accounts": len(members), "posts": sorted(g["posts"])}
        if times:
            ev["first"], ev["last"] = _iso(times[0]), _iso(times[-1])
        out.append(Finding("shared_content", title, sev, summary, members, ev, anchor=f"domain|{host}"))
    return out


# ---------------------------------------------------------------------------
# e. Lookalike names
# ---------------------------------------------------------------------------


def _deletions(key: str, depth: int) -> set[str]:
    """``key`` and each string made from it by deleting up to ``depth`` characters."""
    out = {key}
    frontier = {key}
    for _ in range(depth):
        nxt: set[str] = set()
        for s in frontier:
            for i in range(len(s)):
                nxt.add(s[:i] + s[i + 1:])
        out |= nxt
        frontier = nxt
    return out


def _lookalike_candidates(
    rows: list[tuple[str, str, str | None, datetime | None]], c: dict[str, Any], priority: frozenset[int] = frozenset()
) -> list[tuple[int, int]]:
    """Index pairs (x < y) that can match in ``lookalike_match``. The other pairs cannot match.

    A comparison of each pair is O(n^2) and too slow for a few thousand
    accounts. Equal keys (handle, display name, handle with display name) come
    from a dictionary. A near handle comes from a symmetric-deletion index: if
    two keys are at most k edits apart, they share a string made from each by
    deleting at most k characters. k is the largest distance that
    ``allowed_distance`` can give for the length of the key.

    A bucket with more than ``max_bucket_size`` members gives only the pairs
    that include a ``priority`` row (the owner and the known accounts). Without
    this limit, n accounts with one display name give n(n-1)/2 pairs.
    """
    max_bucket = int(c["max_bucket_size"])
    min_len = int(c["min_handle_length"])
    long_len = int(c["long_handle_min_length"])
    max_d = int(c["max_handle_edit_distance"])
    min_name = int(c["min_name_key_length"])
    hk = [loose_key(r[1]) for r in rows]
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for x in range(len(rows)):
        if not hk[x]:
            continue
        for k in loose_keys(rows[x][1]):
            buckets[("h", k)].append(x)
        for k in (loose_keys(rows[x][2]) if rows[x][2] else ()):
            if len(k) >= min_name:
                buckets[("n", k)].append(x)
                buckets[("h", k)].append(x)  # handle of one = display name of the other
        k = 0
        if len(hk[x]) >= min_len:
            k = 1
        if len(hk[x]) >= long_len:
            k = 2
        for v in _deletions(hk[x], min(k, max_d)):
            buckets[("d", v)].append(x)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        uniq = sorted(set(members))
        if len(uniq) > max_bucket:
            for p in (x for x in uniq if x in priority):
                pairs.update((min(p, x), max(p, x)) for x in uniq if x != p)
            continue
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pairs.add((uniq[i], uniq[j]))
    return sorted(pairs)


def detect_lookalike(d: PatternData, c: dict[str, Any], known: tuple[dict[str, Any], ...]) -> list[Finding]:
    """Two accounts in the graph whose handles or display names look alike (UTS #39 skeleton or a near handle).

    See ``skeleton.lookalike_match``. The edit distance grows with the handle
    length (1 from ``min_handle_length``, 2 from ``long_handle_min_length``),
    so short handles do not match by chance. For a pair of two accounts that
    the owner does not know, a near handle counts only on long handles and at
    one letter. Other pairs of unknown accounts are ``info`` (often the work
    and the personal account of one person, or two persons with one name).

    A pair with the owner or a known account (allowlist or a top contact) gets
    its severity from ``skeleton.lookalike_grade``: ``high`` for an equal
    skeleton on a young account or on a copy of the owner, and for a near
    handle with a display name that also looks alike; ``notable`` for an old
    account with the same name as a known account; ``info`` for a near handle
    with another display name or on an account older than the reference. The benchmark (tests/validation) showed that
    without the age and name checks, common handles (``bschmidt`` and
    ``fschmidt``, ``amberlabs`` and ``emberlabs``) and persons with the same
    name gave ``high`` findings in each large contact list.

    The first member is the lookalike, the second is the reference (the owner,
    the known account, or the older account). The evidence counts the other
    accounts that interacted with the lookalike: users who confuse the two
    accounts send replies to the wrong one.
    """
    known_ids = {k["user_id"] for k in known}
    ego = d.ego_id
    rows: list[tuple[str, str, str | None, datetime | None]] = []
    for uid, p in sorted(d.profiles.items()):
        h = _pf(p, "handle")
        if not h:
            if uid == ego and d.ego_handle:
                h = d.ego_handle
            else:
                continue
        rows.append((uid, str(h).lstrip("@"), _pf(p, "name"), _t(_pf(p, "created_at"))))
    if ego and ego not in d.profiles and d.ego_handle:
        rows.append((ego, d.ego_handle.lstrip("@"), None, None))
    touch: dict[str, set[str]] = defaultdict(set)
    for i in d.interactions:
        touch[i["target_user_id"]].add(i["source_user_id"])
        touch[i["source_user_id"]].add(i["target_user_id"])
    def rank(r: tuple[str, str, str | None, datetime | None]) -> tuple[int, float]:
        """The reference is the owner, then a known account, then the older account."""
        role = 0 if r[0] == ego else (1 if r[0] in known_ids else 2)
        ts = r[3].timestamp() if r[3] is not None else float("inf")
        return role, ts

    out: list[Finding] = []
    priority = frozenset(i for i, r in enumerate(rows) if r[0] == ego or r[0] in known_ids)
    n_info = 0
    for x, y in _lookalike_candidates(rows, c, priority):
        a, b = rows[x], rows[y]
        m = lookalike_match(a[1], a[2], b[1], b[2], c)
        if m is None:
            continue
        ref, look = (a, b) if rank(a) <= rank(b) else (b, a)
        if ref[0] != ego and ref[0] not in known_ids and m[0] == "handle_edit_distance":
            # Two accounts that the owner does not know: a near handle alone is
            # too common in a large graph. Keep only one letter on long handles.
            if m[1] > 1 or min(len(a[1]), len(b[1])) < int(c["long_handle_min_length"]):
                continue
        if ref[0] == ego or (ref[0] in known_ids and look[0] not in known_ids):
            whom = "your account" if ref[0] == ego else "an account you know"
            before = look[3] is not None and ref[3] is not None and look[3] < ref[3]
            sev = lookalike_grade(m[0], look[2], ref[2], _age_days(d, look[0]), ref[0] == ego, c, before)
        else:
            sev, whom = "info", "another account in your graph"
        others = sorted(touch.get(look[0], set()) - {look[0], ref[0]})
        if ref[0] == ego and look[0] in known_ids:
            sev = "notable"
        if sev == "info":
            n_info += 1
            if n_info > int(c["max_info_findings"]):
                continue
        title = f"An account name looks like {whom}"
        how = {
            "handle_skeleton": "the handles are the same when separators and look-alike letters are ignored",
            "name_skeleton": "the display names are the same when spaces and look-alike letters are ignored",
            "handle_name_skeleton": "the handle of one is the display name of the other",
            "handle_edit_distance": f"the handles are {m[1]} letter changes apart",
        }[m[0]]
        summary = (
            f"Two accounts look alike: {how}. "
            + (f"{_plural(len(others), 'other account')} interacted with the lookalike. " if others else "")
            + "Lookalike names are used to impersonate an account; they are also used by one person for two accounts."
        )
        ev = {
            "match": m[0], "distance": m[1], "lookalike": look[0], "reference": ref[0],
            "reference_role": "owner" if ref[0] == ego else ("known" if ref[0] in known_ids else "other"),
            "lookalike_created_at": _iso(look[3]) if look[3] else None,
            "reference_created_at": _iso(ref[3]) if ref[3] else None,
            "interacting_accounts": others[:50],
        }
        out.append(Finding("lookalike", title, sev, summary, [look[0], ref[0]], ev, anchor=m[0]))
    return out


# ---------------------------------------------------------------------------
# f. Profile statistical outliers
# ---------------------------------------------------------------------------


def _robust_z(values: dict[str, float]) -> dict[str, float]:
    """Modified z-score 0.6745 * (x - median) / MAD (Iglewicz and Hoaglin 1993).

    If the MAD is 0, the mean absolute deviation times 1.2533 replaces it
    (the same scale for normal data). If that is 0 too, no z-score.
    """
    xs = list(values.values())
    med = statistics.median(xs)
    mad = statistics.median(abs(x - med) for x in xs)
    if mad > 0:
        return {k: 0.6745 * (v - med) / mad for k, v in values.items()}
    mean_ad = statistics.fmean(abs(x - med) for x in xs)
    if mean_ad <= 0:
        return {}
    return {k: (v - med) / (1.2533 * mean_ad) for k, v in values.items()}


def detect_profile_outliers(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """Contacts whose public profile numbers are far from the usual range of the owner's contacts.

    Robust z-scores (median and MAD, so the outliers do not move the center)
    on three log values: follower-to-following ratio (low tail), lifetime
    posts per day (high tail), and account age (low tail). Only the tail that
    is a known signal counts: a very old or very popular account is not
    reported. ``z`` 3.5 is the Iglewicz-Hoaglin limit for an outlier.
    ``min_accounts`` contacts are necessary for a stable median. Severity
    ``info`` only: an outlier is a reason to look, not a signal alone. At most
    ``max_findings`` findings, taken in turn from each feature, the most
    extreme first. Without the turns, the benchmark (tests/validation) showed
    that in a large contact list the new real accounts (account age) filled
    the list, and an account that follows 3000 accounts with no followers did
    not show.
    """
    skip = _excluded(d)
    ratio: dict[str, float] = {}
    rate: dict[str, float] = {}
    age: dict[str, float] = {}
    for uid, p in d.profiles.items():
        if uid in skip:
            continue
        f, g, n = _pf(p, "followers_count"), _pf(p, "following_count"), _pf(p, "tweet_count")
        days = _age_days(d, uid)
        if f is None or g is None or n is None or days is None:
            continue
        days = max(1.0, days)
        ratio[uid] = math.log10((int(f) + 1) / (int(g) + 1))
        rate[uid] = math.log10(int(n) / days + 0.01)
        age[uid] = math.log10(days)
    if len(ratio) < int(c["min_accounts"]):
        return []
    zlim = float(c["z"])
    feats = {
        "follower_ratio": (_robust_z(ratio), -1, "far fewer followers than accounts it follows"),
        "posts_per_day": (_robust_z(rate), 1, "far more posts each day"),
        "account_age": (_robust_z(age), -1, "a much younger account"),
    }
    hits: dict[str, dict[str, float]] = defaultdict(dict)
    per_feature: dict[str, list[tuple[float, str]]] = {}
    for name, (zs, sign, _) in feats.items():
        per_feature[name] = sorted((-abs(z), uid) for uid, z in zs.items() if z * sign >= zlim)
        for uid, z in zs.items():
            if z * sign >= zlim:
                hits[uid][name] = round(z, 2)
    # Round robin over the features, the most extreme first in each. A feature
    # with a long natural tail (new accounts in a large contact list) must not
    # fill the list alone.
    chosen: list[str] = []
    depth = 0
    while len(chosen) < int(c["max_findings"]) and any(depth < len(v) for v in per_feature.values()):
        for name in feats:
            lst = per_feature[name]
            if depth < len(lst) and lst[depth][1] not in chosen and len(chosen) < int(c["max_findings"]):
                chosen.append(lst[depth][1])
        depth += 1
    out: list[Finding] = []
    for uid in chosen:
        zs = hits[uid]
        p = d.profiles[uid]
        words = [feats[n][2] for n in zs]
        title = "Account profile is far from the usual range of your contacts"
        summary = (
            f"Compared with {len(ratio)} of your contacts, this account has " + "; ".join(words) + ". "
            f"Robust z-scores: " + ", ".join(f"{k} {v}" for k, v in sorted(zs.items())) + ". "
            "This is a reason to look at the account, not a signal alone."
        )
        days = _age_days(d, uid) or 0.0
        ev = {
            "z_scores": dict(sorted(zs.items())), "contacts_compared": len(ratio),
            "followers": _pf(p, "followers_count"), "following": _pf(p, "following_count"),
            "posts": _pf(p, "tweet_count"), "age_days": int(days),
        }
        out.append(Finding("profile_outliers", title, "info", summary, [uid], ev, anchor=uid))
    return out


# ---------------------------------------------------------------------------
# g. One-way pressure
# ---------------------------------------------------------------------------


def detect_one_way(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """An account that sent ``min_inbound`` or more replies, quotes, or mentions to the owner and got none back.

    ``min_posts`` different posts are necessary, so one post that is both a
    reply and a mention does not count two times. Persistent contact with no
    answer is a pattern of reply spam and of harassment, and also of fans. The
    owner can read the account. Severity ``info``.
    """
    from_ego: set[str] = {i["target_user_id"] for i in d.interactions
                          if i["source_user_id"] == d.ego_id and i["type"] in INBOUND_TYPES}
    agg: dict[str, dict[str, Any]] = {}
    for i in _inbound(d):
        u = i["source_user_id"]
        g = agg.setdefault(u, {"n": 0, "posts": set(), "types": defaultdict(int), "times": []})
        g["n"] += 1
        g["types"][i["type"]] += 1
        g["posts"].add(i.get("source_post_id") or i["interaction_id"])
        t = _t(_if(i, "interaction_created_at"))
        if t is not None:
            g["times"].append(t)
    out: list[Finding] = []
    for u, g in sorted(agg.items()):
        if u in from_ego or g["n"] < int(c["min_inbound"]) or len(g["posts"]) < int(c["min_posts"]):
            continue
        times = sorted(g["times"])
        title = f"An account contacted you in {_plural(len(g['posts']), 'post')} and got no response"
        summary = (
            f"This account sent {g['n']} interactions ("
            + ", ".join(f"{v} {k}" for k, v in sorted(g["types"].items()))
            + f") in {len(g['posts'])} posts"
            + (f" from {_iso(times[0])} to {_iso(times[-1])}" if times else "")
            + ". You did not reply to, quote, or mention it in the collected data."
        )
        ev = {"interactions": g["n"], "posts": len(g["posts"]), "types": dict(sorted(g["types"].items()))}
        if times:
            ev["first"], ev["last"] = _iso(times[0]), _iso(times[-1])
        out.append(Finding("one_way", title, "info", summary, [u], ev, anchor=u))
    return out


# ---------------------------------------------------------------------------
# h. Tightly linked groups
# ---------------------------------------------------------------------------


def detect_tight_group(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """Groups of ``min_size`` or more accounts (not the owner) where each pair interacts in both directions.

    An edge needs a reply, quote, or mention from A to B and from B to A. In a
    reply thread, X adds each participant to the mention list, so one-way
    mention triangles are usual in any group conversation. Mutual edges are a
    stronger tie. The groups are the maximal cliques (igraph). Severity
    ``info``: a tight group is often a group of friends. At most
    ``max_findings``. The groups with the smallest share of accounts that the
    owner replied to, quoted, or mentioned come first, then the largest. The
    owner knows a group of colleagues; a group that talks to the owner and to
    each other but never gets an answer is the less usual case. Without this
    order, the benchmark (tests/validation) showed a planted group of 3 that
    fell below the limit behind groups of colleagues.
    """
    import igraph as ig

    skip = {d.ego_id}
    directed: set[tuple[str, str]] = set()
    for i in d.interactions:
        s, t = i["source_user_id"], i["target_user_id"]
        if s in skip or t in skip or s == t or i["type"] not in INBOUND_TYPES:
            continue
        directed.add((s, t))
    mutual = sorted({tuple(sorted((s, t))) for s, t in directed if (t, s) in directed})
    if not mutual:
        return []
    nodes = sorted({u for e in mutual for u in e})
    idx = {u: n for n, u in enumerate(nodes)}
    g = ig.Graph(n=len(nodes), edges=[(idx[a], idx[b]) for a, b in mutual])
    core = g.coreness()
    top = max(core)
    if top > int(c["max_core"]):
        dense = sorted(nodes[v] for v, k in enumerate(core) if k == top)
        title = f"{len(dense)} accounts interact with each other in a dense group"
        summary = (f"Each of these {len(dense)} accounts replied to or mentioned at least {top} of the others in the "
                   "two directions. The group is too dense to list each tight group in it.")
        ev = {"size": len(dense), "core_number": top}
        return [Finding("tight_group", title, "info", summary, dense, ev, anchor="core")]
    cliques = [sorted(nodes[v] for v in cl) for cl in g.maximal_cliques(min=int(c["min_size"]))]
    owner_to = {i["target_user_id"] for i in d.interactions
                if i["source_user_id"] == d.ego_id and i["type"] in INBOUND_TYPES}

    def known_share(cl: list[str]) -> float:
        return sum(1 for u in cl if u in owner_to) / len(cl)

    cliques.sort(key=lambda cl: (known_share(cl), -len(cl), cl))
    out: list[Finding] = []
    for cl in cliques[: int(c["max_findings"])]:
        title = f"{len(cl)} accounts interact with each other in a tight group"
        summary = (
            f"Each pair of these {len(cl)} accounts replied to or mentioned each other in the two directions. "
            "This is often a group of friends or colleagues; it is also how a group of accounts "
            "can make each other look active."
        )
        ev = {"size": len(cl), "mutual_pairs": len(cl) * (len(cl) - 1) // 2,
              "owner_answered_share": round(known_share(cl), 3)}
        out.append(Finding("tight_group", title, "info", summary, cl, ev, anchor="clique"))
    return out


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _member_fields(f: Finding) -> tuple[str, dict[str, Any]] | None:
    ev = f.evidence
    if f.detector == "reply_burst":
        return "reply_burst", {"accounts": ev["accounts"], "minutes": max(1, math.ceil(ev["span_seconds"] / 60))}
    if f.detector == "creation_cluster":
        hours = max(1, math.ceil((_t(ev["last_created"]) - _t(ev["first_created"])).total_seconds() / 3600))  # type: ignore[operator]
        return "creation_cluster", {"accounts": ev["accounts"], "hours": hours}
    if f.detector == "fast_reply":
        return "fast_reply", {"seconds": int(round(ev["median_seconds"])), "replies": ev["replies"]}
    if f.detector == "shared_content":
        if ev["kind"] in ("text", "link"):
            return "shared_text_or_link", {"kind": ev["kind"], "others": ev["accounts"] - 1}
        return "shared_domain", {"domain": ev["domain"], "others": ev["accounts"] - 1}
    if f.detector == "co_action":
        return "co_action", {"accounts": ev["accounts"], "posts": len(ev["posts"]), "minutes": ev["window_minutes"]}
    if f.detector == "mass_tagging":
        return "mass_tagging", {"tagged": ev["tagged"], "posts": ev["posts"]}
    if f.detector == "profile_change" and ev.get("kind") == "toward_known" and f.severity == "high":
        return "profile_change", {"known": ev["reference_handle"], "changed": " and ".join(ev["changed"])}
    return None


def run_patterns(d: PatternData, conf: PatternConfig) -> PatternResult:
    """Run each enabled detector. Returns the findings, the rule input for members, and the known accounts."""
    from . import network as N

    known = known_accounts(d, conf.top_contacts)
    res = PatternResult(known_accounts=known, ego_posts=d.ego_posts)
    dets = conf.detectors
    runs: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("reply_burst", lambda: detect_reply_burst(d, dets["reply_burst"])),
        ("creation_cluster", lambda: detect_creation_cluster(d, dets["creation_cluster"])),
        ("fast_reply", lambda: detect_fast_reply(d, dets["fast_reply"])),
        ("shared_content", lambda: detect_shared_content(d, dets["shared_content"])),
        ("lookalike", lambda: detect_lookalike(d, dets["lookalike"], known)),
        ("profile_outliers", lambda: detect_profile_outliers(d, dets["profile_outliers"])),
        ("one_way", lambda: detect_one_way(d, dets["one_way"])),
        ("tight_group", lambda: detect_tight_group(d, dets["tight_group"])),
        ("co_action", lambda: N.detect_co_action(d, dets["co_action"])),
        ("profile_change", lambda: N.detect_profile_change(d, dets["profile_change"], dets["lookalike"], known)),
        ("mass_tagging", lambda: N.detect_mass_tagging(d, dets["mass_tagging"])),
        ("community_anomaly", lambda: N.detect_community_anomaly(d, dets["community_anomaly"])),
    ]
    seen: set[str] = set()
    for name, fn in runs:
        if not dets[name]["enabled"]:
            continue
        for f in fn():
            if f.severity not in SEVERITIES:
                raise RuntimeError(f"defect: finding severity {f.severity!r}")
            if f.finding_id in seen:
                continue
            seen.add(f.finding_id)
            res.findings.append(f)
    min_rank = _rank(conf.member_rule_min_severity)
    for f in res.findings:
        if f.detector not in RULE_DETECTORS or _rank(f.severity) < min_rank:
            continue
        mf = _member_fields(f)
        if mf is None:
            continue
        key, fields = mf
        table = res.members.setdefault(key, {})
        for u in (f.members if f.rule_members is None else f.rule_members):
            old = table.get(u)
            # Keep the largest group for the cause.
            if old is None or fields.get("accounts", fields.get("others", 0)) > old.get("accounts", old.get("others", 0)):
                table[u] = dict(fields)
    return res


def finding_row(f: Finding, stage_run_id: str) -> tuple[str, str, str, str, str, str, str, str]:
    return (f.finding_id, f.detector, f.title, f.severity, f.summary, dumps(f.members), dumps(f.evidence), stage_run_id)
