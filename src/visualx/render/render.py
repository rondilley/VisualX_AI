"""The renderer: one self-contained HTML file (ARCHITECTURE AD-09, FR-40 to FR-50).

``run_render`` reads the stage outputs from the store, builds one data payload
(compact arrays, gzip, base64), and fills a Jinja2 template with autoescape on.
The page inlines the vendor JavaScript (checked against VENDOR.lock at each
render, SR-04), ``app.js``, and the styles. It writes
``<renders_dir>/map_<stage_run_id>.html`` and records the file in ``renders``
and each user ID in the payload in ``render_members`` (AD-10).

Security controls (threat T1, ARCHITECTURE section 13.2):

* Text from X goes only into the base64 payload (SR-05). No Jinja2 variable
  holds a value from X. The page puts the text into the DOM with
  ``textContent`` only (SR-02, in app.js).
* The page has a strict CSP (SR-09) and loads nothing (SR-03).
* Colors and marks come from configuration and enums, checked here again,
  never from X (no X value goes into CSS).
* The page holds no post text in v0.1 (DR-09) and no profile image URL (SR-03).

Payload additions for the map controls: per node the posts and reposts seen
(counts from ``interactions``, no text); per edge the first and last
interaction time in whole minutes after ``meta.t0`` (None when the endpoint
gave no time); and the ``findings`` rows. A finding member that is not a node
still goes into ``render_members`` (AD-10). A finding severity outside
SEVERITIES becomes "info", because the page uses it as a CSS class.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
from markupsafe import Markup

from ..config import Config
from ..logs import RunLog
from ..model import INTERACTION_TYPES, SIGNAL_STATES, STATE_LABELS
from ..store import Store, now_iso

HERE = Path(__file__).resolve().parent
VENDOR_DIR = HERE / "vendor"
LOCK_NAME = "VENDOR.lock"
TEMPLATE_NAME = "template.html.j2"
APP_JS = "app.js"
PAYLOAD_VERSION = 1

# SR-09. Change only with a recorded reason (ARCHITECTURE AD-09).
# script-src lists the SHA-256 hash of each script element that the page runs
# (the vendor files and app.js), not 'unsafe-inline'. An injected inline
# script then does not run, so it cannot send data out by navigation, which
# connect-src does not control (review 20261103, C-01). The template has static
# style attributes, so style-src keeps 'unsafe-inline'; no value from X goes into
# CSS. ``{scripts}`` is the list of hashes.
CSP_TEMPLATE = (
    "default-src 'none'; script-src {scripts}; script-src-attr 'none'; style-src 'unsafe-inline'; "
    "img-src 'none'; connect-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'"
)
_CSP_SLOT = "VX-CSP-SLOT-7f3a"
# The node size modes of the page (the Node size control in the template).
SIZE_MODES: tuple[str, ...] = ("weighted_degree", "followers", "activity")
# The payload element of the template. Deletion (delete.py) uses this pattern to
# decode each page, so the two cannot drift apart (review 20261103, A-23).
PAYLOAD_RE = re.compile(
    r'<script[^>]*id="vx-payload"[^>]*>\s*([A-Za-z0-9+/=\s]+?)\s*</script>', re.DOTALL
)
# A script element that runs: no type, or a JavaScript type. The payload element
# has type="application/octet-stream" and does not run, so it needs no hash.
_SCRIPT_EL = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL | re.IGNORECASE)
_TYPE_ATTR = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)


def _runs(attrs: str) -> bool:
    m = _TYPE_ATTR.search(attrs)
    return m is None or m.group(1).lower() in ("text/javascript", "module", "application/javascript")


def script_hashes(html: str) -> list[str]:
    """CSP source expressions ('sha256-...') for each script element in ``html`` that runs.

    The hash is over the text of the element after the HTML parser makes each CR
    LF and each CR an LF, as the browser calculates it.
    """
    out = []
    for m in _SCRIPT_EL.finditer(html):
        if not _runs(m.group("attrs")):
            continue
        body = m.group("body").replace("\r\n", "\n").replace("\r", "\n")
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        out.append(f"'sha256-{digest}'")
    return out


def csp_for(html: str) -> str:
    """The CSP for a page: CSP_TEMPLATE with the hashes of its scripts."""
    hashes = script_hashes(html)
    if not hashes:
        raise RenderError("the page has no script to hash")
    return CSP_TEMPLATE.format(scripts=" ".join(dict.fromkeys(hashes)))

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MARK = re.compile(r"^[!-~]{1,2}$")  # printable ASCII, no space
_UNSAFE_IN_SCRIPT = re.compile(r"</script|<!--|<script", re.IGNORECASE)
# Keys from the collect stage record that the header can show. Only scalars.
_WINDOW_KEYS = ("window_days", "window_start", "window_end", "since", "until", "start_time", "end_time")

# Profile fields that go into the page. profile_image_url is not one of them (SR-03).
# Finding severities (store.findings). The value becomes a CSS class, so only these pass.
SEVERITIES = ("info", "notable", "high")
_DIGITS_ID = re.compile(r"^[0-9]{1,25}$")

PROFILE_FIELDS = (
    "description", "url", "location", "created_at", "followers_count", "following_count",
    "tweet_count", "listed_count", "verified_type", "protected",
)


class RenderError(RuntimeError):
    """The render must stop (a vendor file mismatch, a defect in the stage data, or a bad config)."""


# -- vendor chain (SR-04, TSC-31) -------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_vendor(vendor_dir: Path | None = None) -> list[tuple[str, str]]:
    """Check every file in VENDOR.lock and return the inline scripts in order.

    Raises RenderError on a missing lock, a missing file, a SHA-256 difference,
    or a script that could close its ``<script>`` element.
    """
    vendor_dir = VENDOR_DIR if vendor_dir is None else vendor_dir
    lock_path = vendor_dir / LOCK_NAME
    if not lock_path.is_file():
        raise RenderError(f"{lock_path} not found. Run 'python tools/vendor.py fetch'.")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RenderError(f"{lock_path} is not valid JSON") from e
    packages = lock.get("packages") or []
    if not packages:
        raise RenderError(f"{lock_path} lists no packages")
    scripts: list[tuple[str, str]] = []
    for pkg in packages:
        name = f"{pkg.get('name')}@{pkg.get('version')}"
        if not str(pkg.get("integrity", "")).startswith("sha512-"):
            raise RenderError(f"VENDOR.lock: {name} has no sha512 integrity value")
        for f in pkg.get("files") or []:
            p = (vendor_dir / f["dest"]).resolve()
            if vendor_dir.resolve() not in p.parents or not p.is_file():
                raise RenderError(f"vendor file for {name} is missing: {f['dest']}")
            data = p.read_bytes()
            got = _sha256(data)
            if got != f.get("sha256"):
                raise RenderError(
                    f"vendor file {f['dest']} ({name}) has sha256 {got}, VENDOR.lock has {f.get('sha256')}. "
                    "The render stops (SR-04)."
                )
            if pkg.get("role") == "inline" and f["dest"].endswith(".js"):
                text = data.decode("utf-8")
                if _UNSAFE_IN_SCRIPT.search(text):
                    raise RenderError(f"vendor file {f['dest']} contains a sequence that can end a script element")
                scripts.append((name, text))
    if not scripts:
        raise RenderError("VENDOR.lock lists no inline scripts")
    return scripts


# -- payload ----------------------------------------------------------------

def encode_payload(obj: Any) -> str:
    """UTF-8 JSON, then gzip, then base64 (SR-05). ``delete.decode_payloads`` reverses it."""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9, mtime=0)).decode("ascii")


def decode_payload(b64: str) -> Any:
    return json.loads(gzip.decompress(base64.b64decode(b64)).decode("utf-8"))


def _num(v: Any, digits: int = 4) -> float | int | None:
    """A finite number rounded to ``digits`` significant digits, or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    f = float(v)
    if not math.isfinite(f):
        return None
    if f == 0:
        return 0
    r = round(f, max(0, digits - 1 - int(math.floor(math.log10(abs(f))))))
    return int(r) if float(r).is_integer() and abs(r) < 2**53 else r


def _render_config(cfg: Config) -> dict[str, Any]:
    """Display settings from configuration only. Checked again here: no value from X goes into CSS."""
    rc = cfg.get("render", {}) or {}
    state_colors = {s: str(rc.get("state_colors", {}).get(s, "")) for s in SIGNAL_STATES}
    edge_colors = {t: str(rc.get("edge_colors", {}).get(t, "")) for t in INTERACTION_TYPES}
    marks = {s: str(rc.get("state_shapes", {}).get(s, "")) for s in SIGNAL_STATES}
    for k, v in {**state_colors, **edge_colors}.items():
        if not _HEX.match(v):
            raise RenderError(f"render color for {k!r} must be #RRGGBB, not {v!r}")
    for k, v in marks.items():
        if not _MARK.match(v):
            raise RenderError(f"render.state_shapes.{k} must be one or two printable ASCII characters")
    size_mode = rc.get("size_mode", "weighted_degree")
    if size_mode not in SIZE_MODES:
        raise RenderError("render.size_mode must be one of " + ", ".join(SIZE_MODES))
    core_max = int(rc.get("core_view_max_nodes", 2000))
    if core_max < 1:
        raise RenderError("render.core_view_max_nodes must be 1 or more")
    min_w = float(rc.get("min_tie_weight", 0.0))
    if not math.isfinite(min_w) or min_w < 0:
        raise RenderError("render.min_tie_weight must be 0 or more")
    return {
        "states": list(SIGNAL_STATES),
        "state_labels": {s: STATE_LABELS[s] for s in SIGNAL_STATES},
        "state_colors": state_colors,
        "state_marks": marks,
        "types": list(INTERACTION_TYPES),
        "edge_colors": edge_colors,
        "size_mode": size_mode,
        "core_view_max_nodes": core_max,
        "min_tie_weight": min_w,
        "reciprocity_factor": _num(cfg.get("weights.reciprocity_factor", 1.0)),
        "topics": [str(t) for t in (cfg.topics or {}) if _TOPIC.match(str(t))],
    }


def _collect_info(store: Store, cfg: Config) -> dict[str, Any]:
    """Window and sources for the header (FR-49). Only scalars from our own stage records."""
    info: dict[str, Any] = {"collect_stage_run": None, "window": {}, "counts": {}, "run_id": None}
    row = store.latest_stage_run("collect")
    if row is not None:
        info["collect_stage_run"] = row["stage_run_id"]
        info["run_id"] = row["run_id"]
        info["finished_at"] = row["finished_at"]
        try:
            counts = json.loads(row["counts_json"] or "{}")
        except json.JSONDecodeError:
            counts = {}
        if isinstance(counts, dict):
            for k, v in counts.items():
                if k in _WINDOW_KEYS and isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    info["window"][k] = v
                elif isinstance(v, (int, float)) and not isinstance(v, bool) and re.fullmatch(r"[a-z0-9_]{1,40}", k):
                    info["counts"][k] = _num(v, 6)
    span = store.q1(
        "SELECT MIN(interaction_created_at) AS a, MAX(interaction_created_at) AS b, COUNT(*) AS n FROM interactions"
    )
    if span is not None and span["n"]:
        info["observed"] = {"first": span["a"], "last": span["b"], "interactions": span["n"]}
    info["sources"] = [r["source_endpoint"] for r in store.q("SELECT DISTINCT source_endpoint FROM interactions ORDER BY 1")]
    return info


def _epoch(ts: Any) -> int | None:
    """An ISO 8601 time from our own store as integer seconds since 1970, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)  # Python 3.11+ reads a trailing "Z"
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def _edge_times(raw: Any) -> tuple[int | None, int | None]:
    """First and last interaction time of an edge from its timestamps_json (all types)."""
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    lo: int | None = None
    hi: int | None = None
    for v in obj.values():
        for ts in v if isinstance(v, list) else []:
            t = _epoch(ts)
            if t is None:
                continue
            lo = t if lo is None or t < lo else lo
            hi = t if hi is None or t > hi else hi
    return lo, hi


def _table_exists(store: Store, name: str) -> bool:
    return store.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def _ids_in(obj: Any, out: set[str], depth: int = 0) -> None:
    """Collect strings of digits from a finding's evidence. They can be user IDs (AD-10)."""
    if depth > 8:
        return
    if isinstance(obj, str):
        if _DIGITS_ID.match(obj):
            out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _ids_in(k, out, depth + 1)
            _ids_in(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _ids_in(v, out, depth + 1)


# Evidence keys that must not go into the page: post text, bios, and names are
# text from X that the detectors must not keep (DR-09). The detectors keep a hash.
_EVIDENCE_DENY = frozenset({"text", "texts", "bio", "description", "name", "names", "note_tweet"})
_EVIDENCE_MAX_STR = 200
_EVIDENCE_MAX_ITEMS = 200


def _clean_evidence(v: Any, depth: int = 0) -> Any:
    """The evidence of a finding, limited for the page (review 20261103, C-04).

    Keys in ``_EVIDENCE_DENY`` go out. Strings are cut to 200 characters, lists to
    200 items, and the nesting to 3 levels. The page shows each value with
    ``textContent``, so this is a limit on what the file holds, not an XSS control.
    """
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v[:_EVIDENCE_MAX_STR]
    if depth >= 3:
        return None
    if isinstance(v, list):
        return [_clean_evidence(x, depth + 1) for x in v[:_EVIDENCE_MAX_ITEMS]]
    if isinstance(v, dict):
        return {str(k)[:60]: _clean_evidence(x, depth + 1) for k, x in list(v.items())[:_EVIDENCE_MAX_ITEMS]
                if str(k) not in _EVIDENCE_DENY}
    return None


def _findings(store: Store, index: dict[str, int]) -> tuple[list[dict[str, Any]], set[str]]:
    """Findings for the Findings drawer. Members become node indexes; other members stay as IDs.

    Returns the findings and each user ID that they name (for render_members, AD-10).
    An empty or absent table gives an empty list.
    """
    if not _table_exists(store, "findings"):
        return [], set()
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    rows = store.q("SELECT finding_id, detector, title, severity, summary, members_json, evidence_json "
                   "FROM findings ORDER BY finding_id")
    for r in rows:
        try:
            members = json.loads(r["members_json"] or "[]")
        except json.JSONDecodeError:
            members = []
        try:
            evidence = json.loads(r["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        if not isinstance(members, list):
            members = []
        mi: list[int] = []
        mx: list[str] = []
        for m in members:
            uid = str(m)
            ids.add(uid)
            if uid in index:
                mi.append(index[uid])
            else:
                mx.append(uid)
        _ids_in(evidence, ids)
        evidence = _clean_evidence(evidence) if isinstance(evidence, dict) else {}
        sev = r["severity"] if r["severity"] in SEVERITIES else "info"
        key = (r["detector"], json.dumps(sorted(str(m) for m in members), ensure_ascii=False, separators=(",", ":")))
        out.append({"id": r["finding_id"], "det": r["detector"], "title": r["title"], "sev": sev,
                    "sum": r["summary"], "m": mi, "mx": mx, "ev": evidence, "_k": key})
    rank = {s: k for k, s in enumerate(reversed(SEVERITIES))}
    out.sort(key=lambda f: (rank[f["sev"]], -len(f["m"]), str(f["id"])))
    return out, ids


def _activity(store: Store) -> dict[str, tuple[int, int]]:
    """For each account: (posts seen, reposts seen) from the interactions table. No text is read."""
    rows = store.q(
        "SELECT source_user_id AS u, "
        "COUNT(DISTINCT CASE WHEN type <> 'repost' THEN source_post_id END) "
        "+ SUM(CASE WHEN type <> 'repost' AND source_post_id IS NULL THEN 1 ELSE 0 END) AS posts, "
        "SUM(CASE WHEN type = 'repost' THEN 1 ELSE 0 END) AS reposts "
        "FROM interactions GROUP BY source_user_id"
    )
    return {r["u"]: (int(r["posts"] or 0), int(r["reposts"] or 0)) for r in rows}


_TOPIC = re.compile(r"^[a-z0-9_]{1,30}$")


def _groups(store: Store) -> dict[str, dict[str, Any]]:
    """The analyze stage groups (FR-26 to FR-28, FR-30). Topic names are from config/topics.yaml only."""
    if not _table_exists(store, "node_groups"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in store.q("SELECT * FROM node_groups"):
        try:
            t = json.loads(r["topics_json"] or "{}")
        except json.JSONDecodeError:
            t = {}
        direct = [x for x in (t.get("direct") or []) if isinstance(x, str) and _TOPIC.match(x)]
        inf = t.get("inferred")
        out[r["user_id"]] = {**dict(r), "topics": {
            "direct": direct, "inferred": inf if isinstance(inf, str) and _TOPIC.match(inf) else None}}
    return out


def _groups_meta(store: Store) -> dict[str, Any]:
    """Counts of the newest analyze record: communities, modularity, mean stability, runs."""
    row = store.latest_stage_run("analyze")
    if row is None:
        return {}
    try:
        counts = json.loads(row["counts_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    keys = ("leiden_runs", "resolution", "communities", "modularity", "mean_stability", "hubs", "bridges",
            "topic_direct", "topic_inferred")
    return {k: _num(counts[k], 6) for k in keys if isinstance(counts.get(k), (int, float))}


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """A copy of the payload for a file that the owner can show to other persons (T7, v0.2).

    No handle, name, user ID, profile field, topic, signal state, rule result, override, or
    finding stays. The owner node is "Owner". The other nodes are "Account <n>".

    The file must not let a reader find an account again from public data
    (review 20261103, C-02 and C-03):

    - The node arrays get a secret random permutation (``secrets.SystemRandom``),
      and the labels follow the new order. Before, the arrays kept the user ID
      order and the label shuffle used the node count as its seed.
    - No absolute time stays: ``t0``, the collection window, the observed times,
      the run IDs, and the render time are removed. The link times stay only as
      whole days after the first link, so a reader cannot match a link to the
      minute of a public reply. Without ``t0`` the page shows no dates.

    The structure (links, types, weights, layout, communities, hubs, bridges) stays.
    """
    import secrets

    p = json.loads(json.dumps(payload))
    n = len(p["n"]["id"])
    N = p["n"]
    # Secret permutation of all node columns: new position k holds old node perm[k].
    perm = list(range(n))
    secrets.SystemRandom().shuffle(perm)
    new_of = {old: k for k, old in enumerate(perm)}
    for key, col in N.items():
        if not isinstance(col, list) or len(col) != n:
            raise RenderError(f"node column {key!r} does not have one value for each node")
        N[key] = [col[old] for old in perm]
    p["e"]["s"] = [new_of[i] for i in p["e"]["s"]]
    p["e"]["t"] = [new_of[i] for i in p["e"]["t"]]
    old_ego = p["meta"]["ego_index"]
    ego = new_of[old_ego] if isinstance(old_ego, int) and 0 <= old_ego < n else -1
    p["meta"]["ego_index"] = ego
    others = [i for i in range(n) if i != ego]
    label = {i: k + 1 for k, i in enumerate(others)}
    # Link times: whole days after the first link, no absolute anchor.
    for k in ("ft", "lt"):
        p["e"][k] = [None if v is None else (int(v) // 1440) * 1440 for v in p["e"][k]]
    for i in range(n):
        N["id"][i] = "r" + str(label.get(i, 0))
        N["h"][i] = None
        N["nm"][i] = "Owner" if i == ego else "Account " + str(label[i])
        N["st"][i] = SIGNAL_STATES.index("not_scored")
        N["tp"][i] = None
        N["tc"][i] = []
        N["ti"][i] = None
        for k in ("cn", "cps", "cmv", "ctw", "cra"):
            N[k][i] = None
    for f in p["p"]:
        p["p"][f] = [None] * n
    p["hs"] = [0] * n
    p["r"], p["ov"], p["f"] = [], {}, []
    meta = p["meta"]
    meta["ego_handle"] = None
    meta["redacted"] = True
    meta["changes"] = None
    meta["counts"]["states"] = {s: (n if s == "not_scored" else 0) for s in SIGNAL_STATES}
    meta["counts"]["rules_fired"] = 0
    meta["counts"]["findings"] = 0
    meta["counts"]["without_state_record"] = n
    meta["collect"] = {"sources": []}
    meta["t0"] = None
    meta["render_id"] = None
    meta["inputs"] = {}
    meta["generated_at"] = "not shown in a redacted file"
    p["cfg"]["topics"] = []
    return p


def _changes(store: Store, cols: dict[str, list[Any]], findings: list[dict[str, Any]],
             snapshot_id: str | None) -> dict[str, Any] | None:
    """Changes since the snapshot before ``snapshot_id`` (snapshot.py), as node columns and counts.

    Adds the node columns cn (1 = new account), cps (earlier state index), cmv (1 = moved
    to another community), ctw (change of the tie weight to the owner), and cra (rules that
    fired since then), and marks each new finding with ``new``. Returns the header block, or
    None when there is no earlier snapshot.
    """
    from .. import snapshot as snap

    n = len(cols["id"])
    for k in ("cn", "cps", "cmv", "ctw", "cra"):
        cols[k] = [None] * n
    for f in findings:
        f["new"] = 0
    if snapshot_id is None:
        return None
    base = snap.previous(store, snapshot_id)
    if base is None:
        return None
    d = snap.diff(store, base, snapshot_id)
    new_keys = {(det, mj) for det, mj in d["new_findings"]}
    for f in findings:
        f["new"] = int(f["_k"] in new_keys)
    for i, uid in enumerate(cols["id"]):
        ch = d["users"].get(uid)
        if not ch:
            continue
        cols["cn"][i] = 1 if ch.get("new") else None
        if "state" in ch and ch["state"][0] in SIGNAL_STATES:
            cols["cps"][i] = SIGNAL_STATES.index(ch["state"][0])
        cols["cmv"][i] = 1 if ch.get("moved") else None
        cols["ctw"][i] = _num(ch.get("tie"))
        cols["cra"][i] = ch.get("rules_added") or None
    at = {r["snapshot_id"]: r["created_at"] for r in store.q(
        "SELECT snapshot_id, created_at FROM snapshots WHERE snapshot_id IN (?,?)", (base, snapshot_id))}
    return {"base": base, "base_at": at.get(base), "head_at": at.get(snapshot_id), "counts": d["counts"]}


def build_payload(cfg: Config, store: Store, render_id: str, inputs: dict[str, str],
                  snapshot_id: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return (payload, ids). The payload has no post text and no image URLs.

    Post IDs can occur in finding evidence (for example the posts of a reply burst), so
    each ID that a finding names goes into ``render_members`` as a user ID, and each such
    ID that is not a node also as a post ID (AD-10, review 20261102-225212).
    """
    nodes = store.q(
        "SELECT user_id, handle, name, profile_status, is_ego, in_degree, out_degree, degree, weighted_degree, "
        "ego_weight, pagerank, betweenness, x, y FROM nodes ORDER BY user_id"
    )
    if not nodes:
        raise RenderError("the nodes table is empty. Run 'visualx build' and 'visualx analyze' first.")
    index = {r["user_id"]: i for i, r in enumerate(nodes)}
    profiles = store.profiles()
    states = {r["user_id"]: r for r in store.q("SELECT user_id, state, total_points, override_note FROM node_states")}
    rules_by_user: dict[str, list[Any]] = {}
    for r in store.q("SELECT user_id, rule_id, family, points, cause, hard FROM rule_results ORDER BY user_id, rule_id"):
        rules_by_user.setdefault(r["user_id"], []).append(r)

    n = len(nodes)
    cols: dict[str, list[Any]] = {k: [None] * n for k in (
        "id", "h", "nm", "ps", "st", "x", "y", "wd", "dg", "din", "dout", "ew", "pr", "bt", "tp", "pp", "rp")}
    activity = _activity(store)
    prof: dict[str, list[Any]] = {k: [None] * n for k in PROFILE_FIELDS}
    has_state = [0] * n
    ego = -1
    rules: list[list[Any]] = []
    overrides: dict[str, str] = {}
    for i, r in enumerate(nodes):
        uid = r["user_id"]
        p = profiles.get(uid)
        cols["id"][i] = uid
        cols["h"][i] = r["handle"] if r["handle"] is not None else (p or {}).get("handle")
        cols["nm"][i] = r["name"] if r["name"] is not None else (p or {}).get("name")
        cols["ps"][i] = r["profile_status"]
        cols["pp"][i], cols["rp"][i] = activity.get(uid, (0, 0))
        for k, src in (("x", "x"), ("y", "y"), ("wd", "weighted_degree"), ("ew", "ego_weight"),
                       ("pr", "pagerank"), ("bt", "betweenness")):
            cols[k][i] = _num(r[src])
        for k, src in (("dg", "degree"), ("din", "in_degree"), ("dout", "out_degree")):
            cols[k][i] = r[src]
        if r["x"] is None or r["y"] is None:
            raise RenderError(f"node {i} has no layout position. Run 'visualx analyze'.")
        if r["is_ego"]:
            ego = i
        st = states.get(uid)
        state = "not_scored"
        if st is not None:
            state = st["state"]
            has_state[i] = 1
            cols["tp"][i] = _num(st["total_points"])
            if st["override_note"]:
                overrides[str(i)] = st["override_note"]
        if state not in SIGNAL_STATES:
            raise RenderError(f"node {i} has an unknown state {state!r}")
        fired = rules_by_user.get(uid, [])
        # Invariant 5: a state other than no_signals / not_scored needs its causes.
        if state in ("some_signals", "many_signals") and not fired and st is not None and not st["override_note"]:
            raise RenderError(f"node {i} has state {state} with no rule results and no override note (invariant 5)")
        cols["st"][i] = SIGNAL_STATES.index(state)
        for rr in fired:
            rules.append([i, rr["rule_id"], rr["family"], _num(rr["points"]), rr["cause"], int(rr["hard"] or 0)])
        if p is not None:
            for f in PROFILE_FIELDS:
                v = p.get(f)
                prof[f][i] = int(v) if isinstance(v, bool) else v

    edges = store.q(
        "SELECT source, target, reply_count, quote_count, mention_count, repost_count, weight, pair_weight, "
        "timestamps_json FROM edges ORDER BY source, target"
    )
    ecols: dict[str, list[Any]] = {k: [] for k in ("s", "t", "cr", "cq", "cm", "cp", "w", "pw", "ft", "lt")}
    times = [_edge_times(e["timestamps_json"]) for e in edges]
    firsts = [a for a, _ in times if a is not None]
    t0 = min(firsts) if firsts else None
    for e, (first, last) in zip(edges, times, strict=True):
        si, ti = index.get(e["source"]), index.get(e["target"])
        if si is None or ti is None:
            raise RenderError("an edge refers to a node that is not in the nodes table. Run 'visualx build' again.")
        ecols["s"].append(si)
        ecols["t"].append(ti)
        ecols["cr"].append(e["reply_count"])
        ecols["cq"].append(e["quote_count"])
        ecols["cm"].append(e["mention_count"])
        ecols["cp"].append(e["repost_count"])
        ecols["w"].append(_num(e["weight"]))
        ecols["pw"].append(_num(e["pair_weight"]))
        # Whole minutes after meta.t0. None: the endpoint gave no time for this link (DR-02).
        ecols["ft"].append(None if first is None or t0 is None else (first - t0) // 60)
        ecols["lt"].append(None if last is None or t0 is None else (last - t0) // 60)

    groups = _groups(store)
    for k in ("cm", "cs", "hb", "br", "bc", "tc", "ti"):
        cols[k] = [None] * n
    for i, uid in enumerate(cols["id"]):
        gr = groups.get(uid)
        if gr is None:
            continue
        cols["cm"][i] = gr["community"]
        cols["cs"][i] = _num(gr["stability"], 3)
        cols["hb"][i] = int(gr["is_hub"] or 0)
        cols["br"][i] = int(gr["is_bridge"] or 0)
        cols["bc"][i] = gr["bridge_communities"]
        cols["tc"][i] = gr["topics"].get("direct") or []
        cols["ti"][i] = gr["topics"].get("inferred")

    findings, finding_ids = _findings(store, index)
    changes = _changes(store, cols, findings, snapshot_id)
    for f in findings:
        f.pop("_k", None)

    ego_id = store.ego_id
    if ego < 0 and ego_id in index:
        ego = index[ego_id]
    missing = sum(1 for s in cols["ps"] if s == "missing")
    state_counts = {s: 0 for s in SIGNAL_STATES}
    for s in cols["st"]:
        state_counts[SIGNAL_STATES[s]] += 1
    meta = {
        "render_id": render_id,
        "generated_at": now_iso(),
        "inputs": inputs,
        "ego_index": ego,
        "ego_handle": store.get_meta("ego_handle"),
        "collect": _collect_info(store, cfg),
        "counts": {"nodes": n, "edges": len(edges), "missing_profile": missing, "states": state_counts,
                   "rules_fired": len(rules), "without_state_record": n - sum(has_state),
                   "findings": len(findings), "edges_without_time": len(edges) - len(firsts)},
        "t0": t0,
        "groups": _groups_meta(store),
        "redacted": False,
        "changes": changes,
    }
    payload = {
        "v": PAYLOAD_VERSION,
        "meta": meta,
        "cfg": _render_config(cfg),
        "n": cols,
        "p": prof,
        "hs": has_state,
        "r": rules,
        "ov": overrides,
        "e": ecols,
        "f": findings,
    }
    # render_members must hold each user ID in the payload (AD-10): nodes and finding members.
    extra = sorted(finding_ids - set(cols["id"]))
    return payload, list(cols["id"]) + extra


# -- page -------------------------------------------------------------------

def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(HERE)),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def render_page(payload_b64: str, vendor_scripts: list[tuple[str, str]] | None = None) -> str:
    """Fill the template. ``payload_b64`` must be base64 only (SR-05)."""
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", payload_b64):
        raise RenderError("the payload is not base64")
    scripts = load_vendor() if vendor_scripts is None else vendor_scripts
    app_js = (HERE / APP_JS).read_text(encoding="utf-8")
    if _UNSAFE_IN_SCRIPT.search(app_js):
        raise RenderError("app.js contains a sequence that can end a script element")
    tpl = _env().get_template(TEMPLATE_NAME)
    html = tpl.render(
        csp=Markup(_CSP_SLOT),
        vendor_scripts=[(name, Markup(text)) for name, text in scripts],
        app_js=Markup(app_js),
        payload=payload_b64,
    )
    if html.count(_CSP_SLOT) != 1:
        raise RenderError("the CSP slot is not in the template exactly one time")
    # The CSP holds only constants and hashes of the static scripts, no value from X.
    return html.replace(_CSP_SLOT, csp_for(html), 1)


def run_render(cfg: Config, store: Store, log: RunLog, out_path: Path | None = None, redact: bool = False) -> Path:
    """Render the newest analyze and score outputs to one HTML file. Return the file path.

    ``redact`` writes the structure only (``redact_payload``). The render is still
    recorded with its members, so a deletion finds it and deletes it (AD-10).
    """
    inputs = store.check_fresh("render")
    vendor_scripts = load_vendor()  # stop before any write if a vendor file is changed
    collect = store.latest_stage_run("collect")
    run_id = collect["run_id"] if collect is not None else None
    from ..snapshot import take

    snapshot_id = take(store)  # the same analyze and score outputs use the same snapshot
    sid = store.begin_stage("render", run_id, inputs, cfg.hash())
    render_id = sid
    payload, user_ids = build_payload(cfg, store, render_id, inputs, snapshot_id)
    node_ids = set(payload["n"]["id"])
    if redact:
        payload = redact_payload(payload)
    b64 = encode_payload(payload)
    html = render_page(b64, vendor_scripts)
    name = f"map_{sid}_redacted.html" if redact else f"map_{sid}.html"
    path = Path(out_path) if out_path is not None else cfg.renders_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8", newline="\n")
    tmp.replace(path)
    counts = {
        "nodes": payload["meta"]["counts"]["nodes"],
        "edges": payload["meta"]["counts"]["edges"],
        "hidden_missing_profile": payload["meta"]["counts"]["missing_profile"],
        "rules_fired": payload["meta"]["counts"]["rules_fired"],
        "payload_bytes": len(b64),
        "file_bytes": path.stat().st_size,
        "redacted": int(redact),
    }
    with store.tx() as c:
        c.execute(
            "INSERT INTO renders(render_id, path, run_id, created_at, stage_run_id) VALUES (?,?,?,?,?)",
            (render_id, str(path.resolve()), run_id, now_iso(), sid),
        )
        c.executemany(
            "INSERT INTO render_members(render_id, user_id, post_id) VALUES (?,?,NULL)",
            [(render_id, u) for u in sorted(set(user_ids))],
        )
        # IDs from findings that are not nodes can be post IDs in the evidence: index them as posts too.
        c.executemany(
            "INSERT INTO render_members(render_id, user_id, post_id) VALUES (?,NULL,?)",
            [(render_id, x) for x in sorted(set(user_ids) - node_ids)],
        )
        store.finish_stage(sid, counts, conn=c)
    log.write("render", stage_run_id=sid, **counts)
    return path
