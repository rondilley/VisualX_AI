"""X data archive importer (FR-06, IR-04, TSC-02). A free source of the owner's outbound events.

Input: the archive ``.zip`` file that X gives to the owner, or the extracted
folder. The archive holds ``data/<name>.js`` files. Each file is one JSON array
behind a JavaScript assignment, for example ``window.YTD.tweets.part0 = [...]``.
The importer removes the text up to the first ``=`` and reads the remainder
with ``json.loads``. It never runs the JavaScript. It reads the zip members in
memory, so nothing is extracted to disk, and it refuses an archive with an
absolute or ``..`` member name.

What it reads: ``data/account.js`` (the account ID, for the owner check of
FR-54) and the owner's posts in ``data/tweets.js`` / ``data/tweet.js`` and
their ``-partN`` files. What it does not read: likes, follows, direct
messages, and all other personal files. Those files are recognized and
reported as "not used". A file that is not on the list of known archive files
is reported by name (IR-04).

Mapping (FR-21, the same rules as ``xapi.map_post``):

- A post with text that starts with ``RT @handle`` is a repost to the
  ``user_mentions`` entry with that handle, and nothing else. Without a
  numeric ID for that handle, the post gives no event (unmapped).
- ``in_reply_to_user_id_str`` that is not the owner gives a reply.
- A quote: the archive post has no quoted user ID, only the URL of the quoted
  post (``https://twitter.com/<handle>/status/<id>``). The handle resolves to
  a numeric ID from the ``user_mentions`` of the same post or from an X API
  profile in the store. Otherwise the target is the provisional user ID
  ``h:<handle>``, the same mechanism as the Grok source: the build stage
  merges it into the X user ID when an X API profile with that handle is in
  the store (``Store.merge_provisional``).
- Each other ``user_mentions`` entry gives a mention.
- No self-edges.

Each event links to its archive record (TSC-02): ``source_endpoint`` names the
archive file, ``source_post_id`` is the post ID, and ``raw_request_hash`` is
the SHA-256 of the canonical JSON of the archive record. The interaction ID
uses only type, source, target, and post, so an archive event and the same
event from the X API have the same ID and the store keeps one row.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..logs import RunLog
from ..model import INTERACTION_OPTIONAL_FIELDS, PROFILE_OPTIONAL_FIELDS, Interaction, Profile, dumps, status_for
from ..store import Store, now_iso

ADAPTER = "archive"
ADAPTER_VERSION = "1"
MAX_FILE_BYTES = 1 << 30  # one archive file larger than 1 GiB is refused
ARCHIVE_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"  # "Wed Oct 10 20:19:24 +0000 2018"
STORE_TIME_FMT = "%Y-%m-%dT%H:%M:%S.000Z"  # the X API v2 created_at form that the store holds

PREFIX_RE = re.compile(r"^\s*window\.YTD\.[A-Za-z0-9_]+\.part\d+\s*$")
DATA_JS_RE = re.compile(r"^data/([A-Za-z0-9_.\-]+)\.js$")
PART_RE = re.compile(r"-part\d+$")
STATUS_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?P<handle>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<id>\d{1,25})(?:[/?#].*)?$",
    re.IGNORECASE,
)
RT_RE = re.compile(r"^RT @(?P<handle>[A-Za-z0-9_]{1,15})")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

TWEET_FILES = frozenset({"tweets", "tweet"})

# Files of the X archive (2018 to 2026 layouts) that the importer knows and does not read.
# They are out of scope (FR-06 is about outbound posts) or private (direct messages,
# likes, follows, contacts, devices, IP addresses). The "-partN" forms are known too.
KNOWN_UNUSED = frozenset({
    "account-creation-ip", "account-label", "account-suspension", "account-timezone",
    "ad-engagements", "ad-impressions", "ad-free-article-visits",
    "ad-mobile-conversions-attributed", "ad-mobile-conversions-unattributed",
    "ad-online-conversions-attributed", "ad-online-conversions-unattributed",
    "ads-revenue-sharing", "ageinfo", "app", "article", "article-metadata",
    "audio-video-calls-in-dm", "audio-video-calls-in-dm-recipient-sessions",
    "block", "branch-links", "catalog-item", "commerce-catalog",
    "community-note", "community-note-rating", "community-note-tombstone", "community-tweet",
    "connected-application", "contact", "deleted-note-tweet", "deleted-tweet-headers", "deleted-tweets",
    "device-token", "direct-message-group-headers", "direct-message-headers", "direct-message-mute",
    "direct-messages", "direct-messages-group", "email-address-change", "follower", "following",
    "grok-chat-item", "ip-audit", "key-registry", "like", "lists-created", "lists-member",
    "lists-subscribed", "manifest", "moment", "mute", "ni-devices", "note-tweet",
    "periscope-account-information", "periscope-ban-information", "periscope-broadcast-metadata",
    "periscope-comments-made-by-user", "periscope-expired-broadcasts", "periscope-followers",
    "periscope-profile-description", "personalization", "phone-number", "product-drop", "product-set",
    "professional-data", "profile", "protected-history", "reply-prompt", "saved-search",
    "screen-name-change", "shop-module", "shopify-account", "smartblock", "spaces-metadata", "sso",
    "tweet-headers", "tweetdeck", "twitter-article", "twitter-article-metadata", "twitter-circle",
    "twitter-circle-member", "twitter-circle-tweet", "twitter-shop", "user-link-clicks", "verified",
    "verified-organization",
})


class ArchiveError(RuntimeError):
    """The archive cannot be imported. Nothing was written to the store."""


def _safe_name(name: str) -> str:
    """A file name for the terminal: control and non-ASCII characters become '?'."""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in name)


# ---------------------------------------------------------------- archive access
class _Source:
    """The files of an archive as (relative path, size), with a reader for one file."""

    def names(self) -> list[tuple[str, int]]:
        raise ArchiveError("no archive source")

    def read(self, rel: str) -> bytes:
        raise ArchiveError(f"cannot read {rel}")

    def close(self) -> None:
        return None


class _ZipSource(_Source):
    def __init__(self, path: Path):
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as e:
            raise ArchiveError(f"{path} is not a readable zip file ({type(e).__name__})") from e
        self.members: dict[str, zipfile.ZipInfo] = {}
        all_names: list[str] = []
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            raw = info.filename
            norm = raw.replace("\\", "/")
            parts = norm.split("/")
            if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in parts:
                self.zf.close()
                raise ArchiveError(f"the zip file has an unsafe member name ({_safe_name(raw)}); it was not read")
            all_names.append(norm)
            self.members[norm] = info
        roots = sorted((n[: -len("data/account.js")] for n in all_names if n == "data/account.js" or n.endswith("/data/account.js")),
                       key=lambda p: (p.count("/"), p))
        self.prefix = roots[0] if roots else ""

    def names(self) -> list[tuple[str, int]]:
        out = []
        for n, info in self.members.items():
            rel = n[len(self.prefix):] if n.startswith(self.prefix) else "../" + n
            out.append((rel, info.file_size))
        return sorted(out)

    def read(self, rel: str) -> bytes:
        info = self.members.get(self.prefix + rel)
        if info is None:
            raise ArchiveError(f"{_safe_name(rel)} is not in the archive")
        if info.file_size > MAX_FILE_BYTES:
            raise ArchiveError(f"{_safe_name(rel)} is larger than {MAX_FILE_BYTES} bytes; it was not read")
        try:
            with self.zf.open(info) as f:
                data = f.read(MAX_FILE_BYTES + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            raise ArchiveError(f"cannot read {_safe_name(rel)} from the zip file ({type(e).__name__})") from e
        if len(data) > MAX_FILE_BYTES:
            raise ArchiveError(f"{_safe_name(rel)} is larger than {MAX_FILE_BYTES} bytes; it was not read")
        return data

    def close(self) -> None:
        self.zf.close()


class _DirSource(_Source):
    def __init__(self, path: Path):
        p = path.resolve()
        # The archive root (with data/ in it), or the data folder itself.
        self.scan, self.lead = p, ""
        if not (p / "data" / "account.js").is_file() and p.name.lower() == "data" and (p / "account.js").is_file():
            self.lead = "data/"
        self.files: dict[str, Path] = {}
        self.outside: list[str] = []
        for f in sorted(self.scan.rglob("*")):
            rel = self.lead + f.relative_to(self.scan).as_posix()
            if f.is_symlink():
                self.outside.append(rel)  # a link can point out of the archive: not read
                continue
            if f.is_file():
                self.files[rel] = f

    def names(self) -> list[tuple[str, int]]:
        out = [(rel, f.stat().st_size) for rel, f in self.files.items()]
        out += [(rel, 0) for rel in self.outside]
        return sorted(out)

    def read(self, rel: str) -> bytes:
        f = self.files.get(rel)
        if f is None:
            raise ArchiveError(f"{_safe_name(rel)} is not in the archive folder")
        if not f.resolve().is_relative_to(self.scan):
            raise ArchiveError(f"{_safe_name(rel)} is outside the archive folder; it was not read")
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ArchiveError(f"{_safe_name(rel)} is larger than {MAX_FILE_BYTES} bytes; it was not read")
        try:
            return f.read_bytes()
        except OSError as e:
            raise ArchiveError(f"cannot read {_safe_name(rel)} ({type(e).__name__})") from e


def open_archive(path: str | Path) -> _Source:
    p = Path(path)
    if p.is_dir():
        return _DirSource(p)
    if p.is_file():
        return _ZipSource(p)
    raise ArchiveError(f"{p} is not a file or a folder")


# ---------------------------------------------------------------- parsing
def parse_js(data: bytes, rel: str) -> list[Any]:
    """The JSON array in one archive file. The JavaScript prefix is removed, never run."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ArchiveError(f"{_safe_name(rel)} is not UTF-8 text") from e
    head, sep, body = text.partition("=")
    if not sep or not PREFIX_RE.match(head):
        raise ArchiveError(f"{_safe_name(rel)} does not start with 'window.YTD.<name>.partN ='; it is not a known archive file form")
    try:
        value = json.loads(body)
    except ValueError as e:
        raise ArchiveError(f"{_safe_name(rel)} has JSON that cannot be read ({e.msg} at line {e.lineno})") from e
    if not isinstance(value, list):
        raise ArchiveError(f"{_safe_name(rel)} does not hold a JSON array")
    return value


def classify(rel: str) -> str:
    """One of: account, tweets, known (recognized, not used), media, viewer, unknown."""
    m = DATA_JS_RE.match(rel)
    if m:
        stem = m.group(1)
        if stem == "account":
            return "account"
        base = PART_RE.sub("", stem)
        if base in TWEET_FILES:
            return "tweets"
        if base in KNOWN_UNUSED or base == "account":
            return "known"
        return "unknown"
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "data" and re.search(r"[_-]media$", parts[1]):
        return "media"
    if rel == "Your archive.html" or parts[0] == "assets":
        return "viewer"
    return "unknown"


def archive_time(value: Any) -> str | None:
    """The archive ``created_at`` in the store form, or None if it cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), ARCHIVE_TIME_FMT).astimezone(UTC).strftime(STORE_TIME_FMT)
    except ValueError:
        return None


def record_hash(record: Any) -> str:
    """SHA-256 of the canonical JSON of one archive record (the link to the record, TSC-02)."""
    return hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()


def _numeric_id(v: Any) -> str | None:
    s = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else ""
    return s if s.isdigit() and s.strip("0") else None


def _urls(tweet: dict[str, Any]) -> list[str] | None:
    ent = tweet.get("entities")
    if not isinstance(ent, dict):
        return None
    out = []
    for u in ent.get("urls") or []:
        if isinstance(u, dict):
            v = u.get("expanded_url") or u.get("url")
            if isinstance(v, str) and v:
                out.append(v)
    return out


def _mentions(tweet: dict[str, Any]) -> list[tuple[str | None, str | None]]:
    """(numeric user ID or None, handle or None) for each user_mentions entry."""
    ent = tweet.get("entities")
    out = []
    for m in (ent.get("user_mentions") or []) if isinstance(ent, dict) else []:
        if not isinstance(m, dict):
            continue
        h = m.get("screen_name")
        out.append((_numeric_id(m.get("id_str", m.get("id"))), h if isinstance(h, str) and HANDLE_RE.match(h) else None))
    return out


def _quoted(tweet: dict[str, Any], urls: list[str] | None) -> tuple[str, str] | None:
    """(handle, status ID) of the quoted post, or None.

    With ``quoted_status_id_str`` in the record, only the URL with that status ID
    counts. Without it, the last status URL of the post counts: X shows the last
    post link of a post as the quoted post.
    """
    found = []
    for u in urls or []:
        m = STATUS_URL_RE.match(u)
        if m and m.group("handle").lower() not in ("i", "intent", "share", "home"):
            found.append((m.group("handle"), m.group("id")))
    qid = _numeric_id(tweet.get("quoted_status_id_str"))
    if qid:
        hit = [f for f in found if f[1] == qid]
        return hit[-1] if hit else None
    return found[-1] if found else None


# ---------------------------------------------------------------- report
@dataclass
class ArchiveReport:
    run_id: str
    archive: str
    dry_run: bool = False
    account_handle: str | None = None
    files_used: list[str] = field(default_factory=list)
    files_known_unused: list[str] = field(default_factory=list)
    files_unrecognized: list[str] = field(default_factory=list)
    media_files: int = 0
    viewer_files: int = 0
    window_start: str | None = None
    window_end: str | None = None
    window_days: int = 0
    records: int = 0
    posts_in_window: int = 0
    outside_window: int = 0
    mapped: dict[str, int] = field(default_factory=lambda: {"reply": 0, "quote": 0, "mention": 0, "repost": 0})
    unmapped: dict[str, int] = field(default_factory=lambda: {
        "not_a_post_record": 0, "no_post_id": 0, "no_readable_time": 0, "repost_target_unknown": 0,
        "reply_without_user_id": 0, "mention_without_user_id": 0, "quote_without_handle": 0,
    })
    posts_without_events: int = 0
    provisional_quote_targets: int = 0
    interactions_added: int = 0
    already_in_store: int = 0
    text_dropped: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def events(self) -> int:
        return sum(self.mapped.values())

    def summary_lines(self) -> list[str]:
        title = f"X archive import {self.run_id}" + (" (dry run, nothing written)" if self.dry_run else "")
        win = (f"{self.window_start} to {self.window_end} ({self.window_days} days)" if self.window_start
               else "all posts (no window)")
        lines = [
            title,
            f"  archive: {_safe_name(self.archive)}",
            f"  account: @{_safe_name(self.account_handle or '?')} (matches the store owner ID)",
            "  cost: $0.000, network calls: 0",
            f"  files read ({len(self.files_used)}): " + ", ".join(_safe_name(f) for f in self.files_used),
            f"  files recognized, not used ({len(self.files_known_unused)}): "
            + (", ".join(_safe_name(f) for f in self.files_known_unused) or "none"),
            f"  media files not read: {self.media_files}; archive viewer files not read: {self.viewer_files}",
            f"  files NOT recognized ({len(self.files_unrecognized)}):" + ("" if self.files_unrecognized else " none"),
        ]
        lines += [f"    {_safe_name(f)}" for f in self.files_unrecognized]
        lines += [
            f"  window: {win}",
            f"  post records: {self.records}; in the window: {self.posts_in_window}; outside the window: {self.outside_window}",
            "  events: " + ", ".join(f"{k} {v}" for k, v in self.mapped.items()) + f" (total {self.events})",
            f"  quote targets with a provisional h:<handle> ID: {self.provisional_quote_targets}",
            f"  posts with no event (own posts, self-replies): {self.posts_without_events}",
            "  unmapped: " + (", ".join(f"{k} {v}" for k, v in self.unmapped.items() if v) or "none"),
        ]
        if not self.dry_run:
            lines.append(f"  new interactions: {self.interactions_added}; already in the store: {self.already_in_store}")
            if self.text_dropped:
                lines.append(f"  retain_text = false: removed the text of {self.text_dropped} interactions (AD-11)")
        lines += [f"  warning: {_safe_name(w)}" for w in self.warnings]
        return lines


# ---------------------------------------------------------------- mapping
def map_tweet(
    record: Any,
    ego_id: str,
    ego_handle: str,
    endpoint: str,
    run_id: str,
    observed_at: str,
    known: dict[str, str],
    unmapped: dict[str, int],
) -> list[Interaction]:
    """Map one archive post record to interactions with the FR-21 type rules.

    ``known`` maps a case-folded handle to the numeric user ID of an X API
    profile in the store. ``unmapped`` counts the items that give no event.
    """
    tweet = record.get("tweet") if isinstance(record, dict) and isinstance(record.get("tweet"), dict) else record
    if not isinstance(tweet, dict):
        unmapped["not_a_post_record"] += 1
        return []
    post_id = _numeric_id(tweet.get("id_str", tweet.get("id")))
    if post_id is None:
        unmapped["no_post_id"] += 1
        return []
    rhash = record_hash(record)
    text_v = tweet.get("full_text", tweet.get("text"))
    text = text_v if isinstance(text_v, str) else None
    urls = _urls(tweet)
    created = archive_time(tweet.get("created_at"))
    unreadable = frozenset({"interaction_created_at"}) if tweet.get("created_at") is not None and created is None else frozenset()
    mentions = _mentions(tweet)
    by_handle = {h.casefold(): uid for uid, h in mentions if uid and h}
    ego_h = ego_handle.casefold()

    def make(kind: str, target: str, target_post: str | None) -> Interaction:
        values = {
            "source_post_id": post_id,
            "target_post_id": target_post,
            "interaction_created_at": created,
            "conversation_id": None,  # the archive post has no conversation ID
            "text": text,
            "urls": urls,
        }
        return Interaction(
            type=kind, source_user_id=ego_id, target_user_id=target, observed_at=observed_at,
            source_endpoint=endpoint, adapter=ADAPTER, adapter_version=ADAPTER_VERSION, run_id=run_id,
            field_status=status_for(values, INTERACTION_OPTIONAL_FIELDS, unreadable=unreadable),
            raw_request_hash=rhash, **values,
        )

    out: list[Interaction] = []
    rt = RT_RE.match(text or "")
    if rt:
        target = by_handle.get(rt.group("handle").casefold())
        if target is None:
            unmapped["repost_target_unknown"] += 1
        elif target != ego_id:
            out.append(make("repost", target, None))  # the archive has no reposted post ID
        return out

    covered: set[str] = {ego_id}
    covered_handles: set[str] = {ego_h}
    reply_user = _numeric_id(tweet.get("in_reply_to_user_id_str", tweet.get("in_reply_to_user_id")))
    reply_post = _numeric_id(tweet.get("in_reply_to_status_id_str", tweet.get("in_reply_to_status_id")))
    if reply_user:
        if reply_user not in covered:
            out.append(make("reply", reply_user, reply_post))
        covered.add(reply_user)
    elif reply_post:
        unmapped["reply_without_user_id"] += 1
    q = _quoted(tweet, urls)
    if q is not None:
        handle, qpost = q
        hf = handle.casefold()
        if hf not in covered_handles:
            target = by_handle.get(hf) or known.get(hf) or f"h:{hf}"
            covered_handles.add(hf)
            if target not in covered:
                out.append(make("quote", target, qpost))
            covered.add(target)
    elif any(re.match(r"^https?://(?:www\.)?(?:twitter|x)\.com/i(?:/web)?/status/\d+", u, re.I) for u in urls or []):
        unmapped["quote_without_handle"] += 1
    for uid, h in mentions:
        if uid is None:
            unmapped["mention_without_user_id"] += 1
            continue
        if uid in covered or (h is not None and h.casefold() in covered_handles):
            continue
        covered.add(uid)
        out.append(make("mention", uid, None))
    return out


# ---------------------------------------------------------------- import
def _account(src: _Source) -> tuple[str, str | None]:
    items = parse_js(src.read("data/account.js"), "data/account.js")
    for it in items:
        acc = it.get("account") if isinstance(it, dict) else None
        if isinstance(acc, dict):
            aid = _numeric_id(acc.get("accountId"))
            if aid:
                h = acc.get("username")
                return aid, h if isinstance(h, str) and HANDLE_RE.match(h) else None
    raise ArchiveError("data/account.js has no accountId; the owner check (FR-54) cannot run")


def _part_order(rel: str) -> tuple[int, str]:
    m = re.search(r"-part(\d+)\.js$", rel)
    return (int(m.group(1)) if m else 0, rel)


def run_import(
    cfg: Config,
    store: Store,
    log: RunLog,
    path: str | Path,
    run_id: str,
    *,
    dry_run: bool = False,
    days: int | None = None,
    now: datetime | None = None,
) -> ArchiveReport:
    """Import the owner's outbound events from an X archive. Makes no network call and costs $0.

    ``days`` limits the import to posts from the last N days (default
    ``collect.window_days``, the window of the X API collector). 0 imports all posts.
    Each problem that stops the import raises ArchiveError before the first write.
    """
    rep = ArchiveReport(run_id=run_id, archive=str(path), dry_run=dry_run)
    days = int(cfg.get("collect.window_days", 90)) if days is None else int(days)
    if days < 0:
        raise ArchiveError("--days must be 0 or more")
    now = now or datetime.now(UTC)
    if days:
        rep.window_days = days
        rep.window_start = (now - timedelta(days=days)).strftime(STORE_TIME_FMT)
        rep.window_end = now.strftime(STORE_TIME_FMT)

    src = open_archive(path)
    try:
        names = src.names()
        kinds = {rel: classify(rel) for rel, _ in names}
        if "data/account.js" not in kinds:
            raise ArchiveError(f"{path} has no data/account.js; it is not an X data archive")
        tweet_files = sorted((r for r, k in kinds.items() if k == "tweets"), key=_part_order)
        rep.files_used = ["data/account.js", *tweet_files]
        rep.files_known_unused = sorted(r for r, k in kinds.items() if k == "known")
        rep.files_unrecognized = sorted(r for r, k in kinds.items() if k == "unknown")
        rep.media_files = sum(1 for k in kinds.values() if k == "media")
        rep.viewer_files = sum(1 for k in kinds.values() if k == "viewer")

        # Owner check (invariant 1, FR-54): the archive account must be the store owner.
        account_id, account_handle = _account(src)
        rep.account_handle = account_handle
        ego = store.ego_id
        if ego is None:
            raise ArchiveError(
                "The store does not know the owner account ID yet. Run 'visualx auth status' or 'visualx collect' "
                "first; then import the archive."
            )
        if ego.startswith("h:"):
            raise ArchiveError(
                "The store has only a provisional owner ID (from the Grok source). Run 'visualx auth status' to "
                "record the X user ID of the owner; then import the archive."
            )
        if account_id != ego:
            raise ArchiveError("The archive belongs to a different account than the store owner (FR-54). Nothing was imported.")
        ego_handle = account_handle or str(store.get_meta("ego_handle") or cfg.get("owner.handle", "") or "")
        cfg_handle = str(cfg.get("owner.handle", "") or "").lstrip("@")
        if account_handle and cfg_handle and account_handle.casefold() != cfg_handle.casefold():
            rep.warnings.append(f"the archive handle @{account_handle} is not owner.handle @{cfg_handle} in the config "
                                "(the account ID matches; the handle can have changed)")
        if not tweet_files:
            rep.warnings.append("the archive has no data/tweets.js or data/tweet.js file")

        # Read every post file before the first write, so a broken file stops the import with no change.
        parsed = [(rel, parse_js(src.read(rel), rel)) for rel in tweet_files]
    finally:
        src.close()

    known = {(p.get("handle") or "").casefold(): uid for uid, p in store.profiles().items()
             if p.get("handle") and not uid.startswith("h:")}
    observed = now_iso()
    events: dict[str, Interaction] = {}
    for rel, records in parsed:
        endpoint = f"archive:{rel}"
        for record in records:
            rep.records += 1
            tweet = record.get("tweet") if isinstance(record, dict) and isinstance(record.get("tweet"), dict) else record
            created = archive_time(tweet.get("created_at")) if isinstance(tweet, dict) else None
            if rep.window_start and isinstance(tweet, dict):
                if created is None:
                    rep.unmapped["no_readable_time"] += 1  # the window check needs the post time
                    continue
                if not (rep.window_start <= created <= rep.window_end):
                    rep.outside_window += 1
                    continue
            before = sum(rep.unmapped.values())
            got = map_tweet(record, ego, ego_handle, endpoint, run_id, observed, known, rep.unmapped)
            if isinstance(tweet, dict) and _numeric_id(tweet.get("id_str", tweet.get("id"))):
                rep.posts_in_window += 1
                if not got and sum(rep.unmapped.values()) == before:
                    rep.posts_without_events += 1
            for it in got:
                if it.interaction_id not in events:  # the same post in two part files counts once
                    rep.mapped[it.type] += 1
                    events[it.interaction_id] = it

    # Handle-only profiles for the provisional quote targets, as the Grok source writes them.
    existing = set(store.profiles())
    profs: dict[str, Profile] = {}
    for it in events.values():
        uid = it.target_user_id
        if uid.startswith("h:") and uid not in existing and uid not in profs:
            pv = {"handle": uid[2:]}
            profs[uid] = Profile(user_id=uid, fetched_at=observed, source_endpoint=it.source_endpoint, adapter=ADAPTER,
                                 adapter_version=ADAPTER_VERSION, field_status=status_for(pv, PROFILE_OPTIONAL_FIELDS),
                                 handle=uid[2:])
    rep.provisional_quote_targets = len({it.target_user_id for it in events.values() if it.target_user_id.startswith("h:")})

    if dry_run:
        return rep

    sid = store.begin_stage("collect", run_id, {}, cfg.hash())
    counts: dict[str, Any] = {
        "source": ADAPTER, "complete": True, "cost_usd": 0.0, "requests": 0,
        "archive_files_read": len(rep.files_used), "archive_files_unrecognized": len(rep.files_unrecognized),
        "archive_posts": rep.records, "archive_posts_in_window": rep.posts_in_window,
        **{f"archive_{k}_events": v for k, v in rep.mapped.items()},
        "archive_unmapped": sum(rep.unmapped.values()),
    }
    if rep.window_start:
        counts.update(window_start=rep.window_start, window_end=rep.window_end, window_days=rep.window_days)
    with store.tx() as c:
        rep.interactions_added = store.put_interactions(events.values(), conn=c)
        store.put_profiles(list(profs.values()), conn=c)
        rep.already_in_store = len(events) - rep.interactions_added
        counts["interactions_added"] = rep.interactions_added
        store.finish_stage(sid, counts, conn=c)
    if not cfg.get("data.retain_text", True):
        rep.text_dropped = store.drop_text()
    log.write("archive_import", files_read=len(rep.files_used), files_unrecognized=len(rep.files_unrecognized),
              posts=rep.records, events=rep.events, added=rep.interactions_added,
              unmapped=sum(rep.unmapped.values()))
    return rep
