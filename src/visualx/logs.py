"""JSON-lines run log with HMAC IDs (OR-01, IR-06).

The log holds no names, bios, post text, or tokens. IDs go in only as
HMAC-SHA256 values with a local key, so deletion does not have to touch logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from .store import now_iso

FORBIDDEN_KEYS = frozenset(
    {"name", "description", "bio", "text", "handle", "username", "access_token", "refresh_token",
     "token", "authorization", "code", "code_verifier", "client_secret", "password"}
)
# Keys that hold a raw X ID. The logger hashes these values.
# since_id and until_id hold post IDs of other users (steps 2 and 6). The
# pagination tokens are opaque, but X can derive them from post IDs, so they are
# hashed too (review 20261103, A-05).
ID_KEYS = frozenset({"user_id", "post_id", "id", "author_id", "target_user_id", "source_user_id", "ids",
                     "since_id", "until_id", "pagination_token", "next_token"})
# Keys that end in "_id" but hold IDs that the tool made, not X IDs. Each other key
# that ends in "_id" or "_ids" is hashed too (review 20261103, B-27).
TOOL_ID_KEYS = frozenset({"run_id", "stage_run_id", "render_id", "snapshot_id", "export_id", "batch_id",
                          "sample_id", "finding_id", "rule_id", "lid"})
_LONG_NUMBER = re.compile(r"[0-9]{8,}")
# Keys whose long number is a time, not an ID (the rate-limit reset in epoch seconds).
NUMBER_KEYS = frozenset({"reset"})


def load_hmac_key(data_dir: str | Path) -> bytes:
    p = Path(data_dir) / "log_hmac.key"
    if p.exists():
        return bytes.fromhex(p.read_text(encoding="ascii").strip())
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex())
    return key


class RunLog:
    def __init__(self, path: str | Path, key: bytes):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = key

    def hid(self, ident: str) -> str:
        return hmac.new(self.key, ident.encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def _clean(self, k: str, v: Any) -> Any:
        if k.lower() in FORBIDDEN_KEYS:
            raise ValueError(f"log field {k!r} is not allowed (OR-01, SR-01)")
        if k in ID_KEYS or (k not in TOOL_ID_KEYS and (k.endswith("_id") or k.endswith("_ids"))):
            if isinstance(v, (list, tuple, set)):
                return [self.hid(str(x)) for x in v]
            return self.hid(str(v))
        if isinstance(v, dict):
            return {kk: self._clean(kk, vv) for kk, vv in v.items()}
        if k in NUMBER_KEYS:
            return v
        if isinstance(v, (list, tuple)):
            return [self._clean_value(x) for x in v]
        return self._clean_value(v)

    def _clean_value(self, v: Any) -> Any:
        """A string of 8 or more digits can be an X ID under a key that is not in the list (B-27)."""
        if isinstance(v, str) and _LONG_NUMBER.fullmatch(v):
            return self.hid(v)
        return v

    def write(self, event: str, **fields: Any) -> None:
        rec = {"at": now_iso(), "event": event}
        for k, v in fields.items():
            rec[k] = self._clean(k, v)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


class NullLog(RunLog):
    """A log that checks fields but writes nothing (tests)."""

    def __init__(self) -> None:
        self.key = b"null"
        self.records: list[dict[str, Any]] = []

    def write(self, event: str, **fields: Any) -> None:
        rec = {"event": event}
        for k, v in fields.items():
            rec[k] = self._clean(k, v)
        self.records.append(rec)
