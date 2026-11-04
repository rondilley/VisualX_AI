"""Communities, stability, hubs, bridges, and bio topics (FR-26 to FR-28, FR-30).

The analyze stage calls ``run_groups`` after the metrics. All work is on the
undirected graph with the owner removed: with the owner in the graph, one
community holds almost every account (AD-07).

* **Leiden** (leidenalg, GPL-3.0) with the RB configuration model, one run for
  each random seed (20 or more, TSC-11). The run with the highest quality is the
  reference partition.
* **Stability** of a node: the mean Jaccard similarity between its reference
  community and its community in each other run. 1.0 means that the node was
  always in the same group. The page labels communities as exploratory and shows
  this value (FR-27).
* **Hubs:** the accounts with the most links to other accounts (owner removed).
  **Bridges:** high undirected betweenness with the owner removed, or links into two or more
  other communities (FR-30).
* **Topics** come from the profile description with keyword rules from
  ``config/topics.yaml``, then from label propagation over the links (FR-28).
  A topic is a display label only. No rule or detector reads it, and a topic for a
  sensitive category is refused (TR-06).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

import igraph as ig
import leidenalg

# TR-06: no topic for politics, religion, health, ethnicity, sexuality, or trade-union
# membership. A topic name or a keyword that matches one of these words is refused.
SENSITIVE_WORDS = re.compile(
    r"\b(politic\w*|election\w*|democrat\w*|republican\w*|conservative\w*|liberal\w*|leftist\w*|"
    r"maga|socialis\w*|communis\w*|religio\w*|christian\w*|catholic\w*|muslim\w*|islam\w*|jew|jews|jewish|"
    r"hindu\w*|buddhis\w*|atheis\w*|church\w*|faith\w*|god|bible|health\w*|medical|disease\w*|"
    r"illness\w*|disab\w*|mental|ethnic\w*|racial\w*|racis\w*|sexual\w*|gay|lesbian\w*|lgbt\w*|queer|"
    r"trans|pride|union\w*)\b",
    re.IGNORECASE,
)

GROUP_DEFAULTS: dict[str, Any] = {
    "resolution": 1.0,
    "runs": 20,
    "random_seed": 42,
    "min_community_size": 3,
    "hub_top_share": 0.02,
    "hub_min_degree": 5,
    "bridge_top_share": 0.02,
    "bridge_min_communities": 2,
    "bridge_min_links": 2,
    "topic_min_share": 0.5,
    "topic_min_neighbors": 2,
    "topic_rounds": 5,
}


class TopicError(ValueError):
    """config/topics.yaml has a defect, or names a sensitive category (TR-06)."""


def check_topics(topics: Any) -> dict[str, list[re.Pattern[str]]]:
    """Return {topic: [compiled patterns]}. Each keyword matches as a whole word, case-insensitive.

    A keyword that starts with ``re:`` is a regular expression. Raises TopicError.
    """
    if topics in (None, {}, []):
        return {}
    if not isinstance(topics, dict):
        raise TopicError("topics.yaml must map each topic name to a list of keywords")
    out: dict[str, list[re.Pattern[str]]] = {}
    for name, words in topics.items():
        name = str(name)
        if not re.fullmatch(r"[a-z0-9_]{1,30}", name):
            raise TopicError(f"topic name {name!r} must be 1 to 30 characters of a-z, 0-9, and _")
        if SENSITIVE_WORDS.search(name.replace("_", " ")):
            raise TopicError(f"topic {name!r} names a sensitive category (TR-06)")
        if not isinstance(words, list) or not words:
            raise TopicError(f"topic {name!r} must have a list of keywords")
        pats = []
        for w in words:
            w = str(w)
            if w.startswith("re:"):
                body = w[3:]
                if SENSITIVE_WORDS.search(body):
                    raise TopicError(f"topic {name!r}: the pattern {body!r} names a sensitive category (TR-06)")
                try:
                    pats.append(re.compile(body, re.IGNORECASE))
                except re.error as e:
                    raise TopicError(f"topic {name!r}: bad pattern {body!r}: {e}") from e
            else:
                if SENSITIVE_WORDS.search(w):
                    raise TopicError(f"topic {name!r}: the keyword {w!r} names a sensitive category (TR-06)")
                pats.append(re.compile(r"(?<![\w#@])" + re.escape(w) + r"(?!\w)", re.IGNORECASE))
        out[name] = pats
    return out


def undirected_alters(nodes: list[str], edges: list[dict[str, Any]], ego: str) -> tuple[ig.Graph, list[str]]:
    """Undirected graph without the owner. A pair weight is the larger of its two directions."""
    alters = [n for n in nodes if n != ego]
    idx = {u: i for i, u in enumerate(alters)}
    pair: dict[tuple[int, int], float] = {}
    for e in edges:
        s, t = e["source"], e["target"]
        if s == ego or t == ego or s == t:
            continue
        a, b = sorted((idx[s], idx[t]))
        pair[(a, b)] = max(pair.get((a, b), 0.0), float(e["pair_weight"]))
    keys = sorted(pair)
    g = ig.Graph(n=len(alters), edges=keys, directed=False)
    g.es["weight"] = [pair[k] for k in keys]
    return g, alters


def leiden_runs(g: ig.Graph, resolution: float, runs: int, seed: int) -> list[tuple[list[int], float]]:
    """One Leiden partition for each random seed: (membership, quality)."""
    out = []
    w = "weight" if g.ecount() else None
    for k in range(runs):
        part = leidenalg.find_partition(
            g, leidenalg.RBConfigurationVertexPartition, weights=w,
            resolution_parameter=resolution, seed=seed + k, n_iterations=-1,
        )
        out.append((list(part.membership), float(part.quality())))
    return out


def stability(ref: list[int], others: list[list[int]]) -> list[float]:
    """Mean Jaccard similarity of each node's reference community with its community in each run."""
    n = len(ref)
    if not others:
        return [1.0] * n
    ref_size = Counter(ref)
    total = [0.0] * n
    for memb in others:
        size = Counter(memb)
        both = Counter(zip(ref, memb))
        for i in range(n):
            a, b = ref[i], memb[i]
            inter = both[(a, b)]
            total[i] += inter / (ref_size[a] + size[b] - inter)
    return [t / len(others) for t in total]


def _top(values: dict[int, float], share: float, minimum: float) -> set[int]:
    """The nodes in the top ``share`` by value (at least one if any qualifies), each with value >= minimum."""
    cand = sorted((v, i) for i, v in values.items() if v >= minimum and v > 0)
    if not cand:
        return set()
    k = max(1, math.ceil(share * len(values)))
    cand.sort(key=lambda x: (-x[0], x[1]))
    cut = cand[min(k, len(cand)) - 1][0]
    return {i for v, i in cand if v >= cut}


def topics_for(descriptions: dict[str, str | None], patterns: dict[str, list[re.Pattern[str]]]) -> dict[str, list[str]]:
    """Topics from the profile description (keyword rules). Accounts with no match are left out."""
    out: dict[str, list[str]] = {}
    if not patterns:
        return out
    for uid, desc in descriptions.items():
        if not desc:
            continue
        hit = [name for name, pats in patterns.items() if any(p.search(desc) for p in pats)]
        if hit:
            out[uid] = sorted(hit)
    return out


def propagate(g: ig.Graph, alters: list[str], direct: dict[str, list[str]], p: dict[str, Any]) -> dict[str, str]:
    """Label propagation: an account with no direct topic takes the topic of most of its link weight.

    A topic needs ``topic_min_share`` of the weight of the labeled neighbors and
    ``topic_min_neighbors`` labeled neighbors with it. Labels from earlier rounds count.
    Returns {user_id: topic} for inferred topics only.
    """
    labels: dict[int, set[str]] = {i: set(direct[u]) for i, u in enumerate(alters) if u in direct}
    inferred: dict[int, str] = {}
    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for e in g.es:
        a, b = e.tuple
        adj[a].append((b, e["weight"]))
        adj[b].append((a, e["weight"]))
    for _ in range(int(p["topic_rounds"])):
        new: dict[int, str] = {}
        for i in range(len(alters)):
            if i in labels or i in inferred:
                continue
            score: dict[str, float] = defaultdict(float)
            count: Counter[str] = Counter()
            total = 0.0
            for j, w in adj.get(i, []):
                tj = labels.get(j) or ({inferred[j]} if j in inferred else set())
                if not tj:
                    continue
                total += w
                for t in tj:
                    score[t] += w / len(tj)
                    count[t] += 1
            if total <= 0:
                continue
            best = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if best[1] / total >= float(p["topic_min_share"]) and count[best[0]] >= int(p["topic_min_neighbors"]):
                new[i] = best[0]
        if not new:
            break
        inferred.update(new)
    return {alters[i]: t for i, t in inferred.items()}


def run_groups(nodes: list[str], edges: list[dict[str, Any]], ego: str,
               descriptions: dict[str, str | None], topics_cfg: Any, params: dict[str, Any] | None = None,
               ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ({user_id: group record}, counts). The owner gets a record with no community."""
    p = {**GROUP_DEFAULTS, **(params or {})}
    runs = int(p["runs"])
    if runs < 20:
        raise ValueError("analyze.groups.runs must be 20 or more (TSC-11)")
    patterns = check_topics(topics_cfg)
    g, alters = undirected_alters(nodes, edges, ego)
    out: dict[str, dict[str, Any]] = {}
    counts: dict[str, Any] = {"leiden_runs": runs, "resolution": float(p["resolution"])}
    if alters:
        parts = leiden_runs(g, float(p["resolution"]), runs, int(p["random_seed"]))
        best = max(range(len(parts)), key=lambda k: (parts[k][1], -k))
        ref = parts[best][0]
        stab = stability(ref, [m for k, (m, _) in enumerate(parts) if k != best])
        size = Counter(ref)
        # Community number = rank by size (0 = largest). Small groups get no number.
        big = sorted((c for c, s in size.items() if s >= int(p["min_community_size"])), key=lambda c: (-size[c], c))
        rank = {c: r for r, c in enumerate(big)}
        comm = [rank.get(c) for c in ref]
        counts.update({"communities": len(big), "leiden_quality": round(parts[best][1], 4),
                       "modularity": round(g.modularity(ref, weights="weight" if g.ecount() else None), 4)
                       if g.ecount() else 0.0,
                       "mean_stability": round(sum(stab) / len(stab), 4)})
        deg = {i: float(d) for i, d in enumerate(g.degree())}
        hubs = _top(deg, float(p["hub_top_share"]), float(p["hub_min_degree"]))
        # Undirected betweenness with distance 1/w: a bridge carries paths in both directions.
        # The directed value of the metrics stage is 0 for an account that only sends links.
        if g.ecount():
            dist = [1.0 / w if w > 0 else math.inf for w in g.es["weight"]]
            bt = dict(enumerate(float(x) for x in g.betweenness(directed=False, weights=dist)))
        else:
            bt = {i: 0.0 for i in range(len(alters))}
        bridges_bt = _top(bt, float(p["bridge_top_share"]), 0.0)
        links_into: dict[int, Counter[int]] = defaultdict(Counter)
        for e in g.es:
            a, b = e.tuple
            if comm[b] is not None:
                links_into[a][comm[b]] += 1
            if comm[a] is not None:
                links_into[b][comm[a]] += 1
        reach: dict[int, int] = {}
        for i in range(len(alters)):
            reach[i] = sum(1 for c, k in links_into[i].items() if c != comm[i] and k >= int(p["bridge_min_links"]))
        bridges_cm = {i for i, k in reach.items() if k >= int(p["bridge_min_communities"])}
        direct = topics_for({u: descriptions.get(u) for u in alters}, patterns)
        inferred = propagate(g, alters, direct, p)
        for i, u in enumerate(alters):
            reasons = []
            if i in bridges_bt:
                reasons.append("betweenness")
            if i in bridges_cm:
                reasons.append("communities")
            out[u] = {
                "community": comm[i], "community_size": size[ref[i]], "stability": round(stab[i], 4),
                "is_hub": int(i in hubs), "is_bridge": int(bool(reasons)),
                "bridge_reason": ",".join(reasons) or None,
                "bridge_communities": reach[i],
                "topics": {"direct": direct.get(u, []), "inferred": inferred.get(u)},
            }
        counts.update({"hubs": len(hubs), "bridges": sum(1 for r in out.values() if r["is_bridge"]),
                       "topic_direct": len(direct), "topic_inferred": len(inferred)})
    own = topics_for({ego: descriptions.get(ego)}, patterns)
    out[ego] = {"community": None, "community_size": None, "stability": None, "is_hub": 0, "is_bridge": 0,
                "bridge_reason": None, "bridge_communities": 0,
                "topics": {"direct": own.get(ego, []), "inferred": None}}
    return out, counts
