"""Labeled scenarios for the pattern-detector benchmark (tests/validation).

A scenario is a synthetic store for one owner: background traffic that looks
like the ego network of a security practitioner, plus planted campaigns with
ground truth. ``run_scenario`` puts the records in a new store, runs the real
``build`` and ``score`` stages, and reads the findings, the rule results, and
the states. ``evaluate`` compares them with the ground truth.

The records follow the live data shape (collect/xapi.py, FR-21):

- The collector sees only posts by the owner and posts that reply to, quote,
  or mention the owner. Each post gives one interaction for each target: a
  reply to the replied-to author, a quote to the quoted author, and a mention
  to each other mentioned account.
- A mention has no target post. A repost from ``retweeted_by`` has no time,
  no text, and no post ID.
- Owner posts are in the table ``posts`` with impressions and the reply count.

Background design (each number is a stated assumption, not a measurement):

- Account age: 80 % of contacts are 2 to 19 years old, 12 % are 180 days to
  2 years, 6 % are 30 to 180 days, 2 % are 3 to 30 days (new real users).
- Followers, following, and lifetime posts per day are log-normal.
- Each contact has 1 + Pareto(2) episodes in 90 days. An episode is a reply to
  an owner post, a mention, a quote, a repost, or a conversation with the
  owner. Reply latency is log-normal with a median of 40 minutes, 8 minutes on
  a viral post, with a per-person speed factor.
- The owner answers some replies. 40 % of those answers get a fast answer
  back (10 to 90 seconds): a live conversation, not automation.
- 12 % of posts carry a link: common sites, security news sites, and personal
  blogs. A few news events make 2 to 4 contacts share the same article.
- Announcement posts have 10 times the usual reach and get formulaic
  congratulations, some of them identical and 30 characters or more after
  normalization.
- Groups of colleagues mention each other and the owner (mutual groups).
- Some persons have a second (work) account with the same display name.
- A few benign accounts are unusual: aggregators, very large accounts,
  lurkers, new real users, organizations.
- Seed timelines (FR-07, step 6): the 20 top contacts by tie weight have 30
  days of their own posts. Each seed belongs to one of 5 scenes. A scene is a
  pool of other accounts (most of them not contacts of the owner) that the
  seed replies to, quotes, and mentions. These posts come from the timeline
  endpoint, so the detectors know the seeds.
- 3 % of the contacts changed the handle or the display name once before the
  last fetch (profile history), for example a new handle or a job title.

All profiles have the adapter ``xapi``, because the store records the profile
history only for X API profiles.

Planted campaigns have a ``tune`` form (used to set thresholds) and a
``holdout`` form (other sizes, timings, and look-alike methods, never used to
set thresholds). Each detector also has hard negatives near its threshold.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from visualx.build import edge_weight, run_build
from visualx.config import Config
from visualx.logs import NullLog
from visualx.model import (
    INTERACTION_OPTIONAL_FIELDS,
    PROFILE_OPTIONAL_FIELDS,
    Interaction,
    Profile,
    status_for,
)
from visualx.score.calibrate import DETECTION_LEVEL
from visualx.score.engine import RuleSet, load_rules, run_score
from visualx.store import Store

REPO = Path(__file__).resolve().parents[2]
RULES_PATH = REPO / "config" / "rules.yaml"

UTC = timezone.utc
NOW = datetime(2026, 11, 1, tzinfo=UTC)
FETCHED = "2026-11-01T00:00:00.000Z"
WINDOW_DAYS = 90
START = NOW - timedelta(days=WINDOW_DAYS)

EGO = "100"
EGO_HANDLE = "rondilley"
EGO_NAME = "Ron Dilley"

WEIGHTS = {"reply": 3.0, "quote": 2.0, "mention": 1.5, "repost": 1.0, "reciprocity_factor": 1.5}

DETECTORS: tuple[str, ...] = (
    "reply_burst", "creation_cluster", "fast_reply", "shared_content",
    "lookalike", "profile_outliers", "one_way", "tight_group",
    "co_action", "profile_change", "mass_tagging", "community_anomaly",
)
COORDINATION: tuple[str, ...] = ("reply_burst", "creation_cluster", "fast_reply", "shared_content",
                                 "co_action", "mass_tagging")
# The severity at which a finding counts as a detection. The coordination
# detectors give a rule (B02, B09 to B12) only at "notable" or higher, and a
# lookalike of the owner or a known account is "high". The other detectors are
# "info" by design: a finding is a reason to look.
# One source with the calibration report (review 20261103: the two lists were copies).
DETECT_LEVEL: dict[str, str] = {
    d: DETECTION_LEVEL.get(d, "notable")
    for d in ("reply_burst", "creation_cluster", "fast_reply", "shared_content", "lookalike", "profile_outliers",
              "one_way", "tight_group", "co_action", "profile_change", "mass_tagging", "community_anomaly")
}
SEV_RANK = {"info": 0, "notable": 1, "high": 2}
MEMBER_RULES = {"reply_burst": "B09", "creation_cluster": "B10", "fast_reply": "B11", "shared_content": "B02|B12",
                "co_action": "B13", "mass_tagging": "B14"}
SEED_COUNT = 20
SEED_DAYS = 30
SCENES = 5

# -- word pools ----------------------------------------------------------------

FIRST = (
    "alice adam amir ana andrea anton ben beth bilal carla carlos chen chris claire dan dana david deepa diego "
    "dmitri elena eli emma eric farah felix fiona frank gina greg hana hannah henrik ian ines irene ivan jack "
    "jamal jane jason jen jia joe john jonas jorge julia kai karen kate kenji kim kurt lara lars leo liam lina "
    "lucas luis maria mark marta max maya mei mike mina nadia nate nina noah olga omar oscar pablo paul petra "
    "priya quinn raj rachel ravi rita rob rosa sam sara sean sofia stefan sue tara tom uma victor wei will "
    "yara yusuf zane zoe"
).split()
LAST = (
    "abbott acosta adler ahmed alvarez bauer becker bell berg brooks burke campbell carter chang chavez cohen "
    "cole costa cruz dalton das diaz dixon doyle duarte ellis evans fischer flores ford foster garcia gill "
    "gomez grant gray gupta hahn hall hansen hart hayes hill ho holt hughes ivanov jensen jones kang kaur "
    "keller khan kim klein koch kowalski kumar lam lang larsen lee lopez lund mason meyer miller moreno "
    "murphy nagy nash nguyen novak obrien ortiz park patel perez petrov price quinn ramos reed reyes rivera "
    "rossi roth russo saito santos schmidt shah silva singh sousa stone suzuki tan taylor torres tran vega "
    "vogel wagner walsh wang weber wells white wong wu yamada young zhang"
).split()
WORDS = (
    "acorn amber anvil arrow aspen atlas basalt beacon birch bolt bramble breeze cedar cinder citadel clover "
    "comet coral crane crypt delta drift dusk echo ember falcon fern flint forge frost garnet glyph granite "
    "harbor hawk hollow iris ivory jade kestrel lagoon lantern ledger lumen lynx maple marble meadow mesa "
    "nimbus north oak onyx orbit osprey otter pebble pixel prism quartz quill raven reef ridge rook rune "
    "sable sage shard signal slate spark spruce static summit talon thorn tidal topaz tundra vale vector "
    "violet vortex willow wren zenith zephyr"
).split()
TOPICS = (
    "logging edr siem mfa phishing ransomware backups patching inventory iam sso oauth tls dns bgp kerberos "
    "ntlm beacons yara sigma suricata zeek sbom supplychain firmware uefi bootkits kubernetes "
    "s3 buckets secrets vaults keys rotation tabletop incident triage playbooks threatintel osint deception "
    "honeypots canaries segmentation zerotrust vpn rdp ssh lateral persistence exfil dlp email spf dkim dmarc"
).split()
OPENERS = (
    "Good point", "Interesting", "Seen this too", "Agree", "Not sure about that", "We hit the same thing",
    "Matches what we see", "Counterpoint", "Solid take", "Hard disagree", "Underrated point", "Yes and",
    "This is the part people skip", "Fair, but", "Saving this", "Real talk", "From the IR side",
)
BODIES = (
    "the {a} gap is the real problem with {b}", "most teams never test {a} until {b} breaks",
    "{a} is still underrated next to {b}", "we patched {a} last week and {b} still alerted",
    "{a} detections need tuning before {b} helps", "nobody budgets for {a} and then {b} happens",
    "{a} without {b} is just theater", "our {a} rollout broke {b} for two days",
    "the vendor docs on {a} skip the {b} part", "ask who owns {a} before buying more {b}",
    "{a} metrics look great until you check {b}", "the {a} logs had the {b} answer all along",
)
TAILS = ("", "Thanks for posting.", "Curious what others think.", "Learned that the hard way.",
         "Would read a longer write-up.", "Great thread.", "Same here.", "Cheers.")
SHORT_REPLIES = ("Great post, thanks!", "Agreed.", "This.", "So true", "Nice!", "100%", "Ha!", "Good one",
                 "Thanks Ron", "Interesting", "Bookmarked", "Yep", "+1", "Wow", "Great thread")
# Formulaic congratulations. The normalized form of some is 30 characters or more.
CONGRATS = ("Congrats!", "Congrats Ron!", "Congratulations!", "Well deserved", "Nice one!",
            "Congratulations, well deserved!", "Congrats Ron, well deserved!",
            "Congratulations Ron, well deserved!", "Congratulations on the new role!",
            "Huge congrats, so well deserved!", "Congratulations, so happy for you!",
            "That is awesome news, congratulations!")
COMMON_SITES = ("github.com", "youtube.com", "youtu.be", "linkedin.com", "medium.com", "en.wikipedia.org",
                "docs.google.com", "arxiv.org", "substack.com", "reddit.com")
NEWS_SITES = ("bleepingcomputer.com", "krebsonsecurity.com", "therecord.media", "securityweek.com",
              "darkreading.com", "cisa.gov", "nvd.nist.gov", "attack.mitre.org", "arstechnica.com",
              "theregister.com", "schneier.com", "wired.com", "thehackernews.com", "404media.co")
HOUR_W = (3.0, 2.5, 2.0, 1.5, 1.0, 0.6, 0.4, 0.3, 0.3, 0.4, 0.6, 1.0, 1.5, 2.0, 2.6, 3.2, 3.6, 3.8, 3.8,
          3.8, 3.7, 3.6, 3.4, 3.2)  # UTC hours, a mostly US audience
# Look-alike letters (project hooks allow only ASCII source text here).
CYR = {"a": chr(0x430), "e": chr(0x435), "o": chr(0x43E), "p": chr(0x440), "c": chr(0x441), "i": chr(0x456)}
GRK = {"o": chr(0x3BF), "a": chr(0x3B1), "i": chr(0x3B9), "n": chr(0x3B7)}


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# -- data classes ----------------------------------------------------------------


@dataclass
class Truth:
    """A planted group. ``positive`` groups should be found; negative groups are near-threshold traps."""

    detector: str
    members: list[str]
    variant: str
    positive: bool


@dataclass
class Scenario:
    name: str
    seed: int
    holdout: bool
    n_contacts: int
    profiles: list[Profile]
    interactions: list[Interaction]
    posts: list[dict[str, Any]]
    truth: list[Truth]
    background: set[str]
    roles: dict[str, str]
    benign_groups: list[set[str]]
    known: list[str]
    history: list[Profile]
    seeds: list[str]

    @property
    def planted(self) -> set[str]:
        return {u for t in self.truth for u in t.members}


@dataclass
class RunResult:
    findings: list[dict[str, Any]]
    states: dict[str, str]
    rules: dict[str, set[str]]
    summary: dict[str, Any]
    seconds: float = 0.0


# -- generator ---------------------------------------------------------------------


class Gen:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.profiles: dict[str, Profile] = {}
        self.items: list[Interaction] = []
        self.posts: dict[str, dict[str, Any]] = {}
        self.originals: list[str] = []
        self.author: dict[str, str] = {}
        self.n_uid = 0
        self.n_post = 0
        self.handles: set[str] = set()
        self.roles: dict[str, str] = {}
        self.background: set[str] = set()
        self.benign_groups: list[set[str]] = []
        self.speed: dict[str, float] = {}
        self.history: list[Profile] = []
        self.seeds: list[str] = []

    # ids and time
    def new_uid(self) -> str:
        self.n_uid += 1
        return str(5_000_000 + self.n_uid)

    def new_pid(self, t: datetime) -> str:
        self.n_post += 1
        return f"{int(t.timestamp())}{self.n_post:07d}"

    def daytime(self, day: datetime) -> datetime:
        h = self.rng.choices(range(24), weights=HOUR_W)[0]
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return base + timedelta(hours=h, seconds=self.rng.uniform(0, 3600))

    def any_time(self, lo: datetime = START, hi: datetime = NOW) -> datetime:
        lo = max(lo, START - timedelta(days=3650))
        for _ in range(20):
            day = lo + timedelta(days=self.rng.uniform(0, max(0.01, (hi - lo).total_seconds() / 86400.0)))
            t = self.daytime(day)
            if lo <= t < hi:
                return t
        return lo + (hi - lo) / 2

    # profiles
    def unique_handle(self, h: str) -> str:
        base = h[:15]
        out = base
        n = 1
        while out.casefold() in self.handles:
            n += 1
            out = (base[:13] + str(n))[:15]
        self.handles.add(out.casefold())
        return out

    def person_handle(self, first: str, last: str) -> str:
        r = self.rng
        style = r.choices(["fl", "f_l", "il", "fl2", "w", "wsec", "0x", "gen", "lf"],
                          weights=[20, 15, 12, 10, 12, 8, 5, 6, 12])[0]
        if style == "fl":
            h = first + last
        elif style == "f_l":
            h = first + "_" + last
        elif style == "il":
            h = first[0] + last
        elif style == "fl2":
            h = first + last + str(r.randint(10, 99))
        elif style == "w":
            h = r.choice(WORDS) + r.choice(WORDS)
        elif style == "wsec":
            h = r.choice(WORDS) + r.choice(["sec", "_sec", "ops", "_ir", "labs"])
        elif style == "0x":
            h = "0x" + r.choice(WORDS)
        elif style == "gen":
            h = first.title() + str(r.randint(10_000_000, 99_999_999))
        else:
            h = last + first[0]
        if r.random() < 0.4 and "_" not in h:
            h = h.title()
        return self.unique_handle(h)

    def display_name(self, first: str, last: str, handle: str) -> str:
        r = self.rng
        k = r.choices(["person", "first", "nick", "handle", "tagged"], weights=[58, 10, 15, 10, 7])[0]
        if k == "person":
            return f"{first.title()} {last.title()}"
        if k == "first":
            return first.title()
        if k == "nick":
            return f"{r.choice(WORDS).title()} {r.choice(WORDS).title()}"
        if k == "handle":
            return handle
        return f"{first.title()} {last.title()} | {r.choice(['DFIR', 'AppSec', 'CTI', 'Red Team', 'GRC'])}"

    def add_profile(self, uid: str, *, handle: str, name: str | None, age_days: float, followers: int,
                    following: int, posts_per_day: float | None = None, tweets: int | None = None,
                    listed: int | None = None, verified: str = "none", bio: str | None = None,
                    url: str | None = None, default_image: bool = False, created: datetime | None = None) -> None:
        r = self.rng
        created = created or (NOW - timedelta(days=age_days))
        age = max(1.0, (NOW - created).total_seconds() / 86400.0)
        if tweets is None:
            tweets = int(round((posts_per_day if posts_per_day is not None else 1.0) * age))
        if listed is None:
            listed = int(followers * r.uniform(0.002, 0.03))
        if bio is None:
            bio = (f"{r.choice(['Security', 'Detection', 'IR', 'AppSec', 'Cloud', 'Network'])} "
                   f"{r.choice(['engineer', 'lead', 'researcher', 'analyst', 'nerd'])}. Views my own.")
        if url is None:
            url = f"https://{handle.lower()}.dev" if r.random() < 0.4 else ""
        vals: dict[str, Any] = {
            "handle": handle,
            "name": name,
            "description": bio,
            "url": url,
            "location": r.choice(["", "Seattle", "Austin", "Berlin", "London", "Toronto", "Remote", "Earth"]),
            "created_at": iso(created),
            "followers_count": int(followers),
            "following_count": int(following),
            "tweet_count": int(tweets),
            "listed_count": int(listed),
            "verified_type": verified,
            "protected": False,
            "profile_image_url": ("https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png"
                                  if default_image else f"https://pbs.twimg.com/profile_images/{uid}/a.jpg"),
        }
        self.profiles[uid] = Profile(
            user_id=uid, fetched_at=FETCHED, source_endpoint="/2/users", adapter="xapi", adapter_version="1",
            field_status=status_for(vals, PROFILE_OPTIONAL_FIELDS), **{k: vals[k] for k in PROFILE_OPTIONAL_FIELDS},
        )

    def normal_contact(self, role: str = "contact", age_days: float | None = None) -> str:
        r = self.rng
        uid = self.new_uid()
        first, last = r.choice(FIRST), r.choice(LAST)
        handle = self.person_handle(first, last)
        if age_days is None:
            band = r.choices([(730, 7000), (180, 730), (30, 180), (3, 30)], weights=[80, 12, 6, 2])[0]
            age_days = r.uniform(*band)
        followers = int(math.exp(r.gauss(math.log(300), 1.6)))
        following = min(7500, int(math.exp(r.gauss(math.log(400), 1.0))))
        rate = math.exp(r.gauss(math.log(1.5), 1.2))
        verified = r.choices(["none", "blue", "business"], weights=[86, 13, 1])[0]
        self.add_profile(uid, handle=handle, name=self.display_name(first, last, handle), age_days=age_days,
                         followers=followers, following=following, posts_per_day=rate, verified=verified,
                         bio="" if r.random() < 0.1 else None, default_image=r.random() < 0.03)
        self.roles[uid] = role
        self.speed[uid] = math.exp(r.gauss(0.0, 0.7))
        return uid

    # posts and interactions
    def _interaction(self, kind: str, src: str, tgt: str, pid: str | None, tpid: str | None, t: datetime | None,
                     text: str | None, urls: list[str] | None, endpoint: str) -> None:
        vals = {
            "source_post_id": pid, "target_post_id": tpid,
            "interaction_created_at": iso(t) if t is not None else None,
            "conversation_id": tpid or pid, "text": text, "urls": urls,
        }
        self.items.append(Interaction(
            type=kind, source_user_id=src, target_user_id=tgt, observed_at=FETCHED, source_endpoint=endpoint,
            adapter="validation", adapter_version="1", run_id="run_val",
            field_status=status_for(vals, INTERACTION_OPTIONAL_FIELDS), **vals,
        ))

    def owner_post(self, t: datetime, reply_to: tuple[str, str] | None = None, mentions: tuple[str, ...] = (),
                   impressions: int | None = None) -> str:
        """An owner post. ``reply_to`` is (user, post) for an owner reply."""
        r = self.rng
        pid = self.new_pid(t)
        if impressions is None:
            impressions = int(math.exp(r.gauss(math.log(600 if reply_to is None else 120), 0.9)))
        self.posts[pid] = {
            "id": pid, "author_id": EGO, "created_at": iso(t), "conversation_id": pid, "text": "owner post",
            "public_metrics": {"impression_count": impressions, "reply_count": 0, "like_count": 0},
            "referenced_tweets": [{"type": "replied_to", "id": reply_to[1]}] if reply_to else None,
        }
        self.author[pid] = EGO
        if reply_to is None:
            self.originals.append(pid)
        covered = {EGO}
        if reply_to is not None:
            self._interaction("reply", EGO, reply_to[0], pid, reply_to[1], t, "owner reply text", [],
                              "/2/users/:id/tweets")
            covered.add(reply_to[0])
        for m in mentions:
            if m not in covered:
                covered.add(m)
                self._interaction("mention", EGO, m, pid, None, t, "owner post text", [], "/2/users/:id/tweets")
        return pid

    def post(self, src: str, t: datetime, *, reply_to: str | None = None, quote: str | None = None,
             mentions: tuple[str, ...] = (), text: str | None = None, urls: list[str] | None = None) -> str:
        """A post by ``src`` that the collector sees. ``reply_to`` and ``quote`` are post IDs.

        A post with no reply and no quote mentions the owner (the collector
        finds it through the owner's mentions).
        """
        pid = self.new_pid(t)
        self.author[pid] = src
        text = text if text is not None else self.text()
        urls = list(urls) if urls is not None else self.links(src)
        covered = {src}
        endpoint = "/2/users/:id/mentions"
        if reply_to is not None:
            tgt = self.author[reply_to]
            if tgt not in covered:
                self._interaction("reply", src, tgt, pid, reply_to, t, text, urls, endpoint)
                covered.add(tgt)
                if tgt == EGO:
                    self.posts[reply_to]["public_metrics"]["reply_count"] += 1
        if quote is not None:
            tgt = self.author[quote]
            if tgt not in covered:
                self._interaction("quote", src, tgt, pid, quote, t, text, urls, "/2/tweets/:id/quote_tweets")
                covered.add(tgt)
        targets = (EGO, *mentions) if reply_to is None and quote is None else mentions
        for m in targets:
            if m not in covered:
                covered.add(m)
                self._interaction("mention", src, m, pid, None, t, text, urls, endpoint)
        return pid

    def rename(self, uid: str, versions: list[tuple[float, str | None, str | None]]) -> None:
        """Give ``uid`` earlier profile fetches. ``versions``: (days before NOW, handle, name), oldest first.

        Each earlier fetch is a copy of the current profile with that handle and
        display name (None keeps the current value). The current profile stays
        the last fetch.
        """
        cur = self.profiles[uid]
        for days, h, n in versions:
            self.history.append(replace(cur, fetched_at=iso(NOW - timedelta(days=days)),
                                        handle=h if h is not None else cur.handle,
                                        name=n if n is not None else cur.name))

    def seed_post(self, src: str, t: datetime, targets: list[str], kind: str) -> None:
        """A post from the timeline of seed ``src`` (step 6). ``kind``: reply, quote, or mention.

        A reply or a quote goes to ``targets[0]``, to a post of that account that
        the collector did not read (only its ID is known). The other targets are
        mentions.
        """
        pid = self.new_pid(t)
        self.author[pid] = src
        text, urls = self.text(), self.links(src)
        rest = targets
        if kind in ("reply", "quote") and targets:
            tpid = self.new_pid(t - timedelta(minutes=self.rng.uniform(5, 600)))
            self._interaction(kind, src, targets[0], pid, tpid, t, text, urls, "/2/users/:id/tweets")
            rest = targets[1:]
        for m in dict.fromkeys(rest):
            if m != src and m != (targets[0] if kind in ("reply", "quote") and targets else None):
                self._interaction("mention", src, m, pid, None, t, text, urls, "/2/users/:id/tweets")

    def repost(self, src: str, owner_pid: str) -> None:
        self._interaction("repost", src, EGO, None, owner_pid, None, None, None, "/2/tweets/:id/retweeted_by")

    def text(self) -> str:
        r = self.rng
        if r.random() < 0.25:
            return r.choice(SHORT_REPLIES)
        a, b = r.sample(TOPICS, 2)
        s = f"{r.choice(OPENERS)}, {r.choice(BODIES).format(a=a, b=b)}. {r.choice(TAILS)}".strip()
        return s if r.random() < 0.5 else s.lower()

    def links(self, src: str) -> list[str]:
        r = self.rng
        if r.random() > 0.12:
            return []
        k = r.random()
        if k < 0.5:
            site = r.choice(COMMON_SITES)
            return [f"https://{site}/{r.choice(WORDS)}/{r.choice(WORDS)}{r.randint(1, 999)}"]
        if k < 0.8:
            site = r.choice(NEWS_SITES)
            return [f"https://{site}/news/{r.choice(TOPICS)}-{r.choice(WORDS)}-{r.randint(1000, 99999)}"]
        h = str(self.profiles[src].handle or "x").lower()
        return [f"https://blog.{h}.net/posts/{r.choice(TOPICS)}-{r.randint(1, 300)}"]

    def latency(self, uid: str, viral: bool) -> float:
        med = (8 if viral else 40) * 60.0 * self.speed.get(uid, 1.0)
        return max(20.0, math.exp(self.rng.gauss(math.log(med), 1.3)))

    def pick_original(self, after: datetime | None = None, before: datetime | None = None) -> str:
        cands = [p for p in self.originals
                 if (after is None or self.t(p) >= after) and (before is None or self.t(p) < before)]
        cands = cands or self.originals
        w = [self.posts[p]["public_metrics"]["impression_count"] ** 0.6 for p in cands]
        return self.rng.choices(cands, weights=w)[0]

    def t(self, pid: str) -> datetime:
        return parse(self.posts[pid]["created_at"])

    def created(self, uid: str) -> datetime:
        return parse(str(self.profiles[uid].created_at))

    def last_time(self) -> datetime:
        return parse(str(self.items[-1].interaction_created_at))

    def reply_to_owner(self, uid: str, pid: str | None = None, latency: float | None = None, **kw: Any) -> str | None:
        pid = pid or self.pick_original(after=self.created(uid))
        viral = self.posts[pid]["public_metrics"]["impression_count"] > 8000
        t = self.t(pid) + timedelta(seconds=latency if latency is not None else self.latency(uid, viral))
        if t >= NOW or t < self.created(uid):
            return None
        return self.post(uid, t, reply_to=pid, **kw)


# -- background ------------------------------------------------------------------------


def _top_known(g: Gen, count: int = 10) -> list[str]:
    """The owner's top contacts as patterns.known_accounts finds them (build weights, owner interaction)."""
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for i in g.items:
        if i.source_user_id != i.target_user_id:
            counts[(i.source_user_id, i.target_user_id)][i.type] += 1
    alpha = {k: v for k, v in WEIGHTS.items() if k != "reciprocity_factor"}
    w = {k: edge_weight(dict(v), alpha) for k, v in counts.items()}
    ego_w: dict[str, float] = defaultdict(float)
    for (s, t), ws in w.items():
        back = w.get((t, s), 0.0)
        pw = (ws + back) * WEIGHTS["reciprocity_factor"] if back > 0 else ws
        if s == EGO:
            ego_w[t] = max(ego_w[t], pw)
        elif t == EGO:
            ego_w[s] = max(ego_w[s], pw)
    owner_to = {i.target_user_id for i in g.items if i.source_user_id == EGO and i.type in ("reply", "quote", "mention")}
    ranked = sorted(ego_w.items(), key=lambda kv: (-kv[1], kv[0]))
    return [u for u, _ in ranked if u in owner_to and u in g.profiles][:count]


def background(g: Gen, n: int) -> None:
    r = g.rng
    g.add_profile(EGO, handle=EGO_HANDLE, name=EGO_NAME, age_days=6200, followers=max(800, n * 6),
                  following=900, posts_per_day=4.0, listed=120, url="https://example.org")
    # Owner original posts: about one each day in the daytime. 3 % are viral; 3 are announcements.
    for d in range(WINDOW_DAYS):
        day = START + timedelta(days=d)
        for _ in range(_poisson(r, 1.1)):
            t = g.daytime(day)
            if START <= t < NOW - timedelta(hours=2):
                imp = int(math.exp(r.gauss(math.log(600), 0.9))) * (40 if r.random() < 0.03 else 1)
                g.owner_post(t, impressions=imp)
    announce = sorted(r.sample(g.originals, 3))  # a list: set order changes with the hash seed
    for pid in announce:
        # A post that gets a wave of replies has the reach to show them.
        g.posts[pid]["public_metrics"]["impression_count"] *= 10

    contacts = [g.normal_contact() for _ in range(n)]
    g.background.update(contacts)
    episodes = {u: min(40, 1 + int(r.paretovariate(2.0))) for u in contacts}
    core = set(sorted(contacts, key=lambda u: -episodes[u])[: max(5, n // 20)])

    for u in contacts:
        for _ in range(episodes[u]):
            k = r.random()
            if k < 0.52:
                pid = g.pick_original(after=g.created(u))
                if pid in announce:
                    txt = r.choice(CONGRATS) if r.random() < 0.85 else None
                    g.reply_to_owner(u, pid, text=txt)
                    continue
                rp = g.reply_to_owner(u, pid)
                if rp is not None and r.random() < (0.4 if u in core else 0.08):
                    _conversation(g, u, rp)
            elif k < 0.72:
                g.post(u, g.any_time(max(START, g.created(u))))
            elif k < 0.80:
                pid = g.pick_original(after=g.created(u))
                t = g.t(pid) + timedelta(seconds=math.exp(r.gauss(math.log(7200), 1.2)))
                if g.created(u) <= t < NOW:
                    g.post(u, t, quote=pid)
            elif k < 0.90:
                g.repost(u, g.pick_original())
            else:
                # The owner starts: a post that mentions the contact, sometimes answered.
                t = g.any_time()
                op = g.owner_post(t, mentions=(u,))
                if r.random() < 0.5 and g.created(u) <= t:
                    t2 = t + timedelta(seconds=g.latency(u, False))
                    if t2 < NOW:
                        g.post(u, t2, reply_to=op)
    # Replies to owner announcement posts: a wave of congratulations from more contacts.
    for pid in announce:
        for u in r.sample(contacts, min(len(contacts), max(8, n // 40))):
            if g.created(u) < g.t(pid):
                g.reply_to_owner(u, pid, text=r.choice(CONGRATS))
    # News events: 2 to 4 contacts share the same article within hours.
    for _ in range(max(3, n // 300)):
        url = f"https://{r.choice(NEWS_SITES)}/news/{r.choice(TOPICS)}-breach-{r.randint(10000, 99999)}"
        t0 = g.any_time(START, NOW - timedelta(days=2))
        for u in r.sample(contacts, r.randint(2, 4)):
            t = t0 + timedelta(hours=r.uniform(0, 30))
            if g.created(u) < t < NOW:
                g.post(u, t, urls=[url], text=g.text())
    # Colleague groups: mutual mentions in posts that also mention the owner.
    for _ in range(max(2, n // 120)):
        grp = r.sample(contacts, r.randint(3, 6))
        g.benign_groups.append(set(grp))
        for u in grp:
            g.roles[u] = "colleague"
            for _ in range(r.randint(2, 5)):
                others = tuple(r.sample([x for x in grp if x != u], r.randint(1, len(grp) - 1)))
                g.post(u, g.any_time(max(START, g.created(u))), mentions=others)
    # Fans: fast humans with notifications on (1.5 to 10 minutes).
    for _ in range(max(2, n // 200)):
        u = r.choice(contacts)
        g.roles[u] = "fan"
        for pid in r.sample(g.originals, min(len(g.originals), r.randint(4, 9))):
            g.reply_to_owner(u, pid, latency=r.uniform(90, 600))
    # Personal and work accounts of one person: same display name, both old.
    for _ in range(max(1, n // 250)):
        u = r.choice(contacts)
        p = g.profiles[u]
        uid = g.new_uid()
        handle = g.unique_handle((str(p.handle) + r.choice(["_work", "_corp", "_alt", "IR"]))[:15])
        g.add_profile(uid, handle=handle, name=p.name, age_days=r.uniform(900, 4000),
                      followers=int(math.exp(r.gauss(math.log(150), 1.0))), following=200, posts_per_day=0.3)
        g.roles[uid] = "work_pair"
        g.roles[u] = "work_pair"
        g.background.add(uid)
        g.speed[uid] = 1.0
        for _ in range(r.randint(1, 3)):
            g.reply_to_owner(uid)
    # Benign profile changes: 3 % of contacts changed the handle or the display name once.
    for u in r.sample(contacts, max(2, n * 3 // 100)):
        p = g.profiles[u]
        if r.random() < 0.5:
            g.rename(u, [(r.uniform(20, 80), g.unique_handle(r.choice(WORDS) + r.choice(LAST)), None)])
        else:
            g.rename(u, [(r.uniform(20, 80), None, str(p.name).split(" | ")[0] + " | " + r.choice(["DFIR", "CTI", "IR"]))])
    # Unusual but benign accounts.
    for k in range(max(5, n // 100)):
        kind = ("aggregator", "large", "lurker", "newuser", "org")[k % 5]
        uid = g.new_uid()
        g.speed[uid] = 1.0
        if kind == "aggregator":
            g.add_profile(uid, handle=g.unique_handle(r.choice(WORDS) + "newsfeed"), name="Sec News Feed",
                          age_days=r.uniform(800, 3000), followers=r.randint(20_000, 200_000), following=40,
                          posts_per_day=r.uniform(150, 400))
        elif kind == "large":
            g.add_profile(uid, handle=g.unique_handle(r.choice(FIRST) + r.choice(LAST) + "x"), name="Famous Person",
                          age_days=r.uniform(3000, 6000), followers=r.randint(1_000_000, 5_000_000), following=300,
                          posts_per_day=8, verified="blue")
        elif kind == "lurker":
            g.add_profile(uid, handle=g.unique_handle(r.choice(WORDS) + r.choice(WORDS) + "1"),
                          name=r.choice(FIRST).title(), age_days=r.uniform(400, 3000), followers=r.randint(2, 20),
                          following=r.randint(1500, 4000), posts_per_day=0.05, bio="")
        elif kind == "newuser":
            g.add_profile(uid, handle=g.unique_handle(r.choice(FIRST) + "_" + r.choice(LAST)), name="New Person",
                          age_days=r.uniform(5, 25), followers=r.randint(5, 40), following=r.randint(50, 200),
                          posts_per_day=3)
        else:
            g.add_profile(uid, handle=g.unique_handle(r.choice(WORDS) + "security"),
                          name=f"{r.choice(WORDS).title()} Security", age_days=r.uniform(1500, 5000),
                          followers=r.randint(5000, 50000), following=500, posts_per_day=2, verified="business")
        g.roles[uid] = "unusual_" + kind
        g.background.add(uid)
        for _ in range(r.randint(1, 3)):
            g.reply_to_owner(uid)
    seed_timelines(g, n)


def seed_timelines(g: Gen, n: int) -> None:
    """Step 6: 30 days of posts of the top ``SEED_COUNT`` contacts, in ``SCENES`` scenes of other accounts."""
    r = g.rng
    ranked = [u for u in _top_known(g, 10 ** 6) if u in g.background and g.roles.get(u) != "work_pair"]
    seeds = ranked[:SEED_COUNT]
    g.seeds = list(seeds)
    if not seeds:
        return
    contacts = sorted(u for u in g.background if g.roles.get(u) in ("contact", "colleague", "fan"))
    outsiders = [g.normal_contact(role="outsider") for _ in range(min(600, n))]
    g.background.update(outsiders)
    pools: list[list[str]] = [[] for _ in range(SCENES)]
    for k, u in enumerate(outsiders):
        pools[k % SCENES].append(u)
    for k in range(SCENES):
        pools[k] += r.sample(contacts, min(len(contacts), 10))
    lo = NOW - timedelta(days=SEED_DAYS)
    for k, s in enumerate(seeds):
        pool = pools[k % SCENES]
        # Each seed talks mostly to its own part of the scene.
        core = r.sample(pool, min(len(pool), r.randint(15, 40)))
        for _ in range(r.randint(15, 50)):
            t = g.any_time(max(lo, g.created(s)))
            x = r.random()
            if x < 0.5:
                g.seed_post(s, t, [r.choice(core)] + r.sample(core, r.randint(0, 2)), "reply")
            elif x < 0.8:
                g.seed_post(s, t, r.sample(core, r.randint(1, 3)), "mention")
            elif x < 0.9:
                g.seed_post(s, t, [r.choice(core)], "quote")
            else:
                other = pools[(k + r.randint(1, SCENES - 1)) % SCENES]
                g.seed_post(s, t, [r.choice(other)], "mention")


def _conversation(g: Gen, u: str, their_post: str) -> None:
    """The owner answers a reply; 40 % of the time the contact answers back fast (a live conversation)."""
    r = g.rng
    t = g.last_time() + timedelta(seconds=math.exp(r.gauss(math.log(1800), 1.0)))
    if t >= NOW:
        return
    op = g.owner_post(t, reply_to=(u, their_post))
    for _ in range(r.randint(1, 3)):
        fast = r.random() < 0.4
        t2 = t + timedelta(seconds=r.uniform(10, 90) if fast else g.latency(u, False))
        if t2 >= NOW:
            return
        their = g.post(u, t2, reply_to=op)
        if r.random() < 0.5:
            return
        t = t2 + timedelta(seconds=math.exp(r.gauss(math.log(900), 1.0)))
        if t >= NOW:
            return
        op = g.owner_post(t, reply_to=(u, their))


def _poisson(r: random.Random, lam: float) -> int:
    k, p, lim = 0, 1.0, math.exp(-lam)
    while True:
        p *= r.random()
        if p <= lim:
            return k
        k += 1


# -- planted campaigns --------------------------------------------------------------------


def _sock(g: Gen, *, age_days: float | None = None, created: datetime | None = None, style: str = "gen",
          established: bool = False, role: str = "planted") -> str:
    """A campaign account. ``established`` gives an old, normal-looking (bought or taken over) account."""
    r = g.rng
    uid = g.new_uid()
    first = r.choice(FIRST)
    if style == "gen":
        h = first.title() + str(r.randint(10_000_000, 99_999_999))
    else:
        h = r.choice(WORDS) + r.choice(WORDS) + str(r.randint(100, 999))
    h = g.unique_handle(h)
    if established:
        g.add_profile(uid, handle=h, name=f"{first.title()} {r.choice(LAST).title()}",
                      age_days=age_days or r.uniform(1500, 4000), created=created,
                      followers=int(math.exp(r.gauss(math.log(400), 0.8))),
                      following=int(math.exp(r.gauss(math.log(500), 0.6))), posts_per_day=1.0)
    else:
        g.add_profile(uid, handle=h, name=first.title(), age_days=age_days or 60.0, created=created,
                      followers=r.randint(0, 60), following=r.randint(20, 900), posts_per_day=r.uniform(0.5, 30),
                      bio=r.choice(["", "Crypto | Trading | Freedom", "Just here to learn"]),
                      default_image=r.random() < 0.4, url="")
    g.roles[uid] = role
    g.speed[uid] = 1.0
    return uid


def _spread_ages(r: random.Random, n: int, lo: float, hi: float, gap: float) -> list[float]:
    """n ages in [lo, hi] days, each at least ``gap`` days from the others (so no creation cluster)."""
    out: list[float] = []
    for _ in range(2000):
        if len(out) == n:
            break
        a = r.uniform(lo, hi)
        if all(abs(a - b) >= gap for b in out):
            out.append(a)
    return out


def plant_reply_burst(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []

    def burst(members: list[str], pid: str | None, start: datetime, width_s: float, kinds: tuple[str, ...]) -> None:
        for u in members:
            t = start + timedelta(seconds=r.uniform(0, width_s))
            kind = r.choice(kinds)
            if kind == "reply" and pid:
                g.post(u, t, reply_to=pid)
            elif kind == "quote" and pid:
                g.post(u, t, quote=pid)
            else:
                g.post(u, t)

    if not holdout:
        n = r.randint(4, 7)
        ms = [_sock(g, age_days=a) for a in _spread_ages(r, n, 15, 175, 6)]
        pid = g.pick_original(before=NOW - timedelta(days=1))
        burst(ms, pid, g.t(pid) + timedelta(minutes=r.uniform(1, 30)), r.uniform(180, 720), ("reply",))
        out.append(Truth("reply_burst", ms, "young_4to7_reply", True))
        n = r.randint(8, 12)
        ages = _spread_ages(r, n, 15, 175, 6)
        ms = [_sock(g, age_days=a) if k % 2 == 0 else _sock(g, established=True, age_days=r.uniform(1500, 5000))
              for k, a in enumerate(ages)]
        pid = g.pick_original(before=NOW - timedelta(days=1))
        burst(ms, pid, g.t(pid) + timedelta(minutes=r.uniform(5, 120)), r.uniform(300, 720), ("reply",))
        out.append(Truth("reply_burst", ms, "mixed_8to12_reply", True))
    else:
        n = r.randint(5, 9)
        ms = [_sock(g, age_days=a, style="w") for a in _spread_ages(r, n, 10, 170, 5)]
        burst(ms, None, g.any_time(START + timedelta(days=5), NOW - timedelta(days=1)), r.uniform(120, 480), ("mention",))
        out.append(Truth("reply_burst", ms, "young_mentions_no_post", True))
        n = r.randint(4, 6)
        ms = [_sock(g, age_days=a, style="w") for a in _spread_ages(r, n, 10, 170, 8)]
        pid = g.pick_original(before=NOW - timedelta(days=6))
        burst(ms, pid, g.t(pid) + timedelta(days=r.uniform(2, 5)), r.uniform(600, 840), ("reply", "quote"))
        out.append(Truth("reply_burst", ms, "late_burst_reply_quote", True))
    # Hard negatives.
    ms = [_sock(g, age_days=a) for a in _spread_ages(r, 3, 15, 175, 10)]
    pid = g.pick_original(before=NOW - timedelta(days=1))
    burst(ms, pid, g.t(pid) + timedelta(minutes=r.uniform(1, 30)), 600, ("reply",))
    out.append(Truth("reply_burst", ms, "neg_three_accounts", False))
    ms = [_sock(g, age_days=a) for a in _spread_ages(r, 5, 15, 175, 10)]
    pid = g.pick_original(before=NOW - timedelta(days=1))
    t = g.t(pid) + timedelta(minutes=2)
    for u in ms:
        t += timedelta(minutes=r.uniform(16.5, 25))
        g.post(u, t, reply_to=pid)
    out.append(Truth("reply_burst", ms, "neg_spread_over_an_hour", False))
    ms = [_sock(g, established=True) for _ in range(r.randint(4, 5))]
    pid = g.pick_original(before=NOW - timedelta(days=1))
    burst(ms, pid, g.t(pid) + timedelta(minutes=r.uniform(1, 10)), 600, ("reply",))
    out.append(Truth("reply_burst", ms, "neg_established_small_burst", False))
    return out


def _scatter(g: Gen, u: str, k: int, kinds: tuple[str, ...] = ("reply", "mention")) -> None:
    """k ordinary interactions with the owner at random times after the account was created."""
    r = g.rng
    lo = max(START, g.created(u) + timedelta(hours=1))
    for _ in range(k):
        kind = r.choice(kinds)
        if kind == "reply":
            pid = g.pick_original(after=lo)
            if g.t(pid) < lo:
                g.post(u, g.any_time(lo))
                continue
            if g.reply_to_owner(u, pid) is None:
                g.post(u, g.any_time(lo))
        elif kind == "quote":
            pid = g.pick_original(after=lo)
            t = max(lo, g.t(pid)) + timedelta(hours=r.uniform(1, 30))
            if t < NOW:
                g.post(u, t, quote=pid)
            else:
                g.post(u, g.any_time(lo))
        else:
            g.post(u, g.any_time(lo))


def plant_creation_cluster(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    if not holdout:
        k = r.randint(3, 6)
        base = NOW - timedelta(days=r.uniform(20, 300))
        ms = [_sock(g, created=base + timedelta(hours=r.uniform(0, 60))) for _ in range(k)]
        for u in ms:
            _scatter(g, u, r.randint(1, 3))
        out.append(Truth("creation_cluster", ms, "recent_3to6_in_60h", True))
    else:
        k = r.randint(4, 8)
        base = NOW - timedelta(days=r.uniform(300, 900))
        ms = [_sock(g, created=base + timedelta(hours=r.uniform(0, 20)), style="w") for _ in range(k)]
        for u in ms:
            _scatter(g, u, r.randint(1, 2), ("quote", "mention"))
        out.append(Truth("creation_cluster", ms, "older_4to8_in_20h", True))
    ms = [_sock(g, age_days=a) for a in _spread_ages(r, 3, 40, 200, 5)]
    for u in ms:
        _scatter(g, u, 1)
    out.append(Truth("creation_cluster", ms, "neg_five_days_apart", False))
    base = datetime(2010, 3, 1, tzinfo=UTC)
    ms = [_sock(g, created=base + timedelta(hours=r.uniform(0, 48)), established=True) for _ in range(3)]
    for u in ms:
        _scatter(g, u, 1)
    out.append(Truth("creation_cluster", ms, "neg_2010_sign_up_wave", False))
    return out


def plant_fast_reply(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    u = _sock(g, age_days=r.uniform(100, 2000), established=r.random() < 0.5)
    lo = max(START, g.created(u))
    posts = [p for p in g.originals if lo < g.t(p) < NOW - timedelta(hours=1)]
    if not holdout:
        for pid in r.sample(posts, r.randint(3, 8)):
            g.reply_to_owner(u, pid, latency=r.uniform(4, 45))
        out.append(Truth("fast_reply", [u], "auto_4to45s", True))
    else:
        for pid in r.sample(posts, r.randint(4, 10)):
            lat = r.uniform(20, 55) if r.random() < 0.7 else r.uniform(300, 1200)
            g.reply_to_owner(u, pid, latency=lat)
        out.append(Truth("fast_reply", [u], "auto_20to55s_with_slow_ones", True))
    # Hard negatives: a fast human fan; a live conversation; only two fast replies.
    f = g.normal_contact(role="planted", age_days=r.uniform(800, 4000))
    for pid in r.sample(posts, r.randint(4, 8)):
        g.reply_to_owner(f, pid, latency=r.uniform(80, 300))
    out.append(Truth("fast_reply", [f], "neg_fan_80to300s", False))
    c = g.normal_contact(role="planted", age_days=r.uniform(800, 4000))
    for pid in r.sample(posts, 5):
        rp = g.reply_to_owner(c, pid)
        if rp is None:
            continue
        t = g.last_time() + timedelta(minutes=r.uniform(5, 60))
        if t < NOW - timedelta(minutes=5):
            op = g.owner_post(t, reply_to=(c, rp))
            g.post(c, t + timedelta(seconds=r.uniform(10, 40)), reply_to=op)
    out.append(Truth("fast_reply", [c], "neg_live_conversation", False))
    v = _sock(g, age_days=r.uniform(100, 2000))
    vposts = [p for p in posts if g.t(p) > g.created(v)]
    for pid in r.sample(vposts, 2):
        g.reply_to_owner(v, pid, latency=r.uniform(4, 30))
    out.append(Truth("fast_reply", [v], "neg_two_fast_replies", False))
    return out


PROMO_TEXT = ("I lost access to my account and this team got it back in hours, contact them now for help",
              "Huge opportunity here, I made back my losses in one week with this trading signal group",
              "Your account was flagged for review, verify your details at the link below to keep it")
NEG_PROMO_TEXT = "Limited seats left for the free webinar on cloud incident response, register with the link"
PROMO_SITES = ("acct-restore-help.com", "quickgains-signal.io", "verify-support-center.net",
               "wallet-fix-desk.org", "promo-drop-zone.xyz", "secure-login-review.com")


def _vary(r: random.Random, text: str) -> str:
    """The same text with other case, punctuation, and @handles (normalize_text removes these)."""
    t = text if r.random() < 0.5 else text.upper() if r.random() < 0.3 else text.capitalize()
    if r.random() < 0.6:
        t = "@" + r.choice(WORDS) + " " + t
    return t + r.choice(["", "!", "!!", ".", " ..."])


def plant_shared_content(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    if not holdout:
        text = r.choice(PROMO_TEXT)
        ms = [_sock(g, age_days=a) for a in _spread_ages(r, r.randint(3, 6), 20, 900, 5)]
        t0 = g.any_time(START + timedelta(days=2), NOW - timedelta(days=1))
        for u in ms:
            pid = g.pick_original(before=t0)
            g.post(u, t0 + timedelta(hours=r.uniform(0, 6)), reply_to=pid, text=_vary(r, text), urls=[])
        out.append(Truth("shared_content", ms, "same_text_within_6h", True))
        site = r.choice(PROMO_SITES)
        ms = [_sock(g, age_days=a) for a in _spread_ages(r, r.randint(3, 5), 20, 900, 5)]
        t0 = g.any_time(START + timedelta(days=2), NOW - timedelta(days=1))
        for u in ms:
            url = f"https://www.{site}/claim?utm_source={r.choice(WORDS)}&ref=x"
            g.post(u, t0 + timedelta(minutes=r.uniform(0, 120)), text=g.text(), urls=[url])
        out.append(Truth("shared_content", ms, "same_link_within_2h", True))
    else:
        site = r.choice(PROMO_SITES)
        ms = [_sock(g, age_days=a, style="w") for a in _spread_ages(r, r.randint(3, 5), 20, 900, 5)]
        t0 = g.any_time(START + timedelta(days=5), NOW - timedelta(days=4))
        for u in ms:
            url = f"https://{site}/{r.choice(WORDS)}/{r.randint(100, 999)}"
            g.post(u, t0 + timedelta(hours=r.uniform(0, 60)), text=g.text(), urls=[url])
        out.append(Truth("shared_content", ms, "same_site_paths_within_3d", True))
        text = r.choice(PROMO_TEXT)
        ms = [_sock(g, age_days=a, style="w") for a in _spread_ages(r, r.randint(4, 7), 20, 900, 5)]
        t0 = g.any_time(START + timedelta(days=5), NOW - timedelta(days=5))
        for u in ms:
            if r.random() < 0.5:
                g.post(u, t0 + timedelta(hours=r.uniform(0, 96)), text=_vary(r, text), urls=[])
            else:
                pid = g.pick_original(before=t0)
                g.post(u, t0 + timedelta(hours=r.uniform(0, 96)), reply_to=pid, text=_vary(r, text), urls=[])
        out.append(Truth("shared_content", ms, "same_text_over_4d_mixed", True))
    # Hard negatives: two accounts with one text; four accounts with links to a common site.
    # The text is not one of the campaign texts, so the pair cannot join a campaign group.
    text = NEG_PROMO_TEXT
    ms = [_sock(g, age_days=a) for a in _spread_ages(r, 2, 20, 900, 5)]
    for u in ms:
        g.post(u, g.any_time(), text=_vary(r, text), urls=[])
    out.append(Truth("shared_content", ms, "neg_two_accounts", False))
    ms = [g.normal_contact(role="planted", age_days=r.uniform(800, 4000)) for _ in range(4)]
    t0 = g.any_time(START + timedelta(days=2), NOW - timedelta(days=1))
    for u in ms:
        g.post(u, t0 + timedelta(minutes=r.uniform(0, 90)), text=g.text(),
               urls=[f"https://github.com/{r.choice(WORDS)}/{r.choice(WORDS)}"])
    out.append(Truth("shared_content", ms, "neg_common_site", False))
    return out


def _swap(text: str, table: dict[str, str], n: int, r: random.Random) -> str:
    idx = [i for i, ch in enumerate(text) if ch.lower() in table]
    for i in r.sample(idx, min(n, len(idx))):
        text = text[:i] + table[text[i].lower()] + text[i + 1:]
    return text


def plant_lookalike(g: Gen, holdout: bool, known: list[str]) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    target = None
    for k in known:
        if len(str(g.profiles[k].handle).replace("_", "")) >= 9:
            target = k
            break
    target = target or (known[0] if known else None)

    def imp(handle: str, name: str | None, variant: str) -> None:
        uid = g.new_uid()
        g.add_profile(uid, handle=g.unique_handle(handle), name=name, age_days=r.uniform(3, 120),
                      followers=r.randint(0, 300), following=r.randint(50, 900), posts_per_day=r.uniform(0.5, 10),
                      bio="", url="")
        g.roles[uid] = "planted"
        g.speed[uid] = 1.0
        for _ in range(r.randint(1, 2)):
            g.post(uid, g.any_time(max(START, g.created(uid))))
        out.append(Truth("lookalike", [uid], variant, True))

    if not holdout:
        imp("Ron_Dilley", "Ron Dilley", "owner_underscore")
        imp(r.choice(["rdilley_sec", "ron_d_sec", "dilleyron"]), _swap(EGO_NAME, CYR, 1, r), "owner_name_cyrillic")
        imp("rondilley_", "Ron", "owner_trailing_separator")
    else:
        imp("r0ndilley", "Ron Dilley", "owner_digit_zero")
        imp("RonDiIley", None, "owner_capital_i")
        imp(r.choice(["ron_official_x", "rd_support_desk"]), _swap(EGO_NAME, GRK, 2, r), "owner_name_greek")
        imp("rondillley", "R. Dilley", "owner_double_letter")
    if target is not None:
        th = str(g.profiles[target].handle)
        tn = g.profiles[target].name
        if not holdout:
            alt = th.replace("_", "") if "_" in th else th[: len(th) // 2] + "_" + th[len(th) // 2:]
            imp(alt, tn, "known_separator")
            if tn and len(str(tn).replace(" ", "")) >= 8:
                imp(r.choice(WORDS) + "_" + r.choice(WORDS), _swap(str(tn), CYR, 1, r), "known_name_cyrillic")
        else:
            digits = {"o": "0", "l": "1", "i": "1", "e": "3", "a": "4", "s": "5"}
            imp(_swap(th, digits, 1, r), None, "known_digit_swap")
            if len(th) >= 9:
                j = r.randrange(1, len(th) - 1)
                imp(th[:j] + th[j] + th[j:], tn, "known_double_letter")
    # Hard negatives: another person named Ron, a short similar handle, a fan account.
    for h, n in (("ron_miller", "Ron Miller"), ("rondi", "Rondi"), ("dilleyfan", "Fan of security talks")):
        uid = g.new_uid()
        g.add_profile(uid, handle=g.unique_handle(h), name=n, age_days=r.uniform(900, 4000), followers=300,
                      following=400, posts_per_day=1.0)
        g.roles[uid] = "planted"
        g.speed[uid] = 1.0
        g.post(uid, g.any_time())
        out.append(Truth("lookalike", [uid], "neg_" + h, False))
    return out


def plant_profile_outliers(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []

    def acc(variant: str, positive: bool, **kw: Any) -> None:
        uid = g.new_uid()
        g.add_profile(uid, handle=g.unique_handle(r.choice(WORDS) + r.choice(WORDS) + str(r.randint(10, 99))),
                      name=r.choice(FIRST).title(), **kw)
        g.roles[uid] = "planted"
        g.speed[uid] = 1.0
        g.post(uid, g.any_time(max(START, g.created(uid) + timedelta(hours=1))))
        out.append(Truth("profile_outliers", [uid], variant, positive))

    if not holdout:
        acc("follow_ratio_extreme", True, age_days=r.uniform(400, 1000), followers=r.randint(0, 3),
            following=r.randint(2000, 5000), posts_per_day=1.0)
        acc("posts_per_day_extreme", True, age_days=r.uniform(200, 1000), followers=300, following=300,
            posts_per_day=r.uniform(400, 1500))
        acc("age_days_1to5", True, age_days=r.uniform(1, 5), followers=5, following=50, posts_per_day=5)
    else:
        acc("follow_ratio_strong", True, age_days=r.uniform(400, 1000), followers=r.randint(1, 8),
            following=r.randint(800, 1500), posts_per_day=1.0)
        acc("posts_per_day_200to350", True, age_days=r.uniform(200, 1000), followers=300, following=300,
            posts_per_day=r.uniform(200, 350))
        acc("age_days_2to10", True, age_days=r.uniform(2, 10), followers=30, following=120, posts_per_day=4)
    acc("neg_ratio_moderate", False, age_days=1500, followers=60, following=900, posts_per_day=1.0)
    acc("neg_posts_25_per_day", False, age_days=1500, followers=400, following=300, posts_per_day=25)
    acc("neg_age_120_days", False, age_days=120, followers=80, following=200, posts_per_day=2)
    return out


def plant_one_way(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    if not holdout:
        u = g.normal_contact(role="planted", age_days=r.uniform(200, 3000))
        t0 = NOW - timedelta(days=r.uniform(30, 85))
        for _ in range(r.randint(6, 15)):
            t = g.any_time(t0, NOW)
            if r.random() < 0.5:
                g.post(u, t)
            else:
                g.post(u, t, reply_to=g.pick_original(before=t))
        out.append(Truth("one_way", [u], "unanswered_6to15", True))
    else:
        u = _sock(g, age_days=r.uniform(40, 300))
        t0 = NOW - timedelta(days=r.uniform(12, 30))
        for _ in range(r.randint(5, 7)):
            t = g.any_time(t0, t0 + timedelta(days=10))
            if r.random() < 0.5:
                g.post(u, t)
            else:
                g.post(u, t, quote=g.pick_original(before=t))
        out.append(Truth("one_way", [u], "unanswered_5to7_quotes", True))
    u = g.normal_contact(role="planted", age_days=1500)
    for _ in range(4):
        g.post(u, g.any_time())
    out.append(Truth("one_way", [u], "neg_four_contacts", False))
    u = g.normal_contact(role="planted", age_days=1500)
    last = ""
    for _ in range(8):
        last = g.post(u, g.any_time(START, NOW - timedelta(days=2)))
    g.owner_post(min(NOW - timedelta(hours=1), g.last_time() + timedelta(hours=2)), reply_to=(u, last))
    out.append(Truth("one_way", [u], "neg_owner_answered_once", False))
    return out


def plant_tight_group(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    if not holdout:
        ms = [_sock(g, age_days=a) for a in _spread_ages(r, r.randint(4, 6), 30, 200, 5)]
        for u in ms:
            for _ in range(r.randint(2, 4)):
                others = [x for x in ms if x != u]
                g.post(u, g.any_time(max(START, g.created(u))), mentions=tuple(r.sample(others, r.randint(2, len(others)))))
        # Close the ring: each pair in both directions.
        for a in ms:
            for b in ms:
                if a != b and not any(i.source_user_id == a and i.target_user_id == b for i in g.items):
                    g.post(a, g.any_time(max(START, g.created(a))), mentions=(b,))
        out.append(Truth("tight_group", ms, "ring_4to6_mentions", True))
    else:
        ms = [_sock(g, age_days=a, style="w") for a in _spread_ages(r, r.randint(3, 4), 30, 400, 5)]
        seeds = {u: g.post(u, g.any_time(max(START, g.created(u)), NOW - timedelta(days=3))) for u in ms}
        for a in ms:
            for b in ms:
                if a != b:
                    t = g.t_of(seeds[b]) + timedelta(minutes=r.uniform(5, 600))
                    g.post(a, min(NOW - timedelta(minutes=1), t), reply_to=seeds[b], mentions=(EGO,))
        out.append(Truth("tight_group", ms, "ring_3to4_replies", True))
    ms = [_sock(g, age_days=a) for a in _spread_ages(r, 4, 30, 400, 5)]
    for k, a in enumerate(ms):
        g.post(a, g.any_time(max(START, g.created(a))), mentions=(ms[(k + 1) % 4],))
    out.append(Truth("tight_group", ms, "neg_one_way_chain", False))
    return out


# -- network detectors (score/network.py) ------------------------------------------------


def _owner_posts(g: Gen, k: int) -> list[str]:
    """k different owner original posts, at least one day old, spread over the window."""
    cands = sorted(p for p in g.originals if g.t(p) < NOW - timedelta(days=1))
    return sorted(g.rng.sample(cands, min(k, len(cands))))


def _sync(g: Gen, members: list[str], posts: list[str], spread_s: float, kinds: tuple[str, ...] = ("reply",),
          per_post: int | None = None) -> None:
    """On each post, ``per_post`` (default: all) members act within ``spread_s`` seconds of one random offset."""
    r = g.rng
    for pid in posts:
        t0 = g.t(pid) + timedelta(minutes=r.uniform(20, 480))
        group = members if per_post is None else r.sample(members, per_post)
        for u in group:
            t = t0 + timedelta(seconds=r.uniform(0, spread_s))
            if t >= NOW:
                continue
            if r.choice(kinds) == "quote":
                g.post(u, t, quote=pid)
            else:
                g.post(u, t, reply_to=pid)


def plant_co_action(g: Gen, holdout: bool) -> list[Truth]:
    out: list[Truth] = []
    if not holdout:
        ms = [_sock(g, established=True) for _ in range(3)]
        _sync(g, ms, _owner_posts(g, 9), 240)
        out.append(Truth("co_action", ms, "trio_sync_reply", True))
        ms = [_sock(g, established=True) for _ in range(4)]
        _sync(g, ms, _owner_posts(g, 7), 360, ("reply", "quote"))
        out.append(Truth("co_action", ms, "quad_sync_mixed", True))
    else:
        ms = [_sock(g, established=True, style="w") for _ in range(2)]
        _sync(g, ms, _owner_posts(g, 6), 300, ("reply", "quote"))
        out.append(Truth("co_action", ms, "pair_sync", True))
        ms = [_sock(g, established=True, style="w") for _ in range(5)]
        _sync(g, ms, _owner_posts(g, 12), 480, per_post=3)
        out.append(Truth("co_action", ms, "rotating_five", True))
    # Hard negatives: the same posts at their own times, and only two synchronized posts.
    ms = [_sock(g, established=True) for _ in range(3)]
    for pid in _owner_posts(g, 8):
        for u in ms:
            g.reply_to_owner(u, pid)
    out.append(Truth("co_action", ms, "neg_same_posts_own_time", False))
    ms = [_sock(g, established=True) for _ in range(3)]
    _sync(g, ms, _owner_posts(g, 2), 240)
    for u in ms:
        for pid in _owner_posts(g, 3):
            g.reply_to_owner(u, pid)
    out.append(Truth("co_action", ms, "neg_two_sync_posts", False))
    return out


def _victims(g: Gen, k: int) -> list[str]:
    pool = sorted(u for u in g.background if g.roles.get(u) == "contact")
    return g.rng.sample(pool, min(k, len(pool)))


def plant_mass_tagging(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []

    def tagger(u: str, posts: int, lo: int, hi: int, kind: str) -> None:
        for _ in range(posts):
            tags = tuple(_victims(g, r.randint(lo, hi)))
            if kind == "mention":
                g.post(u, g.any_time(max(START, g.created(u))), mentions=tags)
            elif kind == "quote":
                pid = g.pick_original(after=g.created(u))
                g.post(u, min(NOW - timedelta(minutes=1), g.t(pid) + timedelta(hours=r.uniform(1, 30))),
                       quote=pid, mentions=tags)
            else:
                g.reply_to_owner(u, mentions=tags)

    if not holdout:
        u = _sock(g, age_days=r.uniform(20, 150))
        tagger(u, 5, 6, 9, "mention")
        out.append(Truth("mass_tagging", [u], "young_tagger_mentions", True))
        u = _sock(g, age_days=r.uniform(20, 150))
        tagger(u, 4, 5, 8, "reply")
        out.append(Truth("mass_tagging", [u], "young_tagger_replies", True))
    else:
        u = _sock(g, established=True, style="w")
        tagger(u, 4, 4, 6, "quote")
        out.append(Truth("mass_tagging", [u], "established_quote_tagger", True))
        u = _sock(g, age_days=r.uniform(20, 150), style="w")
        tagger(u, 3, 4, 5, "mention")
        out.append(Truth("mass_tagging", [u], "small_tagger", True))
    # Hard negatives: the same 6 accounts in each post, and a group of friends who know each other.
    u = _sock(g, established=True)
    same = tuple(_victims(g, 6))
    for _ in range(4):
        g.post(u, g.any_time(max(START, g.created(u))), mentions=same)
    out.append(Truth("mass_tagging", [u], "neg_event_retag", False))
    friends = [_sock(g, established=True) for _ in range(8)]
    for a in friends:
        for _ in range(2):
            g.post(a, g.any_time(max(START, g.created(a))), mentions=tuple(r.sample([x for x in friends if x != a], 2)))
    u = _sock(g, established=True)
    for _ in range(3):
        g.post(u, g.any_time(max(START, g.created(u))), mentions=tuple(r.sample(friends, 5)))
    out.append(Truth("mass_tagging", [u], "neg_linked_friends", False))
    return out


def _near(r: random.Random, h: str) -> str:
    """A handle one edit away from ``h`` (a double letter, a swap of two letters, or a digit)."""
    k = r.randrange(3)
    if k == 0:
        i = r.randrange(len(h))
        return (h[:i + 1] + h[i] + h[i + 1:])[:15]
    if k == 1 and len(h) > 3:
        i = r.randrange(1, len(h) - 2)
        return h[:i] + h[i + 1] + h[i] + h[i + 2:]
    return (h + "1")[:15]


def plant_profile_change(g: Gen, holdout: bool, known: list[str]) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    refs = [u for u in known if g.profiles[u].handle and len(str(g.profiles[u].handle)) >= 6] or known

    def takeover(ref: str, steps: int, copy_handle: bool = True) -> str:
        u = _sock(g, established=True, age_days=r.uniform(1500, 4000), style="w")
        p = g.profiles[u]
        rp = g.profiles[ref]
        old_h, old_n = p.handle, p.name
        new_h = g.unique_handle(_near(r, str(rp.handle))) if copy_handle else str(old_h)
        g.profiles[u] = replace(p, handle=new_h, name=rp.name)
        vers: list[tuple[float, str | None, str | None]] = [(r.uniform(40, 80), old_h, old_n)]
        if steps == 2:
            vers.append((r.uniform(10, 30), g.unique_handle(r.choice(WORDS) + r.choice(WORDS)), r.choice(FIRST).title()))
        g.rename(u, vers)
        for _ in range(r.randint(1, 3)):
            g.reply_to_owner(u)
        return u

    if not holdout:
        u = takeover(refs[0], 1)
        out.append(Truth("profile_change", [u], "takeover_near_known", True))
        u = _sock(g, established=True, age_days=r.uniform(2000, 4000), style="w")
        p = g.profiles[u]
        g.profiles[u] = replace(p, handle=g.unique_handle("RonDiIley"), name=EGO_NAME)
        g.rename(u, [(r.uniform(30, 70), p.handle, p.name)])
        g.reply_to_owner(u)
        out.append(Truth("profile_change", [u], "takeover_owner", True))
    else:
        u = takeover(refs[1 % len(refs)], 2)
        out.append(Truth("profile_change", [u], "takeover_two_step", True))
        u = takeover(refs[2 % len(refs)], 1, copy_handle=False)
        out.append(Truth("profile_change", [u], "name_copy_only", True))
        u = _sock(g, established=True, style="w")
        g.rename(u, [(70, g.unique_handle(r.choice(WORDS) + str(r.randint(10, 99))), None),
                     (45, g.unique_handle(r.choice(WORDS) + r.choice(WORDS)), None),
                     (20, g.unique_handle(r.choice(FIRST) + r.choice(LAST)), None)])
        g.reply_to_owner(u)
        out.append(Truth("profile_change", [u], "churn_four_handles", True))
    # Hard negatives: one unrelated new handle, and a job title added to the name.
    u = g.normal_contact(age_days=r.uniform(900, 4000))
    g.rename(u, [(r.uniform(20, 60), g.unique_handle(r.choice(WORDS) + r.choice(LAST)), None)])
    g.reply_to_owner(u)
    out.append(Truth("profile_change", [u], "neg_unrelated_rename", False))
    u = g.normal_contact(age_days=r.uniform(900, 4000))
    plain = str(g.profiles[u].name).split(" | ")[0]
    g.rename(u, [(r.uniform(20, 60), None, plain)])
    g.profiles[u] = replace(g.profiles[u], name=plain + " | DFIR")
    g.reply_to_owner(u)
    out.append(Truth("profile_change", [u], "neg_title_tweak", False))
    return out


def _linked_group(g: Gen, ms: list[str], posts: tuple[int, int], tags: tuple[int, int]) -> None:
    """Members mention each other (and the owner) in their posts."""
    r = g.rng
    for a in ms:
        for _ in range(r.randint(*posts)):
            others = [x for x in ms if x != a]
            g.post(a, g.any_time(max(START, g.created(a) + timedelta(hours=1))),
                   mentions=tuple(r.sample(others, min(len(others), r.randint(*tags)))))


def plant_community_anomaly(g: Gen, holdout: bool) -> list[Truth]:
    r = g.rng
    out: list[Truth] = []
    if not holdout:
        base = r.uniform(40, 60)
        ms = [_sock(g, age_days=base + r.uniform(0, 10)) for _ in range(12)]
        _linked_group(g, ms, (2, 3), (2, 3))
        out.append(Truth("community_anomaly", ms, "farm_batch_10d", True))
    else:
        base = r.uniform(120, 160)
        ms = [_sock(g, established=True, age_days=base + r.uniform(0, 25), style="w") for _ in range(10)]
        _linked_group(g, ms, (2, 3), (1, 2))
        out.append(Truth("community_anomaly", ms, "farm_spread_25d", True))
        base = r.uniform(300, 330)
        ms = [_sock(g, age_days=base + r.uniform(0, 15), style="w") for _ in range(8)]
        roots = {u: g.post(u, g.any_time(max(START, g.created(u)), NOW - timedelta(days=3))) for u in ms}
        for a in ms:
            for b in r.sample([x for x in ms if x != a], 3):
                t = g.t_of(roots[b]) + timedelta(minutes=r.uniform(5, 900))
                g.post(a, min(NOW - timedelta(minutes=1), t), reply_to=roots[b], mentions=(EGO,))
        out.append(Truth("community_anomaly", ms, "farm_replies_15d", True))
    # Hard negatives: an old team, and a class of newer accounts with spread creation dates.
    ms = [g.normal_contact(age_days=a) for a in _spread_ages(r, 10, 1500, 5000, 60)]
    _linked_group(g, ms, (2, 3), (2, 3))
    out.append(Truth("community_anomaly", ms, "neg_old_team", False))
    ms = [g.normal_contact(age_days=a) for a in _spread_ages(r, 12, 200, 900, 45)]
    _linked_group(g, ms, (2, 3), (2, 3))
    out.append(Truth("community_anomaly", ms, "neg_class_spread", False))
    return out


def _t_of(self: Gen, pid: str) -> datetime:
    """The time of any post that the generator made (owner or other account)."""
    if pid in self.posts:
        return self.t(pid)
    for i in reversed(self.items):
        if i.source_post_id == pid and i.interaction_created_at:
            return parse(i.interaction_created_at)
    raise KeyError(pid)


Gen.t_of = _t_of  # type: ignore[attr-defined]

PLANTERS = {
    "reply_burst": plant_reply_burst,
    "creation_cluster": plant_creation_cluster,
    "fast_reply": plant_fast_reply,
    "shared_content": plant_shared_content,
    "profile_outliers": plant_profile_outliers,
    "one_way": plant_one_way,
    "tight_group": plant_tight_group,
    "co_action": plant_co_action,
    "mass_tagging": plant_mass_tagging,
    "community_anomaly": plant_community_anomaly,
}


def build_scenario(seed: int, n_contacts: int, campaigns: tuple[str, ...] | None = DETECTORS,
                   holdout: bool = False) -> Scenario:
    """Background for ``n_contacts`` contacts, plus the planted ``campaigns`` (None or () for background only)."""
    g = Gen(seed)
    background(g, n_contacts)
    known = _top_known(g)
    truth: list[Truth] = []
    for name in campaigns or ():
        if name == "lookalike":
            truth.extend(plant_lookalike(g, holdout, known))
        elif name == "profile_change":
            truth.extend(plant_profile_change(g, holdout, known))
        else:
            truth.extend(PLANTERS[name](g, holdout))
    label = "holdout" if holdout else "tune"
    return Scenario(
        name=f"{label}-s{seed}-n{n_contacts}" + ("" if campaigns else "-bg"),
        seed=seed, holdout=holdout, n_contacts=n_contacts,
        profiles=list(g.profiles.values()), interactions=g.items, posts=list(g.posts.values()),
        truth=truth, background=set(g.background), roles=dict(g.roles), benign_groups=list(g.benign_groups),
        known=known, history=sorted(g.history, key=lambda p: p.fetched_at), seeds=list(g.seeds),
    )


# -- run ------------------------------------------------------------------------------------


def run_scenario(scn: Scenario, workdir: Path, ruleset: RuleSet | None = None) -> RunResult:
    """Fill a new store with the scenario, run the real build and score stages, and read the output."""
    workdir.mkdir(parents=True, exist_ok=True)
    store = Store(workdir / "store.sqlite")
    try:
        store.set_meta("ego_user_id", EGO)
        store.set_meta("ego_handle", EGO_HANDLE)
        sid = store.begin_stage("collect", "run_val", {})
        store.finish_stage(sid, {})
        with store.tx() as c:
            store.put_profiles(scn.history, conn=c)  # earlier fetches first (profile_history)
            store.put_profiles(scn.profiles, conn=c)
            store.put_interactions(scn.interactions, conn=c)
            for p in scn.posts:
                store.put_post(p, "/2/users/:id/tweets", "run_val", conn=c)
        cfg = Config(root=workdir, raw={"weights": dict(WEIGHTS), "data": {"dir": str(workdir)}})
        log = NullLog()
        run_build(cfg, store, log, "run_val")
        t0 = time.perf_counter()
        summary = run_score(cfg, store, log, ruleset=ruleset or load_rules(RULES_PATH))
        secs = time.perf_counter() - t0
        findings = []
        for row in store.q("SELECT * FROM findings ORDER BY finding_id"):
            d = dict(row)
            d["members"] = json.loads(d["members_json"])
            d["evidence"] = json.loads(d["evidence_json"])
            findings.append(d)
        states = {r["user_id"]: r["state"] for r in store.q("SELECT user_id, state FROM node_states")}
        rules: dict[str, set[str]] = defaultdict(set)
        for r in store.q("SELECT user_id, rule_id FROM rule_results"):
            rules[r["user_id"]].add(r["rule_id"])
        return RunResult(findings, states, dict(rules), summary, secs)
    finally:
        store.close()


# -- evaluation ----------------------------------------------------------------------------


def flagged_accounts(res: RunResult, detector: str) -> set[str]:
    """Accounts that a detector flags at its detection level. For a lookalike, only the lookalike side."""
    lvl = SEV_RANK[DETECT_LEVEL[detector]]
    out: set[str] = set()
    for f in res.findings:
        if f["detector"] != detector or SEV_RANK[f["severity"]] < lvl:
            continue
        if detector == "lookalike":
            out.add(f["evidence"]["lookalike"])
        elif detector == "profile_change":
            out.add(f["members"][0])
        else:
            out.update(f["members"])
    return out


def evaluate(scn: Scenario, res: RunResult) -> dict[str, Any]:
    """Per detector: true positives, misses, and false positives on background and hard negatives."""
    planted_all = scn.planted
    out: dict[str, Any] = {"detectors": {}, "states": {}}
    for det in DETECTORS:
        flagged = flagged_accounts(res, det)
        pos = {u for t in scn.truth if t.detector == det and t.positive for u in t.members}
        neg = {u for t in scn.truth if t.detector == det and not t.positive for u in t.members}
        tp = flagged & pos
        fp_bg = flagged & scn.background
        fp_neg = flagged & neg
        other = flagged & (planted_all - pos - neg)
        missed = sorted({t.variant for t in scn.truth if t.detector == det and t.positive
                         and not (set(t.members) & flagged)})
        neg_hits = sorted({t.variant for t in scn.truth if t.detector == det and not t.positive
                           and set(t.members) & flagged})
        lvl = SEV_RANK[DETECT_LEVEL[det]]
        n_find = sum(1 for f in res.findings if f["detector"] == det and SEV_RANK[f["severity"]] >= lvl)
        n_all = sum(1 for f in res.findings if f["detector"] == det)
        out["detectors"][det] = {
            "pos": len(pos), "tp": len(tp), "fp_bg": len(fp_bg), "fp_neg": len(fp_neg), "other": len(other),
            "findings": n_find, "findings_all": n_all, "missed": missed, "neg_hits": neg_hits,
            "fp_bg_roles": sorted(scn.roles.get(u, "?") for u in fp_bg),
        }
    bg = [u for u in scn.background if u in res.states]
    out["states"]["background"] = _state_counts(res, bg)
    coord = {u for t in scn.truth if t.positive and t.detector in COORDINATION for u in t.members}
    out["states"]["coordination_members"] = _state_counts(res, coord)
    with_rule = 0
    for t in scn.truth:
        if t.positive and t.detector in COORDINATION:
            ids = set(MEMBER_RULES[t.detector].split("|"))
            with_rule += sum(1 for u in t.members if res.rules.get(u, set()) & ids)
    out["states"]["coordination_members_with_rule"] = with_rule
    out["states"]["coordination_members_total"] = sum(len(t.members) for t in scn.truth
                                                      if t.positive and t.detector in COORDINATION)
    flagged_bg = [u for u in bg if res.states[u] in ("some_signals", "many_signals")]
    out["states"]["background_flagged_rules"] = _rule_counts(res, flagged_bg)
    return out


def _state_counts(res: RunResult, users: Any) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for u in users:
        c[res.states.get(u, "absent")] += 1
    return dict(c)


def _rule_counts(res: RunResult, users: list[str]) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for u in users:
        for rid in res.rules.get(u, set()):
            c[rid] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def merge(evals: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the counts of several evaluations."""
    out: dict[str, Any] = {"detectors": {}, "states": {}}
    for det in DETECTORS:
        acc: dict[str, Any] = {"pos": 0, "tp": 0, "fp_bg": 0, "fp_neg": 0, "other": 0, "findings": 0,
                               "findings_all": 0, "missed": [], "neg_hits": [], "fp_bg_roles": []}
        for e in evals:
            d = e["detectors"][det]
            for k in ("pos", "tp", "fp_bg", "fp_neg", "other", "findings", "findings_all"):
                acc[k] += d[k]
            for k in ("missed", "neg_hits", "fp_bg_roles"):
                acc[k] += d[k]
        out["detectors"][det] = acc
    for key in ("background", "coordination_members", "background_flagged_rules"):
        acc2: dict[str, int] = defaultdict(int)
        for e in evals:
            for k, v in e["states"][key].items():
                acc2[k] += v
        out["states"][key] = dict(acc2)
    for key in ("coordination_members_with_rule", "coordination_members_total"):
        out["states"][key] = sum(e["states"][key] for e in evals)
    return out


def recall(d: dict[str, Any]) -> float | None:
    return None if d["pos"] == 0 else d["tp"] / d["pos"]


def precision(d: dict[str, Any]) -> float | None:
    n = d["tp"] + d["fp_bg"] + d["fp_neg"]
    return None if n == 0 else d["tp"] / n
