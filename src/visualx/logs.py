"""JSON-lines run log with HMAC IDs (OR-01, IR-06).

The log holds no names, bios, post text, or tokens. IDs go in only as
HMAC-SHA256 values with a local key, so deletion does not have to touch logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from .store import now_iso

FORBIDDEN_KEYS = frozenset(
    {"name", "description", "bio", "text", "handle", "username", "access_token", "refresh_token",
     "token", "authorization", "code", "code_verifier", "client_secret", "password"}
)
# Keys that hold a raw X ID. The logger hashes these values.
ID_KEYS = frozenset({"user_id", "post_id", "id", "author_id", "target_user_id", "source_user_id", "ids"})


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
        if k in ID_KEYS:
            if isinstance(v, (list, tuple, set)):
                return [self.hid(str(x)) for x in v]
            return self.hid(str(v))
        if isinstance(v, dict):
            return {kk: self._clean(kk, vv) for kk, vv in v.items()}
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
