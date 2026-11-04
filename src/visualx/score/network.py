"""Network-level detectors that need the graph or the history (score stage, table ``findings``).

``patterns.py`` holds the first eight detectors and the shared helpers. This
module adds four detectors (owner request 2026-11-03):

- ``co_action``: pairs of accounts that act on the same owner posts within
  minutes of each other, more often than their own timing explains
  (coordination network, after Pacheco et al. 2021).
- ``profile_change``: an account that changed its handle or display name to
  look like the owner or a known account, or that changed its handle many
  times (account takeover and resale).
- ``mass_tagging``: an account that tags many accounts in post after post,
  a new set each time, and the tagged accounts have no link to each other
  (the "star" egonet of OddBall, Akoglu et al. 2010, made to work on
  ego-sampled data; see the docstring).
- ``community_anomaly``: a Louvain community (owner removed) whose members
  were made in the same few weeks, or whose profile numbers are far from the
  other accounts.

The same policy as ``patterns.py`` applies: titles and summaries hold no
handle and no text from X (FR-31, AD-10), no detector reads a sensitive
input, the location, the bio, or a language (TR-06), and a missing field is
not a value (DR-02).
"""

from __future__ import annotations

import bisect
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from .patterns import (
    NETWORK_DEFAULTS,  # noqa: F401  (the defaults live in patterns.py, next to the others)
    Finding,
    PatternData,
    _age_days,
    _excluded,
    _if,
    _iso,
    _pf,
    _plural,
    _poisson_tail,
    _t,
)
from .skeleton import lookalike_grade, lookalike_match, names_alike

ACT_TYPES: tuple[str, ...] = ("reply", "quote")
TAG_TYPES: tuple[str, ...] = ("reply", "quote", "mention")

# Detectors of this module that give a per-account rule (B13, B14, I04).
RULE_DETECTORS: frozenset[str] = frozenset({"co_action", "mass_tagging", "profile_change"})


def seed_accounts(d: PatternData) -> set[str]:
    """Accounts whose own posts the collector read (step 6 seed posts): the owner's timeline endpoint, another author."""
    return {i["source_user_id"] for i in d.interactions
            if i.get("source_endpoint") == "/2/users/:id/tweets" and i["source_user_id"] != d.ego_id}


# ---------------------------------------------------------------------------
# i. Co-action
# ---------------------------------------------------------------------------


def detect_co_action(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """Pairs of accounts that reply to or quote the same owner posts within ``window_minutes``, again and again.

    The question is not "did two accounts reply to the same post" (fans and
    colleagues do), but "do they reply at the same minutes more often than
    chance". For each reply or quote, the latency is the time after the owner
    post. Under the null, the latency of each account on a post comes from the
    latency distribution of all replies to owner posts of a similar reach (two
    bands, split at the median impression count). For a pair (a, b) on a post
    where both acted, ``q`` is the chance that a latency from that
    distribution falls within the window of the latency of a (or of b; the
    larger value). A fast thread or a viral post has dense latencies, so
    ``q`` is large there and two fast replies are no surprise.

    The expected count of synchronized posts for the pair is the sum of ``q``
    over all posts that both acted on. The p-value is the Poisson tail of the
    observed count, with a Bonferroni correction for all pairs that share
    ``min_sync`` or more posts. ``min_q`` keeps a value in the tail of the
    distribution from giving too much weight to one post.

    Significant pairs make a graph; each connected component is one finding.
    Severity ``notable``: members get rule B13. Reposts have no time and do
    not count. Owner posts with more than ``max_actors`` actors are left out
    (the pair count grows with the square).
    """
    skip = _excluded(d)
    w = float(c["window_minutes"]) * 60.0
    post_t: dict[str, datetime] = {}
    for pid, p in d.ego_posts.items():
        t = _t(p.get("created_at"))
        if t is not None:
            post_t[pid] = t
    acts: dict[str, dict[str, float]] = defaultdict(dict)  # post -> user -> latency (s)
    for i in d.interactions:
        if i["type"] not in ACT_TYPES or i["source_user_id"] in skip:
            continue
        pid = _if(i, "target_post_id")
        t = _t(_if(i, "interaction_created_at"))
        if pid not in post_t or t is None:
            continue
        lat = (t - post_t[pid]).total_seconds()
        if lat < 0:
            continue
        u = i["source_user_id"]
        old = acts[pid].get(u)
        if old is None or lat < old:
            acts[pid][u] = lat
    if not acts:
        return []
    imps = sorted(int(d.ego_posts[p].get("impressions") or 0) for p in acts)
    split = imps[len(imps) // 2]
    band_lats: dict[int, list[float]] = {0: [], 1: []}
    band_of: dict[str, int] = {}
    for pid, us in acts.items():
        b = 1 if int(d.ego_posts[pid].get("impressions") or 0) > split else 0
        band_of[pid] = b
        band_lats[b].extend(us.values())
    if min(len(band_lats[0]), len(band_lats[1])) < 50:
        pooled = sorted(band_lats[0] + band_lats[1])
        cdf = {0: pooled, 1: pooled}
    else:
        cdf = {0: sorted(band_lats[0]), 1: sorted(band_lats[1])}
    min_q = float(c["min_q"])

    def q_at(b: int, lat: float) -> float:
        xs = cdf[b]
        n = len(xs)
        if n <= 2:
            return 1.0
        k = bisect.bisect_right(xs, lat + w) - bisect.bisect_left(xs, lat - w) - 1  # not the reply itself
        return max(min_q, k / (n - 2))

    shared: dict[tuple[str, str], int] = defaultdict(int)
    mu: dict[tuple[str, str], float] = defaultdict(float)
    sync: dict[tuple[str, str], list[str]] = defaultdict(list)
    for pid, us in acts.items():
        if len(us) < 2 or len(us) > int(c["max_actors"]):
            continue
        b = band_of[pid]
        items = sorted(us.items())
        for x in range(len(items)):
            ua, la = items[x]
            qa = q_at(b, la)
            for y in range(x + 1, len(items)):
                ub, lb = items[y]
                key = (ua, ub)
                shared[key] += 1
                mu[key] += min(1.0, max(qa, q_at(b, lb)))
                if abs(la - lb) <= w:
                    sync[key].append(pid)
    min_sync = int(c["min_sync"])
    tested = sum(1 for k, n in shared.items() if n >= min_sync)
    if tested == 0:
        return []
    sig: dict[tuple[str, str], dict[str, Any]] = {}
    for key, pids in sync.items():
        k = len(pids)
        if k < min_sync:
            continue
        p = _poisson_tail(k, mu[key])
        if p * tested <= float(c["max_p"]):
            sig[key] = {"sync": k, "shared": shared[key], "expected": mu[key], "p": p, "posts": sorted(pids)}
    if not sig:
        return []
    # Connected components of the significant pairs.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sig:
        parent[find(a)] = find(b)
    comps: dict[str, list[str]] = defaultdict(list)
    for u in {u for k in sig for u in k}:
        comps[find(u)].append(u)
    groups = sorted((sorted(m) for m in comps.values()), key=lambda m: (-len(m), m))
    out: list[Finding] = []
    for members in groups[: int(c["max_findings"])]:
        ms = set(members)
        pairs = sorted((k, v) for k, v in sig.items() if k[0] in ms)
        posts = sorted({p for _, v in pairs for p in v["posts"]})
        best = min(v["p"] for _, v in pairs)
        n_sync = max(v["sync"] for _, v in pairs)
        title = (f"{len(members)} accounts acted on the same posts of yours within minutes of each other, "
                 f"on {_plural(len(posts), 'post')}")
        summary = (
            f"In {_plural(len(pairs), 'pair')} of these accounts, the two accounts replied to or quoted the same post "
            f"within {int(c['window_minutes'])} minutes of each other on up to {n_sync} posts. "
            f"Their reply times on those posts make this unlikely by chance "
            f"(smallest p-value {best:.1e}, with a correction for {tested} pairs). "
            "Accounts that a person or a program runs together act at the same minutes; "
            "friends in a live thread also can."
        )
        ev = {
            "accounts": len(members), "posts": posts[:100], "window_minutes": int(c["window_minutes"]),
            "pairs": [{"a": k[0], "b": k[1], "sync_posts": v["sync"], "shared_posts": v["shared"],
                       "expected": round(v["expected"], 3), "p": float(f"{v['p']:.3g}")} for k, v in pairs[:50]],
            "pairs_tested": tested,
        }
        out.append(Finding("co_action", title, "notable", summary, members, ev, anchor="co_action"))
    return out


# ---------------------------------------------------------------------------
# j. Profile changes
# ---------------------------------------------------------------------------


def detect_profile_change(d: PatternData, c: dict[str, Any], lk: dict[str, Any],
                          known: tuple[dict[str, Any], ...]) -> list[Finding]:
    """Handle or display-name changes in ``profile_history``.

    - A change that makes the account look like the owner or a known account
      (``skeleton.lookalike_match`` on the new values, no match on the old
      values) is ``high`` if ``lookalike_grade`` gives ``high`` with the
      account taken as young (a renamed account did not grow into the name),
      else ``notable``. The ``lookalike`` detector grades an old account
      down, because an old account with a near name is often a second person.
      A rename is the case where the age does not help: an old account that
      is taken over or bought keeps its creation date. Members get rule I04.
    - ``churn_min`` or more different handles: ``notable``. Handle churn is a
      sign of account resale.
    - One handle change: ``info``. People change handles. A change of the
      display name alone is not a finding: it is too common (titles, events).

    The history starts with the first X API fetch of each account, so the
    detector sees only changes between collections.
    """
    hist = d.profile_history
    refs: list[dict[str, Any]] = [dict(k) for k in known]
    if d.ego_id and d.ego_handle and not any(k["user_id"] == d.ego_id for k in refs):
        p = d.profiles.get(d.ego_id)
        refs.append({"user_id": d.ego_id, "handle": d.ego_handle.lstrip("@"), "name": _pf(p, "name"), "role": "owner"})
    out: list[Finding] = []
    for uid, rows in sorted(hist.items()):
        if uid == d.ego_id or uid in d.allowlist or len(rows) < 2:
            continue
        handles = [str(r["handle"]).lstrip("@") for r in rows if r.get("handle")]
        distinct = list(dict.fromkeys(h.casefold() for h in handles))
        toward: tuple[dict[str, Any], str, str, dict[str, Any], dict[str, Any]] | None = None
        for old, new in zip(rows, rows[1:]):
            if (old.get("handle"), old.get("name")) == (new.get("handle"), new.get("name")):
                continue
            for k in refs:
                if k["user_id"] == uid or not k.get("handle"):
                    continue
                nh = str(new.get("handle") or "").lstrip("@")
                oh = str(old.get("handle") or "").lstrip("@")
                if not nh:
                    continue
                m_new = lookalike_match(nh, new.get("name"), str(k["handle"]), k.get("name"), lk)
                if m_new is None:
                    continue
                if oh and lookalike_match(oh, old.get("name"), str(k["handle"]), k.get("name"), lk) is not None:
                    continue
                is_owner = k["user_id"] == d.ego_id
                sev = lookalike_grade(m_new[0], new.get("name"), k.get("name"), 0.0, is_owner, lk, False)
                if sev != "high" and names_alike(new.get("name"), k.get("name"), lk) \
                        and not names_alike(old.get("name"), k.get("name"), lk):
                    sev = "high"  # the name changed to the name of the reference too
                sev = "high" if sev == "high" else "notable"
                if toward is None or (sev == "high" and toward[1] != "high"):
                    toward = (k, sev, m_new[0], old, new)
        if toward is not None:
            k, sev, match, old, new = toward
            whom = "your account" if k["user_id"] == d.ego_id else "an account you know"
            fields = [f for f in ("handle", "name") if old.get(f) != new.get(f)]
            title = f"An account changed its {' and '.join(fields)} to look like {whom}"
            summary = (
                f"Between {old['fetched_at'][:10]} and {new['fetched_at'][:10]} this account changed its "
                f"{' and '.join(fields)}. Before the change it did not look like {whom}; after the change it does. "
                "An account that is taken over or bought keeps its creation date, so its age does not show the change."
            )
            age = _age_days(d, uid)
            ev = {
                "kind": "toward_known", "match": match, "reference": k["user_id"],
                "reference_role": "owner" if k["user_id"] == d.ego_id else str(k.get("role", "known")),
                "reference_handle": str(k["handle"]).lstrip("@"),
                "changed": fields, "before_at": old["fetched_at"], "after_at": new["fetched_at"],
                "changes": len(rows) - 1, "age_days": None if age is None else int(age),
            }
            out.append(Finding("profile_change", title, sev, summary, [uid, k["user_id"]], ev,
                               anchor="toward", rule_members=[uid]))
            continue
        if len(distinct) >= int(c["churn_min"]):
            title = f"An account changed its handle {len(distinct) - 1} times"
            summary = (
                f"This account had {len(distinct)} different handles between {rows[0]['fetched_at'][:10]} "
                f"and {rows[-1]['fetched_at'][:10]}. Accounts that are sold or reused change their handle often."
            )
            ev = {"kind": "churn", "handles": len(distinct), "first_at": rows[0]["fetched_at"],
                  "last_at": rows[-1]["fetched_at"]}
            out.append(Finding("profile_change", title, "notable", summary, [uid], ev, anchor="churn"))
        elif len(distinct) == 2:
            at = next(r["fetched_at"] for r in rows if str(r.get("handle") or "").lstrip("@").casefold() == distinct[1])
            title = "An account changed its handle"
            summary = f"This account changed its handle by {at[:10]}. People change handles; this is a reason to look."
            ev = {"kind": "handle", "handles": 2, "at": at}
            out.append(Finding("profile_change", title, "info", summary, [uid], ev, anchor="handle"))
    return out


# ---------------------------------------------------------------------------
# k. Mass tagging (star egonet)
# ---------------------------------------------------------------------------


def detect_mass_tagging(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """An account that tags many accounts that have no link to each other, with a new set in each post.

    OddBall (Akoglu et al. 2010) finds a "near star": an account whose
    neighbors have almost no links between them. Its power-law test needs the
    full neighborhood of each account. Here the collector sees the posts of
    other accounts only when they touch the owner (or come from a seed), so
    almost every egonet looks like a star, and the regression finds nothing.
    The test that works on this data uses what each post shows: its list of
    tagged accounts (reply target, quote target, and mentions).

    For an account (not the owner, not a seed, not on the allowlist), the
    "fan-out posts" are its posts that tag ``min_fanout`` or more accounts
    other than the owner. With ``min_posts`` or more fan-out posts:

    - tagged: the different accounts in those posts (owner not counted);
    - retag share: 1 - tagged / tag slots. A person who tags a group tags the
      same group again (a high share); a tagging campaign tags new targets;
    - linked share: the part of the tagged accounts that have an interaction
      with another tagged account (not through this account or the owner).
      The friends of a person know each other; the targets of a campaign
      come from all over the network.

    A finding needs ``min_tagged`` accounts, a retag share at or below
    ``max_retag_share``, and a linked share at or below ``max_linked_share``.
    Severity ``notable``: members get rule B14.
    """
    skip = _excluded(d)
    seeds = seed_accounts(d)
    ego = d.ego_id
    posts: dict[tuple[str, str], set[str]] = defaultdict(set)
    adj: dict[str, set[str]] = defaultdict(set)
    for i in d.interactions:
        s, t = i["source_user_id"], i["target_user_id"]
        if s != t and ego not in (s, t):
            adj[s].add(t)
            adj[t].add(s)
        pid = i.get("source_post_id")
        if i["type"] in TAG_TYPES and pid and s != ego and t != ego and t != s:
            posts[(s, pid)].add(t)
    by_user: dict[str, list[set[str]]] = defaultdict(list)
    for (s, _pid), ts in posts.items():
        if len(ts) >= int(c["min_fanout"]):
            by_user[s].append(ts)
    out: list[Finding] = []
    for u, sets in sorted(by_user.items()):
        if u in skip or u in seeds or len(sets) < int(c["min_posts"]):
            continue
        tagged = set().union(*sets)
        slots = sum(len(x) for x in sets)
        if len(tagged) < int(c["min_tagged"]):
            continue
        retag = 1.0 - len(tagged) / slots
        linked = sum(1 for v in tagged if (adj.get(v, set()) - {u}) & tagged)
        lshare = linked / len(tagged)
        pairs = sum(len((adj.get(v, set()) - {u}) & tagged) for v in tagged) // 2
        density = pairs / (len(tagged) * (len(tagged) - 1) / 2)
        if retag > float(c["max_retag_share"]) or lshare > float(c["max_linked_share"]):
            continue
        title = f"An account tagged {len(tagged)} accounts that have almost no link to each other"
        summary = (
            f"This account tagged {len(tagged)} different accounts in {len(sets)} posts "
            f"({slots} tags, {round(100 * retag)} % of them repeated). "
            f"{round(100 * lshare)} % of the tagged accounts have a link to another tagged account. "
            "People tag friends who know each other; mass tagging reaches accounts from all over the network."
        )
        ev = {"tagged": len(tagged), "posts": len(sets), "tag_slots": slots, "retag_share": round(retag, 3),
              "linked_share": round(lshare, 3), "density": round(density, 4), "tagged_accounts": sorted(tagged)[:100]}
        out.append(Finding("mass_tagging", title, "notable", summary, [u], ev, anchor=u))
    return out


# ---------------------------------------------------------------------------
# l. Community anomalies
# ---------------------------------------------------------------------------


def _binom_tail(k: int, n: int, p: float) -> float:
    if k <= 0:
        return 1.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    total = 0.0
    for j in range(k, n + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
                          + j * math.log(p) + (n - j) * math.log1p(-p))
    return min(1.0, total)


def _rank_z(sample: list[float], ref: list[float]) -> float:
    """Mann-Whitney U as a z-value (normal approximation, ties counted half). Negative: the sample is lower."""
    n1, n2 = len(sample), len(ref)
    if n1 == 0 or n2 == 0:
        return 0.0
    srt = sorted(ref)
    u = 0.0
    for x in sample:
        lo = bisect.bisect_left(srt, x)
        hi = bisect.bisect_right(srt, x)
        u += lo + 0.5 * (hi - lo)
    mean = n1 * n2 / 2.0
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    return 0.0 if sd == 0 else (u - mean) / sd


def _norm_tail(z: float) -> float:
    """P(Z <= z) for a standard normal."""
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def communities(d: PatternData, seed: int) -> list[list[str]]:
    """Louvain communities of the interaction graph without the owner (AD-07), with a fixed random seed."""
    import igraph as ig

    ego = d.ego_id
    w: dict[tuple[str, str], float] = defaultdict(float)
    for i in d.interactions:
        s, t = i["source_user_id"], i["target_user_id"]
        if s == t or s == ego or t == ego:
            continue
        w[(s, t) if s < t else (t, s)] += 1.0
    if not w:
        return []
    nodes = sorted({u for e in w for u in e})
    idx = {u: n for n, u in enumerate(nodes)}
    keys = sorted(w)
    g = ig.Graph(n=len(nodes), edges=[(idx[a], idx[b]) for a, b in keys])
    g.es["weight"] = [w[k] for k in keys]
    ig.set_random_number_generator(random.Random(seed))
    try:
        cl = g.community_multilevel(weights="weight")
    finally:
        ig.set_random_number_generator(random)
    return sorted((sorted(nodes[v] for v in c) for c in cl), key=lambda m: (-len(m), m))


def detect_community_anomaly(d: PatternData, c: dict[str, Any]) -> list[Finding]:
    """Communities whose members were made in the same few weeks, or whose profiles are far from the rest.

    The communities are Louvain groups of the graph without the owner. For a
    community of ``min_size`` or more accounts with a creation date:

    - Creation concentration: the largest number ``k`` of members made in one
      window of ``window_days``. The chance for one account to fall in that
      window is the share of the other accounts made in it, or the local
      rate in ``local_days`` around it, the larger value (the number of new
      accounts changes with time). The p-value is the binomial tail, with a
      Bonferroni correction for each window start in each community. A
      significant concentration of ``min_in_window`` or more is ``notable``.
      The members of the finding are the members in the window.
      ``creation_cluster`` looks for 3 accounts in 72 hours in all contacts;
      a farm that was made over some weeks is not dense there, but it is
      dense in its own community.
    - Profile shift: one-sided rank tests (Mann-Whitney) against the other
      accounts for younger accounts and for fewer followers per followed
      account, and a binomial test for more default profile images. Two or
      more significant tests (Bonferroni for all tests) without a
      concentration give an ``info`` finding for the community.

    No per-account rule: these are profile numbers, and profile rules alone
    must not give ``many_signals`` (FR-35).
    """
    comms = [m for m in communities(d, int(c["random_seed"])) if len(m) >= int(c["min_size"])]
    if not comms:
        return []
    skip = _excluded(d)
    created: dict[str, datetime] = {}
    ratio: dict[str, float] = {}
    age: dict[str, float] = {}
    default_img: dict[str, bool] = {}
    for uid, p in d.profiles.items():
        if uid in skip:
            continue
        t = _t(_pf(p, "created_at"))
        if t is not None:
            created[uid] = t
            a = _age_days(d, uid)
            if a is not None:
                age[uid] = a
        f, g = _pf(p, "followers_count"), _pf(p, "following_count")
        if f is not None and g is not None:
            ratio[uid] = math.log10((int(f) + 1) / (int(g) + 1))
        img = _pf(p, "profile_image_url")
        if img is not None:
            default_img[uid] = "default_profile" in str(img)
    all_times = sorted(created.values())
    win = timedelta(days=float(c["window_days"]))
    local = timedelta(days=float(c["local_days"]))
    n_tests = sum(sum(1 for u in m if u in created) for m in comms)
    n_shift = 3 * len(comms)
    out: list[Finding] = []
    for m in comms:
        ms = set(m)
        times = sorted((created[u], u) for u in m if u in created)
        n = len(times)
        if n < int(c["min_size"]):
            continue
        others_n = len(all_times) - n
        best: tuple[float, int, int, int] | None = None  # p, k, i, j
        for i in range(n):
            j = bisect.bisect_right([t for t, _ in times], times[i][0] + win)
            k = j - i
            if k < int(c["min_in_window"]):
                continue
            lo, hi = times[i][0], times[i][0] + win
            in_all = bisect.bisect_right(all_times, hi) - bisect.bisect_left(all_times, lo)
            share = max(0, in_all - k) / max(1, others_n)
            near = (bisect.bisect_right(all_times, lo + local) - bisect.bisect_left(all_times, lo - local)) - k
            share = max(share, max(0, near) / max(1, others_n) * (win / (2 * local)), 1.0 / max(1, others_n))
            p = _binom_tail(k, n, min(1.0, share))
            if best is None or p < best[0]:
                best = (p, k, i, j)
        shifts: dict[str, float] = {}
        ref_age = [v for u, v in age.items() if u not in ms]
        z = _rank_z([age[u] for u in m if u in age], ref_age)
        shifts["younger"] = _norm_tail(z)
        ref_ratio = [v for u, v in ratio.items() if u not in ms]
        z = _rank_z([ratio[u] for u in m if u in ratio], ref_ratio)
        shifts["fewer_followers"] = _norm_tail(z)
        imgs = [default_img[u] for u in m if u in default_img]
        ref_imgs = [v for u, v in default_img.items() if u not in ms]
        base = (sum(ref_imgs) + 1) / (len(ref_imgs) + 2) if ref_imgs else 0.0
        shifts["default_image"] = _binom_tail(sum(imgs), len(imgs), base) if imgs and base > 0 else 1.0
        sig_shifts = {k: v for k, v in shifts.items() if v * n_shift <= float(c["max_p"])}
        lim = float(c["max_p"])
        if best is not None and best[0] * n_tests <= lim:
            p, k, i, j = best
            members = [u for _, u in times[i:j]]
            title = (f"{k} of {n} accounts in one community of your network were made within "
                     f"{int(c['window_days'])} days of each other")
            summary = (
                f"A community of {len(m)} accounts that interact with each other has {k} accounts made between "
                f"{times[i][0].date()} and {times[j - 1][0].date()}. In the rest of your network this is rare "
                f"(p-value {p:.1e}, with a correction for {n_tests} windows). "
                "Accounts made in one batch and linked to each other are a sign of a group of accounts that one "
                "operator runs; a class or a new team can also join at the same time."
                + (" The community is also " + ", ".join(k2.replace("_", " ") for k2 in sorted(sig_shifts)) + "."
                   if sig_shifts else "")
            )
            ev = {"kind": "creation", "community_size": len(m), "accounts": k,
                  "first_created": _iso(times[i][0]), "last_created": _iso(times[j - 1][0]),
                  "p": float(f"{p:.3g}"), "windows_tested": n_tests,
                  "shifts": {k2: float(f"{v:.3g}") for k2, v in sorted(shifts.items())},
                  "community": m[:200]}
            out.append(Finding("community_anomaly", title, "notable", summary, members, ev, anchor="creation"))
        elif len(sig_shifts) >= 2:
            title = f"A community of {len(m)} accounts has profiles far from the rest of your network"
            summary = (
                f"Compared with the other accounts, this community has: "
                + ", ".join(k2.replace("_", " ") for k2 in sorted(sig_shifts))
                + f" (corrected for {n_shift} tests). This is a reason to look, not a signal alone."
            )
            ev = {"kind": "shift", "community_size": len(m),
                  "shifts": {k2: float(f"{v:.3g}") for k2, v in sorted(shifts.items())}, "community": m[:200]}
            out.append(Finding("community_anomaly", title, "info", summary, m[:200], ev, anchor="shift"))
    out.sort(key=lambda f: (f.severity != "notable", -len(f.members)))
    return out[: int(c["max_findings"])]
