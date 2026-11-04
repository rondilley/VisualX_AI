"""Raw response cache (ARCHITECTURE AD-03, AD-10, DR-01, DR-10).

One gzip JSON file for each API request, named by the request hash. The
``cache_index`` and ``cache_ids`` tables let deletion find every file that
holds an ID. A scrub rewrites a file without the objects that match an ID and
keeps the data of the other users in the same file.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .model import dumps
from .store import Store, now_iso

# Keys whose value is a user ID or a post ID in an X API v2 object.
ID_KEYS = frozenset({"id", "author_id", "in_reply_to_user_id", "conversation_id"})
# An object whose own identity or author is a deleted ID is dropped whole.
DROP_KEYS = frozenset({"id", "author_id"})
_DROP = object()


def request_hash(endpoint: str, params: dict[str, Any]) -> str:
    """Hash of a request. Secrets never go into ``params``."""
    return hashlib.sha256(f"{endpoint}?{dumps(params)}".encode()).hexdigest()


def _collect_ids(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ID_KEYS and isinstance(v, str):
                out.add(v)
            else:
                _collect_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids(v, out)


class Cache:
    def __init__(self, store: Store, cache_dir: str | Path):
        self.store = store
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, rhash: str) -> Path:
        return self.dir / f"{rhash}.json.gz"

    def get(self, rhash: str) -> dict[str, Any] | None:
        p = self.path_for(rhash)
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)

    def put(self, rhash: str, endpoint: str, body: dict[str, Any]) -> Path:
        p = self.path_for(rhash)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        tmp.replace(p)
        ids: set[str] = set()
        _collect_ids(body, ids)
        with self.store.tx() as c:
            c.execute("INSERT OR REPLACE INTO cache_index VALUES (?,?,?,?)", (p.name, rhash, endpoint, now_iso()))
            c.execute("DELETE FROM cache_ids WHERE file=?", (p.name,))
            c.executemany("INSERT OR IGNORE INTO cache_ids VALUES (?,?)", [(p.name, i) for i in sorted(ids)])
        return p

    def purge_older_than(self, days: int) -> int:
        """Delete cache files older than ``days`` (DR-10). Returns the count of files removed."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self.store.q("SELECT file FROM cache_index WHERE fetched_at < ?", (cutoff,))
        n = 0
        for r in rows:
            (self.dir / r["file"]).unlink(missing_ok=True)
            with self.store.tx() as c:
                c.execute("DELETE FROM cache_index WHERE file=?", (r["file"],))
                c.execute("DELETE FROM cache_ids WHERE file=?", (r["file"],))
            n += 1
        return n

    def files_with(self, ids: set[str]) -> list[str]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [r["file"] for r in self.store.q(f"SELECT DISTINCT file FROM cache_ids WHERE id IN ({marks})", sorted(ids))]

    def scrub(self, ids: set[str]) -> int:
        """Rewrite each cache file that holds one of ``ids`` without the matching objects."""
        n = 0
        for name in self.files_with(ids):
            p = self.dir / name
            if not p.exists():
                with self.store.tx() as c:
                    c.execute("DELETE FROM cache_ids WHERE file=?", (name,))
                continue
            with gzip.open(p, "rt", encoding="utf-8") as f:
                body = json.load(f)
            cleaned = scrub_obj(body, ids)
            if cleaned is _DROP:
                cleaned = {}
            tmp = p.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False)
            tmp.replace(p)
            left: set[str] = set()
            _collect_ids(cleaned, left)
            with self.store.tx() as c:
                c.execute("DELETE FROM cache_ids WHERE file=?", (name,))
                c.executemany("INSERT OR IGNORE INTO cache_ids VALUES (?,?)", [(name, i) for i in sorted(left)])
            n += 1
        return n


def _id_pattern(ids: set[str]) -> re.Pattern[str]:
    alts = "|".join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
    return re.compile(rf"(?<![0-9A-Za-z_])(?:{alts})(?![0-9A-Za-z_])")


def scrub_obj(obj: Any, ids: set[str], _pat: re.Pattern[str] | None = None) -> Any:
    """Remove objects that carry a deleted ID in an ID key. Redact the ID in other strings.

    A dict whose ``id`` or ``author_id`` is a deleted ID is dropped from its
    parent. Another key that holds a deleted ID (for example
    ``in_reply_to_user_id``) is removed from its object, and the object stays. Other
    strings that contain a deleted ID (for example a status URL) get the ID
    replaced with ``[deleted]``.
    """
    pat = _pat or _id_pattern(ids)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in DROP_KEYS and isinstance(v, str) and v in ids:
                return _DROP
        out: dict[str, Any] = {}
        for k, v in obj.items():
            nv = scrub_obj(v, ids, pat)
            if nv is _DROP:
                continue
            out[k] = nv
        return out
    if isinstance(obj, list):
        return [nv for nv in (scrub_obj(v, ids, pat) for v in obj) if nv is not _DROP]
    if isinstance(obj, str):
        if obj in ids:
            return _DROP
        return pat.sub("[deleted]", obj)
    return obj


def contains_id(obj: Any, ident: str) -> bool:
    """True if ``ident`` occurs as a token anywhere in ``obj``."""
    pat = _id_pattern({ident})
    return bool(pat.search(json.dumps(obj, ensure_ascii=False)))
