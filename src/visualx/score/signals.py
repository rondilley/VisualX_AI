"""Signal functions for the rule engine (ARCHITECTURE AD-08).

A rule in ``config/rules.yaml`` names one signal function. The function reads
one account and gives a value. The rule compares that value with its ``when``
condition. Thresholds and keyword lists come from the rule ``params``, so a
change to them is a configuration change (MR-03).

Each signal function declares its inputs: profile fields, interaction fields,
and context keys. A function can read only what it declares. If a declared
input is not available (``absent`` or ``unreadable`` in ``field_status``, or
no value in the context), the function raises ``MissingInput``. The engine
then records no result for that rule and writes a ``rule_skips`` row (FR-57).
A signal function never changes a missing value into zero.

No signal function reads a sensitive-category input (TR-06).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ..model import INTERACTION_OPTIONAL_FIELDS, INTERACTION_TYPES, PROFILE_OPTIONAL_FIELDS, normalize_text
from .skeleton import (
    lookalike_grade,
    lookalike_match,
    mixed_script_token,
)

CONTEXT_KEYS: frozenset[str] = frozenset(
    {"ego_user_id", "ego_handle", "ego_name", "allowlist", "profile_errors", "follows_ego",
     "known_accounts", "ego_posts", "pattern_members"}
)
TEXT_TYPES: tuple[str, ...] = ("reply", "quote", "mention")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class MissingInput(Exception):
    """A declared input of a signal is not available for this account."""

    def __init__(self, *fields: str):
        super().__init__("+".join(fields))
        self.fields: tuple[str, ...] = fields

    @property
    def field(self) -> str:
        return "+".join(self.fields)


class SignalDefinitionError(RuntimeError):
    """A signal function read an input that it did not declare (a code defect)."""


@dataclass
class Value:
    """The output of a signal: the value that ``when`` tests, and extra fields for the cause."""

    value: Any
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalDef:
    name: str
    fn: Callable[["Inputs", dict[str, Any]], Value]
    profile_fields: tuple[str, ...]
    interaction_fields: tuple[str, ...]
    context: tuple[str, ...]

    @property
    def inputs(self) -> tuple[str, ...]:
        return self.profile_fields + self.interaction_fields + self.context


SIGNALS: dict[str, SignalDef] = {}


def signal(
    *, profile: tuple[str, ...] = (), interaction: tuple[str, ...] = (), context: tuple[str, ...] = ()
) -> Callable[[Callable[["Inputs", dict[str, Any]], Value]], Callable[["Inputs", dict[str, Any]], Value]]:
    """Register a signal function with its declared inputs."""
    for f in profile:
        if f not in PROFILE_OPTIONAL_FIELDS:
            raise SignalDefinitionError(f"unknown profile field {f!r}")
    for f in interaction:
        if f not in INTERACTION_OPTIONAL_FIELDS:
            raise SignalDefinitionError(f"unknown interaction field {f!r}")
    for k in context:
        if k not in CONTEXT_KEYS:
            raise SignalDefinitionError(f"unknown context key {k!r}")

    def deco(fn: Callable[["Inputs", dict[str, Any]], Value]) -> Callable[["Inputs", dict[str, Any]], Value]:
        SIGNALS[fn.__name__] = SignalDef(fn.__name__, fn, tuple(profile), tuple(interaction), tuple(context))
        return fn

    return deco


@dataclass
class Account:
    """All data that the engine has for one account."""

    user_id: str
    profile: dict[str, Any] | None
    own: list[dict[str, Any]]  # interactions where this account is the source
    from_ego: list[dict[str, Any]]  # interactions from the ego to this account
    incoming: list[dict[str, Any]] = field(default_factory=list)  # interactions where this account is the target
    ego_user_id: str | None = None
    ego_handle: str | None = None
    ego_name: str | None = None
    allowlist: frozenset[str] = frozenset()
    profile_errors: frozenset[str] = frozenset()
    follows_ego: bool | None = None  # v0.1 does not collect follows
    # Context from the pattern layer (score/patterns.py). None means "not computed".
    known_accounts: tuple[dict[str, Any], ...] | None = None  # allowlist and top contacts (I02)
    ego_posts: dict[str, dict[str, Any]] | None = None  # owner posts with time and metrics (B08)
    pattern_members: dict[str, dict[str, dict[str, Any]]] | None = None  # detector -> user -> fields


class Inputs:
    """The view of one account that one signal function gets."""

    def __init__(self, account: Account, sdef: SignalDef):
        self.account = account
        self.sdef = sdef

    @property
    def user_id(self) -> str:
        return self.account.user_id

    # -- profile ---------------------------------------------------------
    def _declared(self, kind: str, name: str, allowed: tuple[str, ...]) -> None:
        if name not in allowed:
            raise SignalDefinitionError(f"signal {self.sdef.name} reads {kind} {name!r} but does not declare it")

    def has_profile(self) -> bool:
        return self.account.profile is not None

    def present(self, name: str) -> bool:
        """True if the profile has the field with status 'present'. Never raises for a missing value."""
        self._declared("profile field", name, self.sdef.profile_fields)
        p = self.account.profile
        return p is not None and p["field_status"].get(name) == "present" and p.get(name) is not None

    def profile(self, name: str) -> Any:
        """The value of a declared profile field. Raises MissingInput if it is not available."""
        if not self.present(name):
            raise MissingInput(name)
        return self.account.profile[name]  # type: ignore[index]

    def fetched_at(self) -> datetime:
        p = self.account.profile
        if p is None:
            raise MissingInput("profile")
        return parse_time(p["fetched_at"], "fetched_at")

    # -- interactions ----------------------------------------------------
    def own(
        self,
        name: str | None,
        types: tuple[str, ...] = INTERACTION_TYPES,
        target: str | None = None,
        require_any: bool = False,
    ) -> list[dict[str, Any]]:
        """This account's interactions of ``types`` that have field ``name``.

        If the account has interactions in scope but none has the field, the
        input is missing. If ``require_any`` is set, an empty scope is also a
        missing input.
        """
        scoped = [
            i for i in self.account.own
            if i["type"] in types and (target is None or i["target_user_id"] == target)
        ]
        if name is None:
            return scoped
        self._declared("interaction field", name, self.sdef.interaction_fields)
        have = [i for i in scoped if i["field_status"].get(name) == "present" and i.get(name) is not None]
        if not have and (scoped or require_any):
            raise MissingInput(name)
        return have

    def involved(self, name: str, require_any: bool = False) -> list[dict[str, Any]]:
        """All interactions with this account as the source or the target that have field ``name``.

        Same missing-input rule as ``own``.
        """
        self._declared("interaction field", name, self.sdef.interaction_fields)
        scoped = self.account.own + self.account.incoming
        have = [i for i in scoped if i["field_status"].get(name) == "present" and i.get(name) is not None]
        if not have and (scoped or require_any):
            raise MissingInput(name)
        return have

    def from_ego(self, types: tuple[str, ...]) -> list[dict[str, Any]]:
        self.context("ego_user_id")
        return [i for i in self.account.from_ego if i["type"] in types]

    # -- context ---------------------------------------------------------
    def context(self, key: str, required: bool = True) -> Any:
        self._declared("context key", key, self.sdef.context)
        v = getattr(self.account, key)
        if v is None and required:
            raise MissingInput(key)
        return v


def parse_time(s: Any, name: str) -> datetime:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError as e:
        raise MissingInput(name) from e


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400.0


def _phrase_hit(text: str, phrases: list[str]) -> str | None:
    norm = normalize_text(text)
    for p in phrases:
        np_ = normalize_text(str(p))
        if np_ and re.search(rf"(?<!\w){re.escape(np_)}(?!\w)", norm):
            return str(p)
    return None


def _host(url: str) -> str:
    try:
        host = urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _domain_hit(url: str, domains: list[str]) -> str | None:
    host = _host(url if "://" in url else "http://" + url)
    if host.startswith("www."):
        host = host[4:]
    for d in domains:
        d = str(d).lower()
        if host == d or host.endswith("." + d):
            return d
    return None


# ---------------------------------------------------------------------------
# Gate signals (N01 to N03)
# ---------------------------------------------------------------------------

GATE_FIELDS = ("created_at", "followers_count", "following_count", "tweet_count", "listed_count")


@signal(profile=GATE_FIELDS)
def profile_data_missing(inp: Inputs, params: dict[str, Any]) -> Value:
    """N01: no profile, or no created_at, or no public metrics."""
    if not inp.has_profile():
        return Value(True, {"missing": "profile"})
    missing = [f for f in GATE_FIELDS if not inp.present(f)]
    return Value(bool(missing), {"missing": ",".join(missing)})


@signal(profile=("protected",), context=("profile_errors",))
def protected_or_unavailable(inp: Inputs, params: dict[str, Any]) -> Value:
    """N02: the user lookup gave an error, or the account is protected and has no interactions."""
    if inp.user_id in inp.context("profile_errors"):
        return Value(True, {"reason": "lookup_error"})
    protected = inp.profile("protected")
    has_items = bool(inp.own(None))
    return Value(bool(protected) and not has_items, {"reason": "protected"})


@signal(context=("ego_user_id", "allowlist"))
def known_account(inp: Inputs, params: dict[str, Any]) -> Value:
    """N03: the account is the ego or is on the owner allowlist."""
    if inp.user_id == inp.context("ego_user_id", required=False):
        return Value(True, {"known": "ego"})
    if inp.user_id in inp.context("allowlist"):
        return Value(True, {"known": "allowlist"})
    return Value(False, {"known": ""})


# ---------------------------------------------------------------------------
# Profile signals (P01, P03, P05, P06, P07)
# ---------------------------------------------------------------------------


@signal(profile=("created_at",), interaction=("interaction_created_at",))
def account_age_days_at_first_interaction(inp: Inputs, params: dict[str, Any]) -> Value:
    """P01: whole days from account creation to the earliest timed interaction that involves it.

    The account can be the source or the target (for example, a reply from the
    owner to the account), changed 2026-11-03 by the owner after the first live run.
    Reposts from retweeted_by have no time, so they do not count. With no timed
    interaction the input is missing.
    """
    created = parse_time(inp.profile("created_at"), "created_at")
    timed = inp.involved("interaction_created_at", require_any=True)
    first = min(parse_time(i["interaction_created_at"], "interaction_created_at") for i in timed)
    return Value(math.floor(_days(created, first)))


@signal(profile=("description", "url", "location"))
def empty_profile(inp: Inputs, params: dict[str, Any]) -> Value:
    """P03: bio, URL, and location are all empty."""
    vals = [inp.profile(f) for f in ("description", "url", "location")]
    return Value(all(not str(v).strip() for v in vals))


@signal(profile=("followers_count", "following_count"))
def low_followers_high_following(inp: Inputs, params: dict[str, Any]) -> Value:
    """P05: followers below a limit and following at or above a limit."""
    f = int(inp.profile("followers_count"))
    g = int(inp.profile("following_count"))
    hit = f < params["followers_below"] and g >= params["following_at_least"]
    return Value(hit, {"followers": f, "following": g})


@signal(profile=("tweet_count", "created_at"))
def lifetime_posts_per_day(inp: Inputs, params: dict[str, Any]) -> Value:
    """P06: post count divided by account age in days (minimum age one day)."""
    n = int(inp.profile("tweet_count"))
    created = parse_time(inp.profile("created_at"), "created_at")
    age = max(1.0, _days(created, inp.fetched_at()))
    return Value(round(n / age, 1))


@signal(profile=("followers_count", "following_count"))
def no_social_graph(inp: Inputs, params: dict[str, Any]) -> Value:
    """P07: no followers and follows no accounts."""
    f = int(inp.profile("followers_count"))
    g = int(inp.profile("following_count"))
    return Value(f == 0 and g == 0)


# ---------------------------------------------------------------------------
# Trust signals (T01 to T03)
# ---------------------------------------------------------------------------


@signal(profile=("verified_type",))
def verified_type(inp: Inputs, params: dict[str, Any]) -> Value:
    """T01: the verification type as X gives it."""
    return Value(str(inp.profile("verified_type")))


@signal(profile=("created_at", "followers_count", "listed_count"))
def established_account(inp: Inputs, params: dict[str, Any]) -> Value:
    """T02: account age, followers, and list count all at or above their limits."""
    created = parse_time(inp.profile("created_at"), "created_at")
    followers = int(inp.profile("followers_count"))
    listed = int(inp.profile("listed_count"))
    age = _days(created, inp.fetched_at())
    hit = age >= params["min_age_days"] and followers >= params["min_followers"] and listed >= params["min_listed"]
    return Value(hit, {"years": int(age // 365), "listed": listed, "followers": followers})


@signal(context=("ego_user_id",))
def ego_replied_or_quoted(inp: Inputs, params: dict[str, Any]) -> Value:
    """T03: count of replies and quotes from the ego to this account. Follows are not used."""
    return Value(len(inp.from_ego(("reply", "quote"))))


# ---------------------------------------------------------------------------
# Behavior signals (B01, B03, B04, B05, B07)
# ---------------------------------------------------------------------------


def _shingles(words: list[str], k: int) -> frozenset[tuple[str, ...]]:
    if len(words) < k:
        return frozenset({tuple(words)})
    return frozenset(tuple(words[i:i + k]) for i in range(len(words) - k + 1))


@signal(interaction=("text",))
def max_duplicate_replies(inp: Inputs, params: dict[str, Any]) -> Value:
    """B01: size of the largest group of replies with the same or almost the same normalized text.

    Two replies are in one group if their normalized text is equal, or if the
    Jaccard similarity of their word shingles is at or above ``jaccard``.

    Equal texts are one group before the comparison. The near pairs come from a
    prefix-filter index: two sets with Jaccard at or above t share at least one
    of the first ``len - ceil(t * len) + 1`` items of each set in one global
    order (rare items first). The result is the same as a comparison of each
    pair, without the n^2 work on many replies (review 20261103, B-12).
    """
    replies = inp.own("text", types=("reply",))
    norms = [n for n in (normalize_text(i["text"]) for i in replies) if n]
    k = int(params.get("shingle_words", 3))
    limit = float(params.get("jaccard", 1.0))
    count: dict[str, int] = {}
    for n in norms:
        count[n] = count.get(n, 0) + 1
    texts = sorted(count)
    sh = [_shingles(n.split(), k) for n in texts]
    parent = list(range(len(texts)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if 0 < limit <= 1:
        freq: dict[tuple[str, ...], int] = {}
        for s in sh:
            for g in s:
                freq[g] = freq.get(g, 0) + 1
        index: dict[tuple[str, ...], list[int]] = {}
        for a, s in enumerate(sh):
            ordered = sorted(s, key=lambda g: (freq[g], g))
            prefix = ordered[: len(ordered) - math.ceil(limit * len(ordered)) + 1]
            cands: set[int] = set()
            for g in prefix:
                cands.update(index.get(g, ()))
                index.setdefault(g, []).append(a)
            for b in cands:
                if find(a) == find(b):
                    continue
                small, big = sorted((len(sh[a]), len(sh[b])))
                if big == 0 or small / big < limit:
                    continue  # the Jaccard value cannot reach the limit
                inter = len(sh[a] & sh[b])
                if inter / (len(sh[a]) + len(sh[b]) - inter) >= limit:
                    parent[find(b)] = find(a)
    sizes: dict[int, int] = {}
    for x in range(len(texts)):
        r = find(x)
        sizes[r] = sizes.get(r, 0) + count[texts[x]]
    return Value(max(sizes.values(), default=0))


@signal(interaction=("interaction_created_at",), context=("ego_user_id",))
def max_replies_to_ego_in_window(inp: Inputs, params: dict[str, Any]) -> Value:
    """B03: the most replies and mentions to the ego in one sliding time window.

    Only interactions with a time count. One post that is both a reply and a
    mention counts one time.
    """
    ego = inp.context("ego_user_id")
    items = inp.own("interaction_created_at", types=("reply", "mention"), target=ego)
    seen: dict[str, datetime] = {}
    for i in items:
        key = i["source_post_id"] if i.get("source_post_id") else i["interaction_id"]
        seen[key] = parse_time(i["interaction_created_at"], "interaction_created_at")
    times = sorted(seen.values())
    window = float(params["window_minutes"]) * 60.0
    best = 0
    lo = 0
    for hi, t in enumerate(times):
        while (t - times[lo]).total_seconds() >= window:
            lo += 1
        best = max(best, hi - lo + 1)
    return Value(best)


def _any_source(inp: Inputs, checks: list[Callable[[], tuple[int, str | None]]]) -> tuple[str | None, list[str]]:
    """Run each source check. Returns (first hit, missing fields).

    A check returns (items examined, hit). A hit in any source fires the rule.
    With no hit, a missing source makes the input missing: the source that is
    not there could have the hit, so "did not fire" would be a made-up result
    (invariant 10, review 20261103, B-07). Before, the rule gave "did not fire"
    when one other source was examined.
    """
    missing: list[str] = []
    for check in checks:
        try:
            _n, hit = check()
        except MissingInput as e:
            missing.extend(e.fields)
            continue
        if hit is not None:
            return hit, missing
    if missing:
        raise MissingInput(*missing)
    return None, missing


@signal(profile=("description",), interaction=("text",))
def scam_phrase(inp: Inputs, params: dict[str, Any]) -> Value:
    """B04: a reply, quote, mention, or the bio contains a phrase from the list."""
    phrases = list(params["phrases"])

    def bio() -> tuple[int, str | None]:
        return 1, _phrase_hit(str(inp.profile("description")), phrases)

    def posts() -> tuple[int, str | None]:
        items = inp.own("text", types=TEXT_TYPES)
        for i in items:
            hit = _phrase_hit(i["text"], phrases)
            if hit is not None:
                return len(items), hit
        return len(items), None

    hit, _ = _any_source(inp, [posts, bio])
    return Value(hit is not None, {"phrase": hit or ""})


@signal(profile=("url", "description"), interaction=("urls",))
def messenger_or_shortener_link(inp: Inputs, params: dict[str, Any]) -> Value:
    """B05: a reply link, the profile URL, or a link in the bio goes to a listed domain."""
    domains = list(params["domains"])

    def post_links() -> tuple[int, str | None]:
        items = inp.own("urls", types=TEXT_TYPES)
        for i in items:
            for u in i["urls"]:
                d = _domain_hit(str(u), domains)
                if d:
                    return len(items), d
        return len(items), None

    def profile_url() -> tuple[int, str | None]:
        return 1, _domain_hit(str(inp.profile("url")), domains) if str(inp.profile("url")).strip() else None

    def bio_links() -> tuple[int, str | None]:
        for u in _URL_RE.findall(str(inp.profile("description"))):
            d = _domain_hit(u, domains)
            if d:
                return 1, d
        return 1, None

    hit, _ = _any_source(inp, [post_links, profile_url, bio_links])
    return Value(hit is not None, {"domain": hit or ""})


@signal(interaction=("urls",))
def threat_feed_domain(inp: Inputs, params: dict[str, Any]) -> Value:
    """B06: a link in a reply, quote, or mention goes to a domain on the owner's threat feed.

    ``engine.load_rules`` reads the feed file (``feed_file`` in the rule, relative to
    the configuration folder) into ``domains``, a set of lower-case host names. With no
    feed file the rule has no input (FR-57). Only post links count, not the profile URL
    or the bio, so a profile field alone cannot give this hard rule (FR-35).
    """
    hosts = params.get("domains")
    if hosts is None:
        raise MissingInput("threat_feed")
    for i in inp.own("urls", types=TEXT_TYPES):
        for u in i["urls"]:
            h = _host(str(u) if "://" in str(u) else "http://" + str(u))
            parts = h.split(".")
            for k in range(len(parts) - 1):
                cand = ".".join(parts[k:])
                if cand in hosts:
                    return Value(True, {"domain": cand})
    return Value(False, {"domain": ""})


@signal(context=("follows_ego",))
def account_follows_ego(inp: Inputs, params: dict[str, Any]) -> Value:
    """B05 escalation: does the account follow the ego? v0.1 does not collect follows."""
    return Value(bool(inp.context("follows_ego")))


@signal(interaction=("text",))
def ai_authorship_phrase(inp: Inputs, params: dict[str, Any]) -> Value:
    """B07: a reply, quote, or mention contains a listed phrase that shows AI authorship."""
    phrases = list(params["phrases"])
    for i in inp.own("text", types=TEXT_TYPES):
        hit = _phrase_hit(i["text"], phrases)
        if hit is not None:
            return Value(True, {"phrase": hit})
    return Value(False, {"phrase": ""})


# ---------------------------------------------------------------------------
# Impersonation signals (I01)
# ---------------------------------------------------------------------------


@signal(profile=("handle", "name", "created_at"), context=("ego_user_id", "ego_handle", "ego_name"))
def impersonates_ego(inp: Inputs, params: dict[str, Any]) -> Value:
    """I01: the account looks like the owner account, with the strong test only (hard rule).

    I01 is a hard rule: it gives ``many_signals`` alone. So it must not fire for
    a different person who has a near handle or the same name (review 20261103,
    B-01). The test is ``skeleton.lookalike_match`` against the owner:

    - ``handle_skeleton`` (the handle looks the same as the owner handle, for
      example a changed separator or a confusable letter) fires at any age.
    - A display name that looks like the owner name or handle, or a near handle,
      fires only if ``lookalike_grade`` gives "high" and the account is younger
      than ``young_account_days``. An old account with the same name is often a
      second person with that name. The lookalike detector still reports it.

    ``created_at`` is optional: without it, the account counts as not young.
    The user ID must be different from the owner.
    """
    ego_id = inp.context("ego_user_id")
    ego_handle = str(inp.context("ego_handle")).lstrip("@")
    handle = str(inp.profile("handle")).lstrip("@")
    fields = {"ego_handle": ego_handle}
    if inp.user_id == ego_id:
        return Value(False, {**fields, "match": ""})
    name = str(inp.profile("name")) if inp.present("name") else None
    ego_name = inp.context("ego_name", required=False)
    m = lookalike_match(handle, name, ego_handle, str(ego_name) if ego_name else None, params)
    if m is None:
        return Value(False, {**fields, "match": ""})
    kind = m[0]
    if kind == "handle_skeleton":
        return Value(True, {**fields, "match": kind})
    created = parse_time(inp.profile("created_at"), "created_at") if inp.present("created_at") else None
    age = _days(created, inp.fetched_at()) if created is not None else None
    young = age is not None and age < float(params["young_account_days"])
    grade = lookalike_grade(kind, name, str(ego_name) if ego_name else None, age, True, params)
    if young and grade == "high":
        return Value(True, {**fields, "match": kind})
    return Value(False, {**fields, "match": ""})


# ---------------------------------------------------------------------------
# Starter rules added in v0.2 (FR-56): P02, P04, B02, B08, I02, I03
# ---------------------------------------------------------------------------

_DEFAULT_IMAGE = "default_profile_images"
_HASHTAG_RE = re.compile(r"#(\w+)")


@signal(profile=("profile_image_url",))
def default_profile_image(inp: Inputs, params: dict[str, Any]) -> Value:
    """P02: the profile image URL is the X default image (``default_profile_images`` in the path)."""
    url = str(inp.profile("profile_image_url"))
    marker = str(params.get("marker", _DEFAULT_IMAGE))
    return Value(marker in url)


@signal(profile=("handle",))
def generated_handle(inp: Inputs, params: dict[str, Any]) -> Value:
    """P04: the handle is letters (and ``_``) and then ``min_digits`` or more digits.

    X suggests handles of this form at sign-up, so many human users have one.
    The rule has 1 point for this reason.
    """
    handle = str(inp.profile("handle")).lstrip("@")
    n = int(params["min_digits"])
    m = re.fullmatch(r"[A-Za-z][A-Za-z_]*?(\d+)", handle)
    digits = len(m.group(1)) if m else 0
    return Value(digits >= n, {"digits": digits})


def _pattern_info(inp: Inputs, key: str) -> dict[str, Any] | None:
    members = inp.context("pattern_members")
    return members.get(key, {}).get(inp.user_id)


@signal(interaction=("text", "urls"), context=("pattern_members",))
def shared_text_or_link(inp: Inputs, params: dict[str, Any]) -> Value:
    """B02: the account sent the same normalized text or the same link as 2 or more other accounts.

    The groups come from the pattern detector ``shared_content`` (score/patterns.py),
    which compares all accounts. If the account has interactions but none has
    text or links, the input is missing.
    """
    info = _pattern_info(inp, "shared_text_or_link")
    if info is not None:
        return Value(True, dict(info))

    def text() -> tuple[int, str | None]:
        return len(inp.own("text", types=TEXT_TYPES)), None

    def links() -> tuple[int, str | None]:
        return len(inp.own("urls", types=TEXT_TYPES)), None

    _any_source(inp, [text, links])
    return Value(False, {"kind": "", "others": 0})


@signal(
    profile=("created_at",), interaction=("target_post_id", "interaction_created_at"), context=("ego_posts",)
)
def new_account_on_high_reach_post(inp: Inputs, params: dict[str, Any]) -> Value:
    """B08: a reply or quote to one of the top owner posts, from an account younger than ``max_age_days`` then.

    The top posts are the top ``top_fraction`` of owner posts by impressions,
    and the top ``top_fraction`` by replies (at least one post each). With fewer
    than ``min_posts`` owner posts with metrics, a "top 10%" has no meaning and
    the input ``ego_posts`` is missing. The age test is the P01 test at the time
    of the reply.
    """
    posts = inp.context("ego_posts")
    to_owner = [i for i in inp.own("target_post_id", types=("reply", "quote")) if i["target_post_id"] in posts]
    if not to_owner:
        return Value(False, {"post_id": ""})  # no reply to an owner post: the rule does not apply
    measured = {k: v for k, v in posts.items() if v.get("impressions") is not None or v.get("replies") is not None}
    if len(measured) < int(params["min_posts"]):
        raise MissingInput("ego_posts")
    frac = float(params["top_fraction"])
    k = max(1, math.ceil(len(measured) * frac))
    top: set[str] = set()
    for metric in ("impressions", "replies"):
        ranked = sorted(
            (pid for pid, v in measured.items() if (v.get(metric) or 0) > 0),
            key=lambda pid: measured[pid][metric], reverse=True,
        )
        top.update(ranked[:k])
    items = [i for i in to_owner if i["target_post_id"] in top]
    if not items:
        return Value(False, {"post_id": ""})
    created = parse_time(inp.profile("created_at"), "created_at")
    timed = [i for i in items if i["field_status"].get("interaction_created_at") == "present" and i.get("interaction_created_at")]
    if not timed:
        raise MissingInput("interaction_created_at")
    for i in sorted(timed, key=lambda x: x["interaction_created_at"]):
        age = _days(created, parse_time(i["interaction_created_at"], "interaction_created_at"))
        if age < float(params["max_age_days"]):
            return Value(True, {"post_id": i["target_post_id"], "age_days": math.floor(age)})
    return Value(False, {"post_id": ""})


@signal(profile=("handle", "name", "created_at"), context=("known_accounts",))
def impersonates_known(inp: Inputs, params: dict[str, Any]) -> Value:
    """I02: the handle or display name looks like a known account (allowlist or a top contact of the owner).

    The test is ``skeleton.lookalike_match``: equal UTS #39 skeletons (with
    the separators removed), a handle equal to the display name of the other,
    or a near handle (edit distance by handle length). A known account does not
    match itself, and a known account does not fire this rule, because two
    close contacts with near names are usually related accounts.

    The rule fires only when ``skeleton.lookalike_grade`` gives "high", the
    same test as a "high" finding of the lookalike detector (validation
    2026-11-03). An old account with the same name (often a second person
    with that name), a near handle with another display name, and a near
    handle on an account made before the known account do not fire.
    ``created_at`` is optional here: without it, the account counts as not
    young.
    """
    known = inp.context("known_accounts")
    if any(k["user_id"] == inp.user_id for k in known):
        return Value(False, {"known": "", "match": ""})
    handle = str(inp.profile("handle")).lstrip("@")
    name = str(inp.profile("name")) if inp.present("name") else None
    created = parse_time(inp.profile("created_at"), "created_at") if inp.present("created_at") else None
    age = _days(created, inp.fetched_at()) if created is not None else None
    for k in known:
        if not k.get("handle"):
            continue
        m = lookalike_match(handle, name, str(k["handle"]), k.get("name"), params)
        if m is None:
            continue
        kc = k.get("created_at")
        before = created is not None and bool(kc) and created < parse_time(kc, "created_at")
        if lookalike_grade(m[0], name, k.get("name"), age, False, params, before) != "high":
            continue
        return Value(True, {"known": str(k["handle"]).lstrip("@"), "match": m[0], "distance": m[1],
                            "known_user_id": k["user_id"], "role": k.get("role", "")})
    return Value(False, {"known": "", "match": ""})


_SCRIPT_JOIN = " and "


@signal(profile=("name",), interaction=("text",))
def mixed_script_name_or_hashtag(inp: Inputs, params: dict[str, Any]) -> Value:
    """I03: one word of the display name, or one hashtag in the account's text, mixes scripts (UTS #39).

    See ``skeleton.mixed_script_token``: the test is on each word, so a
    multilingual name with words in two scripts does not fire. The rule never
    gives points to a script or a language as such (research section 6.3).
    """
    def name() -> tuple[int, str | None]:
        m = mixed_script_token(str(inp.profile("name")))
        return 1, (None if m is None else "Name|" + _SCRIPT_JOIN.join(sorted(m[1])))

    def hashtags() -> tuple[int, str | None]:
        items = inp.own("text", types=TEXT_TYPES)
        for i in items:
            for tag in _HASHTAG_RE.findall(i["text"]):
                m = mixed_script_token(tag)
                if m is not None:
                    return len(items), "Hashtag|" + _SCRIPT_JOIN.join(sorted(m[1]))
        return len(items), None

    hit, _ = _any_source(inp, [name, hashtags])
    if hit is None:
        return Value(False, {"where": "", "scripts": ""})
    where, scripts = hit.split("|", 1)
    return Value(True, {"where": where, "scripts": scripts})


# ---------------------------------------------------------------------------
# Coordination rules from the pattern layer (B09 to B12)
# ---------------------------------------------------------------------------
#
# The pattern detectors (score/patterns.py) compare accounts with each other.
# An account that is a member of a coordination finding (severity at or above
# patterns.member_rule_min_severity) gets the rule. The detector gives the
# fields for the cause. Each signal also declares the fields that the detector
# used, so an account without them has a missing input (FR-57).


@signal(interaction=("interaction_created_at",), context=("pattern_members",))
def in_reply_burst(inp: Inputs, params: dict[str, Any]) -> Value:
    """B09: member of a coordinated burst (detector ``reply_burst``)."""
    info = _pattern_info(inp, "reply_burst")
    if info is not None:
        return Value(True, dict(info))
    inp.own("interaction_created_at", types=("reply", "quote", "mention"))
    return Value(False, {})


@signal(profile=("created_at",), context=("pattern_members",))
def in_creation_cluster(inp: Inputs, params: dict[str, Any]) -> Value:
    """B10: member of an account-creation cluster (detector ``creation_cluster``)."""
    info = _pattern_info(inp, "creation_cluster")
    if info is not None:
        return Value(True, dict(info))
    inp.profile("created_at")
    return Value(False, {})


@signal(interaction=("interaction_created_at", "target_post_id"), context=("pattern_members",))
def fast_reply_timing(inp: Inputs, params: dict[str, Any]) -> Value:
    """B11: replies to owner posts arrive very soon after the post (detector ``fast_reply``)."""
    info = _pattern_info(inp, "fast_reply")
    if info is not None:
        return Value(True, dict(info))
    inp.own("interaction_created_at", types=("reply",))
    inp.own("target_post_id", types=("reply",))
    return Value(False, {})


@signal(interaction=("urls",), context=("pattern_members",))
def shared_link_domain(inp: Inputs, params: dict[str, Any]) -> Value:
    """B12: linked to the same less common site as 2 or more other accounts in a short time (detector ``shared_content``)."""
    info = _pattern_info(inp, "shared_domain")
    if info is not None:
        return Value(True, dict(info))
    inp.own("urls", types=TEXT_TYPES)
    return Value(False, {})


# ---------------------------------------------------------------------------
# Network rules from score/network.py (B13, B14, I04)
# ---------------------------------------------------------------------------


@signal(interaction=("interaction_created_at", "target_post_id"), context=("pattern_members",))
def in_co_action(inp: Inputs, params: dict[str, Any]) -> Value:
    """B13: replied to or quoted the same owner posts within minutes of other accounts, more than chance (``co_action``)."""
    info = _pattern_info(inp, "co_action")
    if info is not None:
        return Value(True, dict(info))
    inp.own("interaction_created_at", types=("reply", "quote"))
    inp.own("target_post_id", types=("reply", "quote"))
    return Value(False, {})


@signal(interaction=("source_post_id",), context=("pattern_members",))
def mass_tagging(inp: Inputs, params: dict[str, Any]) -> Value:
    """B14: tagged many accounts with no link to each other, a new set in each post (``mass_tagging``)."""
    info = _pattern_info(inp, "mass_tagging")
    if info is not None:
        return Value(True, dict(info))
    inp.own("source_post_id", types=("reply", "quote", "mention"))
    return Value(False, {})


@signal(profile=("handle",), context=("pattern_members",))
def renamed_toward_known(inp: Inputs, params: dict[str, Any]) -> Value:
    """I04: changed its handle or display name to look like the owner or a known account (``profile_change``, high)."""
    info = _pattern_info(inp, "profile_change")
    if info is not None:
        return Value(True, dict(info))
    inp.profile("handle")
    return Value(False, {})
