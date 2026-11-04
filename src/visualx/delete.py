"""Deletion across all locations that can hold an ID (ARCHITECTURE AD-10, DR-04).

Order:
1. Store rows that reference the ID (one transaction), then a redaction sweep of
   text and JSON columns for other copies of the ID. For a user, pattern
   findings lose the user as a member (``_prune_findings``).
2. Raw cache scrub: each cache file that holds the ID is rewritten without it.
3. ``config/overrides.yaml`` entries for a deleted user, and the user's rows in
   the calibration labeling sheets (``score/calibrate.py``).
4. Rendered HTML files that hold the ID are deleted. The caller then renders the
   newest data again without the ID.
5. ``find_id`` checks every location again, with the HTML payloads decoded (TSC-09).
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cache import Cache, _id_pattern, handle_patterns, handle_sub
from .render.render import PAYLOAD_RE  # the renderer owns the payload element form (A-23)
from .score.calibrate import scrub_sheets, sheets_with
from .store import Store

# An X user ID or post ID is a decimal number. A provisional user ID is
# "h:<handle>", and an X handle has 1 to 15 letters, digits, or "_".
_NUMERIC_ID = re.compile(r"[0-9]{1,25}")
_PROVISIONAL_ID = re.compile(r"h:[A-Za-z0-9_]{1,15}")


def validate_ids(ids: list[str], kind: str) -> list[str]:
    """Return ``ids`` if each one has the form of an X ID. Raise ValueError if not.

    An empty or malformed ID makes the LIKE match and the redaction pattern match
    each row, and that damages the store (review 20261103, A-01).
    """
    if not ids:
        raise ValueError("no ID was given")
    bad = []
    for i in ids:
        s = i if isinstance(i, str) else ""
        ok = _NUMERIC_ID.fullmatch(s) is not None or (kind == "user" and _PROVISIONAL_ID.fullmatch(s) is not None)
        if not ok:
            bad.append(repr(i)[:40])
    if bad:
        form = "a number or h:<handle>" if kind == "user" else "a number"
        raise ValueError(f"not a valid {kind} ID (it must be {form}): " + ", ".join(bad))
    return list(ids)




@dataclass
class DeleteReport:
    ids: list[str]
    kind: str
    store_rows: int = 0
    findings_changed: int = 0
    findings_removed: int = 0
    redacted_cells: int = 0
    cache_files_scrubbed: int = 0
    overrides_removed: int = 0
    calibration_rows_removed: int = 0
    renders_deleted: list[str] = field(default_factory=list)
    exports_deleted: list[str] = field(default_factory=list)
    rerendered: str | None = None
    remaining: list[str] = field(default_factory=list)


def decode_payloads(html: str) -> list[Any]:
    """Decode each gzip+base64 data payload in a rendered page."""
    out = []
    for m in PAYLOAD_RE.finditer(html):
        raw = base64.b64decode("".join(m.group(1).split()))
        out.append(json.loads(gzip.decompress(raw).decode("utf-8")))
    return out


def _text_columns(store: Store) -> dict[str, list[str]]:
    cols: dict[str, list[str]] = {}
    for (t,) in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t.startswith("sqlite_"):
            continue
        cols[t] = [r[1] for r in store.conn.execute(f"PRAGMA table_info({t})").fetchall() if (r[2] or "").upper() == "TEXT"]
    return cols


def _delete_store_rows(store: Store, ident: str, kind: str) -> int:
    """Delete rows that reference the ID. ``cache_ids`` stays: Cache.scrub uses it to find files."""
    n = 0
    with store.tx() as c:
        if kind == "user":
            stmts = [
                ("DELETE FROM interactions WHERE source_user_id=? OR target_user_id=?", 2),
                ("DELETE FROM profiles WHERE user_id=?", 1),
                ("DELETE FROM profile_history WHERE user_id=?", 1),
                ("DELETE FROM calibration_samples WHERE user_id=?", 1),
                ("DELETE FROM calibration_labels WHERE user_id=?", 1),
                ("DELETE FROM profile_errors WHERE user_id=?", 1),
                ("DELETE FROM posts WHERE author_id=?", 1),
                ("DELETE FROM nodes WHERE user_id=?", 1),
                ("DELETE FROM node_groups WHERE user_id=?", 1),
                ("DELETE FROM snapshot_nodes WHERE user_id=?", 1),
                ("DELETE FROM edges WHERE source=? OR target=?", 2),
                ("DELETE FROM rule_results WHERE user_id=?", 1),
                ("DELETE FROM rule_skips WHERE user_id=?", 1),
                ("DELETE FROM node_states WHERE user_id=?", 1),
                ("DELETE FROM overrides WHERE user_id=?", 1),
                ("DELETE FROM render_members WHERE user_id=?", 1),
                # Step 6 keeps one cursor row for each seed user, keyed by the user ID.
                # The redaction sweep must not see it: two redacted rows would collide
                # on the (step, item) key (review 20261103, A-02).
                ("DELETE FROM cursor WHERE item=?", 1),
            ]
        else:
            stmts = [
                ("DELETE FROM interactions WHERE source_post_id=? OR target_post_id=?", 2),
                ("DELETE FROM posts WHERE post_id=?", 1),
                ("DELETE FROM render_members WHERE post_id=?", 1),
                ("DELETE FROM cursor WHERE item=?", 1),
            ]
        for sql, k in stmts:
            n += c.execute(sql, (ident,) * k).rowcount
    return n


def _drop_ident(obj: Any, ident: str) -> Any:
    """``obj`` without dict keys equal to ``ident`` and without list items equal to ``ident``."""
    if isinstance(obj, dict):
        return {k: _drop_ident(v, ident) for k, v in obj.items() if k != ident}
    if isinstance(obj, list):
        return [_drop_ident(v, ident) for v in obj if v != ident]
    return obj


def _prune_findings(store: Store, ident: str) -> tuple[int, int]:
    """Remove a deleted user from the pattern findings. Returns (findings changed, findings deleted).

    A finding about a group (two or more members) loses the member. If fewer
    than 2 members stay, the finding has no meaning and is deleted. A finding
    about one account (one member) is deleted when that account is deleted.
    The user ID is also removed from the evidence (keys and list items); the
    redaction sweep then replaces any other copy with ``[deleted]``. Titles and
    summaries hold no IDs or handles (patterns.py).
    """
    changed = removed = 0
    with store.tx() as c:
        rows = c.execute(
            "SELECT finding_id, members_json, evidence_json FROM findings WHERE members_json LIKE ? OR evidence_json LIKE ?",
            (f"%{ident}%", f"%{ident}%"),
        ).fetchall()
        for fid, mj, ej in rows:
            members = json.loads(mj)
            keep = [m for m in members if m != ident]
            if len(keep) != len(members) and (len(members) == 1 or len(keep) < 2):
                c.execute("DELETE FROM findings WHERE finding_id=?", (fid,))
                removed += 1
                continue
            ev = _drop_ident(json.loads(ej), ident)
            c.execute(
                "UPDATE findings SET members_json=?, evidence_json=? WHERE finding_id=?",
                (json.dumps(keep, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(",", ":")), fid),
            )
            changed += 1
        # Snapshot findings (snapshot.py): the same rule, members only (no evidence there).
        if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='snapshot_findings'").fetchone():
            for rid, mj in c.execute("SELECT rowid, members_json FROM snapshot_findings WHERE members_json LIKE ?",
                                     (f"%{ident}%",)).fetchall():
                members = json.loads(mj)
                keep = [m for m in members if m != ident]
                if len(keep) == len(members):
                    continue
                if len(members) == 1 or len(keep) < 2:
                    c.execute("DELETE FROM snapshot_findings WHERE rowid=?", (rid,))
                else:
                    c.execute("UPDATE snapshot_findings SET members_json=? WHERE rowid=?",
                              (json.dumps(keep, ensure_ascii=False, sort_keys=True, separators=(",", ":")), rid))
    return changed, removed


def _redact_store(store: Store, ident: str) -> int:
    """Replace other copies of the ID in text and JSON columns with ``[deleted]``."""
    pat = _id_pattern({ident})
    n = 0
    with store.tx() as c:
        for table, cols in _text_columns(store).items():
            if table in ("cache_index", "cache_ids"):
                continue
            for col in cols:
                rows = c.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE ?", (f"%{ident}%",)).fetchall()
                for rowid, val in rows:
                    new = pat.sub("[deleted]", val)
                    if new == val:
                        continue
                    c.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, rowid))
                    n += 1
    return n


def _remove_overrides(config_dir: Path, ident: str) -> int:
    p = config_dir / "overrides.yaml"
    if not p.exists():
        return 0
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = data.get("overrides", []) if isinstance(data, dict) else data
    keep = [o for o in items if str(o.get("user_id")) != ident]
    removed = len(items) - len(keep)
    if removed:
        out = {"overrides": keep} if isinstance(data, dict) else keep
        p.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return removed


def _html_has(path: Path, ident: str) -> bool:
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    pat = _id_pattern({ident})
    if pat.search(html):
        return True
    return any(pat.search(json.dumps(p, ensure_ascii=False)) for p in decode_payloads(html))


def _delete_renders(store: Store, ident: str, kind: str) -> list[str]:
    col = "user_id" if kind == "user" else "post_id"
    ids = {r["render_id"] for r in store.q(f"SELECT DISTINCT render_id FROM render_members WHERE {col}=?", (ident,))}
    # Also check every known render file directly, in case render_members is incomplete.
    for r in store.q("SELECT render_id, path FROM renders"):
        if r["render_id"] not in ids and _html_has(Path(r["path"]), ident):
            ids.add(r["render_id"])
    deleted = []
    with store.tx() as c:
        for rid in sorted(ids):
            row = c.execute("SELECT path FROM renders WHERE render_id=?", (rid,)).fetchone()
            if row:
                Path(row[0]).unlink(missing_ok=True)
                deleted.append(row[0])
            c.execute("DELETE FROM renders WHERE render_id=?", (rid,))
            c.execute("DELETE FROM render_members WHERE render_id=?", (rid,))
    return deleted


def _export_has(path: Path, ident: str) -> bool:
    try:
        return bool(_id_pattern({ident}).search(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return False


def _delete_exports(store: Store, ident: str) -> list[str]:
    """Delete each export file (FR-51) that holds the user ID. The owner can export again."""
    if store.q1("SELECT name FROM sqlite_master WHERE type='table' AND name='exports'") is None:
        return []
    ids = {r["export_id"] for r in store.q("SELECT DISTINCT export_id FROM export_members WHERE user_id=?", (ident,))}
    for r in store.q("SELECT export_id, path FROM exports"):
        if r["export_id"] not in ids and _export_has(Path(r["path"]), ident):
            ids.add(r["export_id"])
    deleted = []
    with store.tx() as c:
        for eid in sorted(ids):
            row = c.execute("SELECT path FROM exports WHERE export_id=?", (eid,)).fetchone()
            if row:
                Path(row[0]).unlink(missing_ok=True)
                deleted.append(row[0])
            c.execute("DELETE FROM exports WHERE export_id=?", (eid,))
            c.execute("DELETE FROM export_members WHERE export_id=?", (eid,))
    return deleted


def find_id(store: Store, cache: Cache, ident: str, extra_files: list[Path] | None = None) -> list[str]:
    """Return each location that still holds ``ident`` (TSC-09)."""
    pat = _id_pattern({ident})
    hits: list[str] = []
    for table, cols in _text_columns(store).items():
        for col in cols:
            for (val,) in store.conn.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE ?", (f"%{ident}%",)).fetchall():
                if pat.search(val):
                    hits.append(f"store:{table}.{col}")
                    break
    for p in sorted(cache.dir.glob("*.json.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            if pat.search(f.read()):
                hits.append(f"cache:{p.name}")
    hits += sheets_with(store, ident)
    files = [Path(r["path"]) for r in store.q("SELECT path FROM renders")] + list(extra_files or [])
    for f in files:
        if f.exists() and _html_has(f, ident):
            hits.append(f"render:{f}")
    for r in store.q("SELECT path FROM exports"):
        if _export_has(Path(r["path"]), ident):
            hits.append(f"export:{r['path']}")
    return hits


def delete_ids(
    store: Store,
    cache: Cache,
    config_dir: Path,
    ids: list[str],
    kind: str,
    rerender: Callable[[], str | None] | None = None,
    extra_files: list[Path] | None = None,
) -> DeleteReport:
    if kind not in ("user", "post"):
        raise ValueError("kind must be 'user' or 'post'")
    ids = validate_ids(ids, kind)
    ego = store.ego_id
    if kind == "user" and ego in ids:
        raise ValueError("Deletion of the owner's own account ID is not supported. Delete the data folder.")
    rep = DeleteReport(ids=list(ids), kind=kind)
    handles: set[str] = set()
    if kind == "user":
        # The handle is an identifier too (review 20261103, A-13). Read it before the
        # profile rows go, and delete the provisional ID h:<handle> of the same account.
        handles = _handles_of(store, ids)
        ego_handle = (store.get_meta("ego_handle") or "").casefold()
        handles = {h for h in handles if h.casefold() != ego_handle}
        extra = [f"h:{h.casefold()}" for h in sorted(handles) if f"h:{h.casefold()}" not in ids]
        ids = [*ids, *extra]
    for ident in ids:
        # Renders and exports first: they use the render_members and export_members index,
        # which _delete_store_rows removes.
        rep.renders_deleted += _delete_renders(store, ident, kind)
        if kind == "user":
            rep.exports_deleted += _delete_exports(store, ident)
        rep.store_rows += _delete_store_rows(store, ident, kind)
        if kind == "user":
            ch, rm = _prune_findings(store, ident)
            rep.findings_changed += ch
            rep.findings_removed += rm
        rep.redacted_cells += _redact_store(store, ident)
        if kind == "user":
            rep.overrides_removed += _remove_overrides(config_dir, ident)
            rep.calibration_rows_removed += scrub_sheets(store, ident)
        rep.renders_deleted += _delete_renders(store, ident, kind)  # files that only a content scan finds
        # Map files in the renders folder that the renders table does not list (for example
        # a copy from a backup) are deleted too when they hold the ID (review 20261102-225212).
        for f in extra_files or []:
            if f.exists() and _html_has(f, ident):
                f.unlink()
                rep.renders_deleted.append(str(f))
    rep.cache_files_scrubbed = cache.scrub(set(ids))
    if kind == "user":
        # A redacted copy that the renders table does not list holds no ID, so no
        # search can show whether it has the deleted account. It goes too (review
        # 20261103, C-21). The rerender below makes the listed files again.
        listed = {str(Path(r["path"]).resolve()) for r in store.q("SELECT path FROM renders")}
        for f in extra_files or []:
            if f.name.endswith("_redacted.html") and f.exists() and str(f.resolve()) not in listed:
                f.unlink()
                rep.renders_deleted.append(str(f))
    if handles:
        rep.redacted_cells += _redact_handles(store, handles)
        rep.cache_files_scrubbed += cache.scrub_handles(handles)
        for f in [Path(r["path"]) for r in store.q("SELECT path FROM renders")] + list(extra_files or []):
            if f.exists() and _html_has_handle(f, handles):
                f.unlink()
                store.conn.execute("DELETE FROM renders WHERE path=?", (str(f),))
                rep.renders_deleted.append(str(f))
    if rep.renders_deleted and rerender is not None:
        rep.rerendered = rerender()
    for ident in ids:
        rep.remaining += find_id(store, cache, ident, extra_files)
    if handles:
        rep.remaining += find_handles(store, cache, handles, extra_files)
    return rep


def _handles_of(store: Store, ids: list[str]) -> set[str]:
    """The handles that the store holds for these user IDs (profile and profile history)."""
    out: set[str] = set()
    for uid in ids:
        if uid.startswith("h:"):
            out.add(uid[2:])
        for r in store.q("SELECT handle FROM profiles WHERE user_id=? AND handle IS NOT NULL", (uid,)):
            out.add(str(r["handle"]).lstrip("@"))
        for r in store.q("SELECT handle FROM profile_history WHERE user_id=? AND handle IS NOT NULL", (uid,)):
            out.add(str(r["handle"]).lstrip("@"))
    return {h for h in out if _HANDLE_RE.fullmatch(h)}


_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")


def _redact_handles(store: Store, handles: set[str]) -> int:
    """Replace ``@handle`` and URL uses of the handles in text and JSON columns."""
    pats = handle_patterns(handles)
    n = 0
    with store.tx() as c:
        for table, cols in _text_columns(store).items():
            if table in ("cache_index", "cache_ids", "meta"):
                continue
            for col in cols:
                for h in handles:
                    rows = c.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE ?", (f"%{h}%",)).fetchall()
                    for rowid, val in rows:
                        new = val
                        for pat in pats:
                            new = pat.sub("[deleted]", new)
                        if new != val:
                            c.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (new, rowid))
                            n += 1
    return n


def _html_has_handle(path: Path, handles: set[str]) -> bool:
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    texts = [json.dumps(p, ensure_ascii=False) for p in decode_payloads(html)]
    return any(handle_sub(t, handles) != t for t in texts) or any(
        f'"{h}"'.casefold() in t.casefold() for t in texts for h in handles)


def find_handles(store: Store, cache: Cache, handles: set[str], extra_files: list[Path] | None = None) -> list[str]:
    """Each location that still uses a deleted handle as an identifier (A-13)."""
    pats = handle_patterns(handles)
    hits: list[str] = []
    for table, cols in _text_columns(store).items():
        if table in ("cache_index", "cache_ids", "meta"):
            continue
        for col in cols:
            for (val,) in store.conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL").fetchall():
                if any(p.search(str(val)) for p in pats) or str(val).lstrip("@").casefold() in {
                        h.casefold() for h in handles}:
                    hits.append(f"store:{table}.{col} (handle)")
                    break
    for p in sorted(cache.dir.glob("*.json.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            body = json.load(f)
        if json.dumps(_strings_changed(body, handles)) != "false":
            hits.append(f"cache:{p.name} (handle)")
    files = [Path(r["path"]) for r in store.q("SELECT path FROM renders")] + list(extra_files or [])
    for f in files:
        if f.exists() and _html_has_handle(f, handles):
            hits.append(f"render:{f} (handle)")
    return hits


def _strings_changed(obj: Any, handles: set[str]) -> bool:
    if isinstance(obj, dict):
        return any(_strings_changed(v, handles) for v in obj.values())
    if isinstance(obj, list):
        return any(_strings_changed(v, handles) for v in obj)
    if isinstance(obj, str):
        return handle_sub(obj, handles) != obj
    return False
