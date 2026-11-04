"""SQLite store (ARCHITECTURE AD-01, AD-03).

All stage inputs and outputs live in one ``store.sqlite``. The interaction
table has no foreign key to the profile table (FR-22). Each stage output has a
``stage_runs`` record. The CLI holds a lock file so that one subcommand runs
at a time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .model import SCHEMA_VERSION, Interaction, Profile, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS interactions (
  interaction_id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  source_user_id TEXT NOT NULL,
  target_user_id TEXT NOT NULL,
  source_post_id TEXT,
  target_post_id TEXT,
  interaction_created_at TEXT,
  observed_at TEXT NOT NULL,
  conversation_id TEXT,
  text TEXT,
  text_hash TEXT,
  urls_json TEXT,
  source_endpoint TEXT NOT NULL,
  raw_request_hash TEXT,
  adapter TEXT NOT NULL,
  adapter_version TEXT NOT NULL,
  run_id TEXT NOT NULL,
  field_status_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_int_src ON interactions(source_user_id);
CREATE INDEX IF NOT EXISTS ix_int_tgt ON interactions(target_user_id);
CREATE INDEX IF NOT EXISTS ix_int_spost ON interactions(source_post_id);
CREATE INDEX IF NOT EXISTS ix_int_tpost ON interactions(target_post_id);

CREATE TABLE IF NOT EXISTS profiles (
  user_id TEXT PRIMARY KEY,
  handle TEXT, name TEXT, description TEXT, url TEXT, location TEXT,
  created_at TEXT,
  followers_count INTEGER, following_count INTEGER, tweet_count INTEGER, listed_count INTEGER,
  verified_type TEXT, protected INTEGER, profile_image_url TEXT,
  fetched_at TEXT NOT NULL,
  source_endpoint TEXT NOT NULL, adapter TEXT NOT NULL, adapter_version TEXT NOT NULL,
  field_status_json TEXT NOT NULL
);

-- One row each time the handle, display name, URL, or profile image of an
-- account differs from its last row (detector profile_change). Only X API
-- profiles: a Grok profile has no reliable handle history.
CREATE TABLE IF NOT EXISTS profile_history (
  user_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
  handle TEXT, name TEXT, url TEXT, profile_image_url TEXT,
  PRIMARY KEY (user_id, fetched_at)
);

-- Calibration (FR-38, score/calibrate.py): the sampled accounts with a snapshot
-- of their state, rules, and detectors, and the owner's labels.
CREATE TABLE IF NOT EXISTS calibration_samples (
  sample_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, user_id TEXT NOT NULL,
  stratum TEXT NOT NULL, state TEXT NOT NULL, rules_json TEXT NOT NULL, detectors_json TEXT NOT NULL,
  stratum_size INTEGER NOT NULL, score_stage_run_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cal_user ON calibration_samples(user_id);
CREATE TABLE IF NOT EXISTS calibration_labels (
  sample_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL, note TEXT, labeled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_errors (
  user_id TEXT PRIMARY KEY, error TEXT NOT NULL, at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
  post_id TEXT PRIMARY KEY,
  author_id TEXT NOT NULL,
  created_at TEXT,
  conversation_id TEXT,
  text TEXT,
  public_metrics_json TEXT,
  referenced_json TEXT,
  source_endpoint TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posts_author ON posts(author_id);

CREATE TABLE IF NOT EXISTS cache_index (
  file TEXT PRIMARY KEY, request_hash TEXT NOT NULL, endpoint TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_ids (
  file TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (file, id)
);
CREATE INDEX IF NOT EXISTS ix_cache_ids_id ON cache_ids(id);

CREATE TABLE IF NOT EXISTS cost_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL, step INTEGER NOT NULL, endpoint TEXT NOT NULL,
  billing_class TEXT NOT NULL, resources INTEGER NOT NULL,
  unit_price REAL NOT NULL, cost REAL NOT NULL, reserved REAL NOT NULL,
  response_code INTEGER, at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cursor (
  step INTEGER NOT NULL, item TEXT NOT NULL,
  next_token TEXT, done INTEGER NOT NULL DEFAULT 0,
  run_id TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (step, item)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  manifest_json TEXT
);

CREATE TABLE IF NOT EXISTS stage_runs (
  stage_run_id TEXT PRIMARY KEY,
  stage TEXT NOT NULL, run_id TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  code_version TEXT NOT NULL, inputs_json TEXT NOT NULL, config_hash TEXT,
  counts_json TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
  user_id TEXT PRIMARY KEY,
  handle TEXT, name TEXT,
  profile_status TEXT NOT NULL,
  is_ego INTEGER NOT NULL DEFAULT 0,
  in_degree INTEGER, out_degree INTEGER, degree INTEGER,
  weighted_degree REAL, ego_weight REAL,
  pagerank REAL, betweenness REAL, x REAL, y REAL,
  stage_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_groups (
  user_id TEXT PRIMARY KEY,
  community INTEGER, community_size INTEGER, stability REAL,
  is_hub INTEGER NOT NULL DEFAULT 0, is_bridge INTEGER NOT NULL DEFAULT 0,
  bridge_reason TEXT, bridge_communities INTEGER, topics_json TEXT,
  stage_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, run_id TEXT,
  analyze_stage TEXT NOT NULL, score_stage TEXT NOT NULL, counts_json TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_nodes (
  snapshot_id TEXT NOT NULL, user_id TEXT NOT NULL, state TEXT NOT NULL, total_points REAL,
  community INTEGER, weighted_degree REAL, ego_weight REAL, rules_json TEXT,
  PRIMARY KEY (snapshot_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_sn_user ON snapshot_nodes(user_id);
CREATE TABLE IF NOT EXISTS snapshot_findings (
  snapshot_id TEXT NOT NULL, detector TEXT NOT NULL, severity TEXT, members_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exports (
  export_id TEXT PRIMARY KEY, path TEXT NOT NULL, format TEXT NOT NULL, created_at TEXT NOT NULL,
  stage_run_id TEXT
);
CREATE TABLE IF NOT EXISTS export_members (
  export_id TEXT NOT NULL, user_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_em_user ON export_members(user_id);

CREATE TABLE IF NOT EXISTS edges (
  source TEXT NOT NULL, target TEXT NOT NULL,
  reply_count INTEGER NOT NULL DEFAULT 0, quote_count INTEGER NOT NULL DEFAULT 0,
  mention_count INTEGER NOT NULL DEFAULT 0, repost_count INTEGER NOT NULL DEFAULT 0,
  timestamps_json TEXT NOT NULL,
  weight REAL NOT NULL, pair_weight REAL NOT NULL,
  stage_run_id TEXT NOT NULL,
  PRIMARY KEY (source, target)
);
CREATE INDEX IF NOT EXISTS ix_edges_tgt ON edges(target);

CREATE TABLE IF NOT EXISTS rule_results (
  user_id TEXT NOT NULL, rule_id TEXT NOT NULL, family TEXT NOT NULL,
  points REAL NOT NULL, value_json TEXT, cause TEXT NOT NULL, hard INTEGER NOT NULL DEFAULT 0,
  stage_run_id TEXT NOT NULL,
  PRIMARY KEY (user_id, rule_id)
);
CREATE TABLE IF NOT EXISTS rule_skips (
  user_id TEXT NOT NULL, rule_id TEXT NOT NULL, missing_field TEXT NOT NULL,
  stage_run_id TEXT NOT NULL,
  PRIMARY KEY (user_id, rule_id, missing_field)
);
CREATE TABLE IF NOT EXISTS node_states (
  user_id TEXT PRIMARY KEY, state TEXT NOT NULL, total_points REAL NOT NULL,
  override_note TEXT, stage_run_id TEXT NOT NULL
);

-- Network-level pattern findings (score stage). A finding names two or more accounts or
-- one pattern; it describes signals in the data, not persons. Only render reads it.
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  detector TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL,
  summary TEXT NOT NULL,
  members_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  stage_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overrides (
  user_id TEXT PRIMARY KEY, state TEXT NOT NULL, note TEXT, date TEXT
);

CREATE TABLE IF NOT EXISTS renders (
  render_id TEXT PRIMARY KEY, path TEXT NOT NULL, run_id TEXT, created_at TEXT NOT NULL,
  stage_run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_members (
  render_id TEXT NOT NULL, user_id TEXT, post_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_rm_user ON render_members(user_id);
CREATE INDEX IF NOT EXISTS ix_rm_post ON render_members(post_id);
"""

# Stage order. A stage reads the newest record of each upstream stage.
STAGE_UPSTREAM: dict[str, tuple[str, ...]] = {
    "collect": (),
    "build": ("collect",),
    "analyze": ("build",),
    "score": ("build",),
    "render": ("analyze", "score"),
}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"


class StaleError(RuntimeError):
    """An upstream stage is newer than this stage's inputs, or is missing."""


class LockError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cur = self.get_meta("schema_version")
        if cur is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(cur) > SCHEMA_VERSION:
            raise RuntimeError(f"store schema {cur} is newer than this code ({SCHEMA_VERSION})")

    def close(self) -> None:
        self.conn.close()

    # -- transactions ---------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def q1(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    # -- meta -----------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def ego_id(self) -> str | None:
        return self.get_meta("ego_user_id")

    # -- records --------------------------------------------------------
    def put_interactions(self, items: Iterable[Interaction], conn: sqlite3.Connection | None = None) -> int:
        """Insert interactions. A duplicate interaction_id is ignored. Returns rows added."""
        c = conn or self.conn
        n = 0
        for it in items:
            cur = c.execute(
                """INSERT OR IGNORE INTO interactions VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    it.interaction_id, it.type, it.source_user_id, it.target_user_id,
                    it.source_post_id, it.target_post_id, it.interaction_created_at, it.observed_at,
                    it.conversation_id, it.text, it.text_hash,
                    None if it.urls is None else dumps(it.urls),
                    it.source_endpoint, it.raw_request_hash, it.adapter, it.adapter_version,
                    it.run_id, dumps(it.field_status),
                ),
            )
            n += cur.rowcount
        return n

    def put_profiles(self, items: Iterable[Profile], conn: sqlite3.Connection | None = None) -> int:
        """Insert or replace profiles (the newest fetch wins), and record name changes in ``profile_history``."""
        c = conn or self.conn
        n = 0
        for p in items:
            self._record_history(c, p)
            c.execute(
                """INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p.user_id, p.handle, p.name, p.description, p.url, p.location, p.created_at,
                    p.followers_count, p.following_count, p.tweet_count, p.listed_count,
                    p.verified_type, None if p.protected is None else int(p.protected),
                    p.profile_image_url, p.fetched_at, p.source_endpoint, p.adapter,
                    p.adapter_version, dumps(p.field_status),
                ),
            )
            c.execute("DELETE FROM profile_errors WHERE user_id=?", (p.user_id,))
            n += 1
        return n

    HISTORY_FIELDS: tuple[str, ...] = ("handle", "name", "url", "profile_image_url")

    def _record_history(self, c: sqlite3.Connection, p: Profile) -> None:
        """Add a ``profile_history`` row if the tracked fields differ from the last row of the account.

        Only X API profiles count, and only fields with the status ``present``
        (DR-02: an absent field is not a change). If the account has no history
        row but has a stored profile, that stored profile becomes the first row,
        so the first change after an upgrade is not lost.
        """
        if p.adapter != "xapi":
            return
        vals = {f: (getattr(p, f) if p.field_status.get(f) == "present" else None) for f in self.HISTORY_FIELDS}
        if vals["handle"] is None and vals["name"] is None:
            return
        last = c.execute(
            "SELECT fetched_at, handle, name, url, profile_image_url FROM profile_history "
            "WHERE user_id=? ORDER BY fetched_at DESC LIMIT 1", (p.user_id,)).fetchone()
        if last is None:
            old = c.execute(
                "SELECT fetched_at, handle, name, url, profile_image_url, adapter, field_status_json "
                "FROM profiles WHERE user_id=?", (p.user_id,)).fetchone()
            if old is not None and old[5] == "xapi" and old[0] < p.fetched_at:
                st = json.loads(old[6])
                ov = [old[1 + k] if st.get(f) == "present" else None for k, f in enumerate(self.HISTORY_FIELDS)]
                c.execute("INSERT OR IGNORE INTO profile_history VALUES (?,?,?,?,?,?)", (p.user_id, old[0], *ov))
                last = (old[0], *ov)
        if last is not None:
            if p.fetched_at <= last[0]:
                return
            prev = dict(zip(self.HISTORY_FIELDS, last[1:]))
            # A field that the new fetch does not give keeps its last value.
            if all(vals[f] is None or vals[f] == prev[f] for f in self.HISTORY_FIELDS):
                return
            vals = {f: (vals[f] if vals[f] is not None else prev[f]) for f in self.HISTORY_FIELDS}
        c.execute("INSERT OR IGNORE INTO profile_history VALUES (?,?,?,?,?,?)",
                  (p.user_id, p.fetched_at, *(vals[f] for f in self.HISTORY_FIELDS)))

    def profile_history(self) -> dict[str, list[dict[str, Any]]]:
        """``profile_history`` rows of each account, oldest first."""
        out: dict[str, list[dict[str, Any]]] = {}
        for r in self.q("SELECT * FROM profile_history ORDER BY user_id, fetched_at"):
            out.setdefault(r["user_id"], []).append(dict(r))
        return out

    def put_profile_error(self, user_id: str, error: str, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        c.execute("INSERT OR REPLACE INTO profile_errors VALUES (?,?,?)", (user_id, error, now_iso()))

    def put_post(self, post: dict[str, Any], source_endpoint: str, run_id: str, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        c.execute(
            "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                post["id"], post.get("author_id", ""), post.get("created_at"), post.get("conversation_id"),
                post.get("text"), dumps(post.get("public_metrics")) if post.get("public_metrics") else None,
                dumps(post.get("referenced_tweets")) if post.get("referenced_tweets") else None,
                source_endpoint, now_iso(), run_id,
            ),
        )

    def interactions(self) -> list[dict[str, Any]]:
        out = []
        for r in self.q("SELECT * FROM interactions"):
            d = dict(r)
            d["urls"] = json.loads(d.pop("urls_json")) if d.get("urls_json") else None
            d["field_status"] = json.loads(d.pop("field_status_json"))
            out.append(d)
        return out

    def profiles(self) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self.q("SELECT * FROM profiles"):
            d = dict(r)
            d["field_status"] = json.loads(d.pop("field_status_json"))
            if d["protected"] is not None:
                d["protected"] = bool(d["protected"])
            out[d["user_id"]] = d
        return out

    def profile_errors(self) -> dict[str, str]:
        return {r["user_id"]: r["error"] for r in self.q("SELECT user_id, error FROM profile_errors")}

    # -- cursor ---------------------------------------------------------
    def get_cursor(self, step: int, item: str) -> tuple[str | None, bool]:
        row = self.q1("SELECT next_token, done FROM cursor WHERE step=? AND item=?", (step, item))
        if row is None:
            return None, False
        return row["next_token"], bool(row["done"])

    def set_cursor(self, step: int, item: str, next_token: str | None, done: bool, run_id: str, conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        c.execute(
            """INSERT INTO cursor(step,item,next_token,done,run_id,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(step,item) DO UPDATE SET next_token=excluded.next_token, done=excluded.done,
            run_id=excluded.run_id, updated_at=excluded.updated_at""",
            (step, item, next_token, int(done), run_id, now_iso()),
        )

    def reset_cursor(self) -> None:
        self.conn.execute("DELETE FROM cursor")

    # -- stage runs -----------------------------------------------------
    def latest_stage_run(self, stage: str) -> sqlite3.Row | None:
        return self.q1(
            "SELECT * FROM stage_runs WHERE stage=? AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1",
            (stage,),
        )

    def _check_lineage(self, stage: str, row: sqlite3.Row) -> None:
        """Raise StaleError if ``row`` (the newest output of ``stage``) was made from an
        upstream output that is not the newest one, at any depth (AD-01)."""
        recorded = json.loads(row["inputs_json"] or "{}")
        for up in STAGE_UPSTREAM.get(stage, ()):
            newest = self.latest_stage_run(up)
            if newest is None:
                continue
            if up in recorded and recorded[up] != newest["stage_run_id"]:
                raise StaleError(
                    f"The '{stage}' output is older than the newest '{up}' output. Run 'visualx {stage}' first "
                    f"(or 'visualx run --skip-collect')."
                )
            self._check_lineage(up, newest)

    def check_fresh(self, stage: str) -> dict[str, str]:
        """Return the upstream stage_run IDs for ``stage``.

        Raise StaleError if an upstream output is missing, if an upstream output was made
        from an older output than the newest one at any depth, or if two upstream outputs
        come from different runs of a shared stage (for example, render with an analyze
        output and a score output from different builds). Review 20261102-225212.
        """
        inputs: dict[str, str] = {}
        ancestors: dict[str, tuple[str, str]] = {}
        for up in STAGE_UPSTREAM.get(stage, ()):
            row = self.latest_stage_run(up)
            if row is None:
                raise StaleError(f"'{stage}' needs '{up}' output. Run 'visualx {up}' first.")
            self._check_lineage(up, row)
            inputs[up] = row["stage_run_id"]
            for anc, aid in json.loads(row["inputs_json"] or "{}").items():
                if anc in ancestors and ancestors[anc][1] != aid:
                    other = ancestors[anc][0]
                    raise StaleError(
                        f"The '{other}' and '{up}' outputs come from different '{anc}' runs. "
                        f"Run 'visualx run --skip-collect' to make them agree."
                    )
                ancestors[anc] = (up, aid)
        return inputs

    def stale_downstream(self, stage: str) -> list[str]:
        """Names of upstream stages that are newer than the newest run of ``stage``."""
        mine = self.latest_stage_run(stage)
        if mine is None:
            return []
        inputs = json.loads(mine["inputs_json"])
        out = []
        for up in STAGE_UPSTREAM.get(stage, ()):
            row = self.latest_stage_run(up)
            if row is not None and inputs.get(up) != row["stage_run_id"]:
                out.append(up)
        return out

    def begin_stage(self, stage: str, run_id: str | None, inputs: dict[str, str], config_hash: str | None = None) -> str:
        sid = new_id(stage)
        self.conn.execute(
            "INSERT INTO stage_runs(stage_run_id,stage,run_id,started_at,code_version,inputs_json,config_hash) VALUES(?,?,?,?,?,?,?)",
            (sid, stage, run_id, now_iso(), __version__, dumps(inputs), config_hash),
        )
        return sid

    def finish_stage(self, stage_run_id: str, counts: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
        c = conn or self.conn
        c.execute(
            "UPDATE stage_runs SET finished_at=?, counts_json=? WHERE stage_run_id=?",
            (now_iso(), dumps(counts), stage_run_id),
        )

    # -- provisional IDs from the Grok source ----------------------------
    def rekey_user(self, old: str, new: str) -> int:
        """Replace a provisional user ID (``h:<handle>``) with the X user ID in all tables.

        Interaction IDs hash the user IDs, so they are calculated again. When the same post
        is already stored under the new ID (seen by both sources), the existing row stays and
        the provisional row goes. Returns the count of interactions changed.
        """
        import hashlib

        if old == new:
            return 0
        n = 0
        with self.tx() as c:
            rows = c.execute(
                "SELECT * FROM interactions WHERE source_user_id=? OR target_user_id=?", (old, old)
            ).fetchall()
            for r in rows:
                d = dict(r)
                src = new if d["source_user_id"] == old else d["source_user_id"]
                tgt = new if d["target_user_id"] == old else d["target_user_id"]
                post = d["source_post_id"] or d["target_post_id"] or ""
                iid = hashlib.sha256(f"{d['type']}|{src}|{tgt}|{post}".encode()).hexdigest()[:32]
                c.execute("DELETE FROM interactions WHERE interaction_id=?", (d["interaction_id"],))
                if c.execute("SELECT 1 FROM interactions WHERE interaction_id=?", (iid,)).fetchone():
                    continue
                d.update(interaction_id=iid, source_user_id=src, target_user_id=tgt)
                c.execute(f"INSERT INTO interactions({','.join(d)}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))
                n += 1
            # A profile from the X API (new ID) is better than the handle-only Grok profile.
            if c.execute("SELECT 1 FROM profiles WHERE user_id=?", (new,)).fetchone():
                c.execute("DELETE FROM profiles WHERE user_id=?", (old,))
            else:
                c.execute("UPDATE profiles SET user_id=? WHERE user_id=?", (new, old))
            for t, col in (("nodes", "user_id"), ("rule_results", "user_id"), ("rule_skips", "user_id"),
                           ("node_states", "user_id"), ("render_members", "user_id"), ("overrides", "user_id")):
                c.execute(f"DELETE FROM {t} WHERE {col}=?", (old,))
            c.execute("DELETE FROM edges WHERE source=? OR target=?", (old, old))
            c.execute("DELETE FROM node_groups WHERE user_id=?", (old,))  # the next analyze makes it again
            # Tables that keep a user over time must follow the new ID, so that a later
            # delete by the X user ID finds them (review 20261103, A-14).
            for t in ("snapshot_nodes", "profile_history"):
                c.execute(f"UPDATE OR IGNORE {t} SET user_id=? WHERE user_id=?", (new, old))
                c.execute(f"DELETE FROM {t} WHERE user_id=?", (old,))
            for t in ("calibration_samples", "calibration_labels", "export_members"):
                c.execute(f"UPDATE {t} SET user_id=? WHERE user_id=?", (new, old))
            quoted_old, quoted_new = json.dumps(old), json.dumps(new)
            for t, cols in (("findings", ("members_json", "evidence_json")), ("snapshot_findings", ("members_json",))):
                for col in cols:
                    c.execute(f"UPDATE {t} SET {col}=REPLACE({col}, ?, ?) WHERE instr({col}, ?) > 0",
                              (quoted_old, quoted_new, quoted_old))
            for k in ("ego_user_id",):
                if self.get_meta(k) == old:
                    c.execute("UPDATE meta SET value=? WHERE key=?", (new, k))
        return n

    def merge_provisional(self) -> int:
        """Re-key each ``h:<handle>`` user whose handle now has an X profile. Returns users merged."""
        by_handle = {
            (r["handle"] or "").casefold(): r["user_id"]
            for r in self.q("SELECT user_id, handle FROM profiles WHERE user_id NOT LIKE 'h:%' AND handle IS NOT NULL")
        }
        prov = {r["u"] for r in self.q(
            "SELECT source_user_id AS u FROM interactions WHERE source_user_id LIKE 'h:%' "
            "UNION SELECT target_user_id FROM interactions WHERE target_user_id LIKE 'h:%'")}
        merged = 0
        for old in sorted(prov):
            new = by_handle.get(old[2:])
            if new:
                self.rekey_user(old, new)
                merged += 1
        return merged

    # -- text retention (AD-11, DR-09) -----------------------------------
    def drop_text(self) -> int:
        """Remove post text and keep only text_hash and the URL domains. Returns rows changed.

        The rules that need text (B01, B04, B07) give no result on the next score
        for these rows, because field_status marks the text as ``absent``.

        ``urls`` then holds host names only, and ``field_status["url_paths"]`` is
        ``absent``. The domain rules (B05, B06, the same-site finding) still work.
        The same-link test (B02) does not use these rows, because a host is not a
        link. A second run keeps the hosts as they are (review 20261103, A-11).
        """
        from urllib.parse import urlsplit

        def host(u: str) -> str:
            try:
                return urlsplit(u if "://" in u else "http://" + u).hostname or ""
            except ValueError:
                return ""

        n = 0
        with self.tx() as c:
            rows = c.execute(
                "SELECT interaction_id, text, urls_json, field_status_json FROM interactions "
                "WHERE text IS NOT NULL OR urls_json IS NOT NULL"
            ).fetchall()
            for r in rows:
                fs = json.loads(r["field_status_json"])
                if r["text"] is None and fs.get("url_paths") == "absent":
                    continue  # done on an earlier run
                urls = json.loads(r["urls_json"]) if r["urls_json"] else None
                domains = sorted({host(str(u)) for u in urls} - {""}) if urls else None
                fs["text"] = "absent"
                fs["urls"] = "present" if domains else "absent"
                fs["url_paths"] = "absent"
                c.execute(
                    "UPDATE interactions SET text=NULL, urls_json=?, field_status_json=? WHERE interaction_id=?",
                    (dumps(domains) if domains else None, dumps(fs), r["interaction_id"]),
                )
                n += 1
            c.execute("UPDATE posts SET text=NULL")
        return n

    # -- ledger ---------------------------------------------------------
    def ledger_total(self, run_id: str) -> float:
        row = self.q1("SELECT COALESCE(SUM(cost),0) AS c FROM cost_ledger WHERE run_id=?", (run_id,))
        return float(row["c"]) if row else 0.0


@contextmanager
def lock(data_dir: str | Path) -> Iterator[Path]:
    """One subcommand at a time (AD-01). Stale locks name the PID that held them."""
    p = Path(data_dir) / "visualx.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as e:
        holder = p.read_text(encoding="utf-8", errors="replace").strip()
        raise LockError(
            f"Another visualx command holds {p} ({holder}). If no other command runs, delete the file."
        ) from e
    try:
        os.write(fd, f"pid={os.getpid()} at={now_iso()}".encode())
        os.close(fd)
        yield p
    finally:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
