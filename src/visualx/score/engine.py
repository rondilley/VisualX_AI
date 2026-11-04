"""Signal engine: rules as configuration, signal types as code (ARCHITECTURE AD-08).

``config/rules.yaml`` holds the rule rows and the state bands. Each rule names
a signal function from ``signals.py``. The engine evaluates the rules for each
account, writes ``rule_results``, ``rule_skips``, and ``node_states``, and
records a ``stage_runs`` row for the stage ``score``.

State computation (FR-31, FR-35)::

    a gate rule fires                                   -> not_scored
    an owner override exists                            -> override state
    a hard rule fires                                   -> many_signals
    total >= many_min and a rule of a listed family fired
      and the listed families give many_signals_min_family_points -> many_signals
    total >= some_min                                   -> some_signals
    else                                                -> no_signals

The listed families for ``many_signals`` can be only ``behavior`` and
``impersonation``, so profile rules alone never give ``many_signals``.

Before the rules, the stage runs the network-level pattern detectors
(``patterns.py``). They write the table ``findings`` and give the context for
the cross-account rules (B02, I02, B08, B09 to B12).
"""

from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ..config import Config, ConfigError
from ..logs import RunLog
from ..model import SIGNAL_STATES, dumps
from ..store import Store
from .patterns import (
    DETECTOR_INPUTS,
    PatternConfig,
    PatternConfigError,
    PatternResult,
    finding_row,
    load_pattern_data,
    pattern_config,
    run_patterns,
)
from .signals import SIGNALS, Account, Inputs, MissingInput, SignalDef, Value

FAMILIES: tuple[str, ...] = ("gate", "profile", "trust", "behavior", "impersonation")
MANY_FAMILIES_ALLOWED: frozenset[str] = frozenset({"behavior", "impersonation"})
HARD_FAMILIES_ALLOWED: frozenset[str] = frozenset({"behavior", "impersonation"})
OPS: tuple[str, ...] = ("lt", "lte", "gt", "gte", "eq", "ne", "in")
RULE_KEYS: frozenset[str] = frozenset(
    {"id", "family", "signal", "when", "points", "escalate", "hard", "cause", "params", "note"}
)
ESCALATE_KEYS: frozenset[str] = frozenset({"when", "points", "signal"})
V01_RULE_IDS: frozenset[str] = frozenset(
    {"N01", "N02", "N03", "P01", "P03", "P05", "P06", "P07", "T01", "T02", "T03",
     "B01", "B03", "B04", "B05", "B07", "I01"}
)
# v0.2 (FR-56): the starter rules P02, P04, B02, B08, I02, I03 (B06 has no threat
# feed with known terms yet), B09 to B12 from the pattern layer (patterns.py), and
# B13, B14, I04 from the network detectors (network.py).
V02_RULE_IDS: frozenset[str] = V01_RULE_IDS | frozenset(
    {"P02", "P04", "B02", "B06", "B08", "I02", "I03", "B09", "B10", "B11", "B12", "B13", "B14", "I04"}
)
TOP_KEYS: frozenset[str] = frozenset({"bands", "rules", "patterns"})

# FR-31, TSC-29: words that must not occur in state names, display text, or cause strings.
FORBIDDEN_WORDS: tuple[str, ...] = ("legit", "risky", "bot", "scammer")

# The cause row for an owner override (overrides.yaml). It has 0 points and no rule in rules.yaml.
OVERRIDE_RULE_ID = "OVERRIDE"
OVERRIDE_CAUSE = "You set this state in overrides.yaml"

# TR-06: sensitive categories (X Developer Policy). A rule must not use these
# as a field, a signal name, a parameter, or a keyword. The match is on whole
# words (and a plural with "s"). This check finds only the known terms; the
# owner's review of each new rule is the second control (AD-08).
SENSITIVE_TERMS: dict[str, tuple[str, ...]] = {
    "politics": (
        "politics", "political", "politician", "democrat", "democratic", "republican", "liberal",
        "conservative", "labour", "tory", "gop", "maga", "election", "vote", "voter", "left wing",
        "right wing", "socialist", "communist", "partisan", "party affiliation",
    ),
    "religion": (
        "religion", "religious", "church", "mosque", "synagogue", "temple", "muslim", "islam",
        "islamic", "jewish", "judaism", "jew", "christian", "christianity", "catholic", "protestant",
        "hindu", "hinduism", "buddhist", "buddhism", "sikh", "atheist", "bible", "quran", "torah",
        "god", "faith", "pray", "prayer",
    ),
    "health": (
        "health", "medical", "disease", "illness", "cancer", "hiv", "aids", "diabetes", "covid",
        "vaccine", "pregnant", "pregnancy", "disability", "disabled", "mental", "depression",
        "therapy", "diagnosis", "medication",
    ),
    "ethnicity": (
        "race", "racial", "ethnic", "ethnicity", "nationality", "hispanic", "latino", "latina",
        "asian", "african", "caucasian", "arab", "indigenous", "skin color", "racism", "racist",
    ),
    "sexuality": (
        "gay", "lesbian", "bisexual", "trans", "transgender", "queer", "lgbt", "lgbtq",
        "homosexual", "sexual", "sexuality", "sexual orientation", "gender identity", "pride",
    ),
    "trade_union": ("union", "trade union", "labor union", "labour union", "unionist", "syndicate"),
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RuleFileError(ConfigError):
    """The rule file breaks the rule contract or the policy controls."""


@dataclass(frozen=True)
class Escalation:
    when: dict[str, Any]
    points: float
    signal: str | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    family: str
    signal: str
    when: dict[str, Any]
    points: float
    cause: str
    hard: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    escalate: Escalation | None = None


@dataclass(frozen=True)
class Bands:
    some_signals_min: float
    many_signals_min: float
    many_signals_families: frozenset[str]
    # Groups of rules that rest on one fact: (name, rule IDs, max points of the group).
    # The points of the rules that fire in a group count up to the maximum only.
    overlap: tuple[tuple[str, frozenset[str], float], ...] = ()
    # many_signals also needs this many points from the many_signals families (FR-35).
    many_signals_min_family_points: float = 0.0


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    bands: Bands
    digest: str
    patterns: PatternConfig = field(default_factory=lambda: pattern_config({}))

    @property
    def gates(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.family == "gate")

    @property
    def scored(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.family != "gate")


# ---------------------------------------------------------------------------
# Policy checks (TR-06, TSC-17, TSC-29)
# ---------------------------------------------------------------------------


def _strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            out.extend(_strings(v))
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            out.extend(_strings(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def sensitive_hits(text: str) -> list[tuple[str, str]]:
    """(category, term) pairs for each sensitive term in ``text`` (whole words)."""
    tokens = re.findall(r"[^\W_]+", text.casefold())
    joined = " " + " ".join(tokens) + " "
    hits: list[tuple[str, str]] = []
    for cat, terms in SENSITIVE_TERMS.items():
        for term in terms:
            if f" {term} " in joined or f" {term}s " in joined:
                hits.append((cat, term))
    return hits


_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_WORDS) + r")s?\b", re.IGNORECASE)


def forbidden_word_hits(text: str) -> list[str]:
    """The forbidden words in ``text``, as whole words (a plural with "s" counts).

    A substring match found "bot" in "both" and "robotics". A hostname from X in
    a finding summary then stopped the score stage (review 20261103, B-02).
    """
    found = {m.group(1).casefold() for m in _FORBIDDEN_RE.finditer(text)}
    return [w for w in FORBIDDEN_WORDS if w in found]


def _rule_list(data: Any) -> list[Any]:
    if isinstance(data, dict):
        rules = data.get("rules", [])
    else:
        rules = data
    return rules if isinstance(rules, list) else []


def policy_errors(data: Any) -> list[str]:
    """Policy errors in rule-file data: no family, a sensitive input or keyword, or a forbidden word in a cause."""
    errs: list[str] = []
    for n, r in enumerate(_rule_list(data), 1):
        if not isinstance(r, dict):
            errs.append(f"rule #{n}: not a mapping")
            continue
        rid = str(r.get("id") or f"#{n}")
        fam = r.get("family")
        if not fam:
            errs.append(f"{rid}: rule has no signal family")
        elif fam not in FAMILIES:
            errs.append(f"{rid}: family {fam!r} is not one of {FAMILIES}")
        texts = _strings(r)
        sdef = SIGNALS.get(str(r.get("signal")))
        if sdef is not None:
            texts.extend(sdef.inputs)
        esc = r.get("escalate")
        if isinstance(esc, dict) and esc.get("signal") in SIGNALS:
            texts.extend(SIGNALS[esc["signal"]].inputs)
        seen: set[tuple[str, str]] = set()
        for t in texts:
            for hit in sensitive_hits(t):
                if hit not in seen:
                    seen.add(hit)
                    errs.append(f"{rid}: uses the sensitive-category term '{hit[1]}' ({hit[0]})")
        for w in forbidden_word_hits(str(r.get("cause", ""))):
            errs.append(f"{rid}: cause template contains the word '{w}'")
    if isinstance(data, dict) and data.get("patterns") is not None:
        errs.extend(pattern_policy_errors(data["patterns"]))
    return errs


def pattern_policy_errors(raw: Any) -> list[str]:
    """TR-06 for the ``patterns`` section: no sensitive-category term in a key, a value, or a detector input."""
    errs: list[str] = []
    texts = _strings(raw)
    dets = raw.get("detectors") if isinstance(raw, dict) else None
    if isinstance(dets, dict):
        for name in dets:
            texts.extend(DETECTOR_INPUTS.get(str(name), ()))
    seen: set[tuple[str, str]] = set()
    for t in texts:
        for hit in sensitive_hits(t):
            if hit not in seen:
                seen.add(hit)
                errs.append(f"patterns: uses the sensitive-category term '{hit[1]}' ({hit[0]})")
    return errs


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _check_when(rid: str, when: Any) -> dict[str, Any]:
    if not isinstance(when, dict) or not when:
        raise RuleFileError(f"{rid}: 'when' must be a mapping such as {{lt: 30}}")
    for op, arg in when.items():
        if op not in OPS:
            raise RuleFileError(f"{rid}: 'when' operator {op!r} is not one of {OPS}")
        if op == "in" and not isinstance(arg, list):
            raise RuleFileError(f"{rid}: 'when: in' needs a list")
        if op in ("lt", "lte", "gt", "gte") and (isinstance(arg, bool) or not isinstance(arg, (int, float))):
            raise RuleFileError(f"{rid}: 'when: {op}' needs a number")
    return dict(when)


def _check_points(rid: str, v: Any, what: str = "points") -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RuleFileError(f"{rid}: '{what}' must be a number")
    return float(v)


def _check_cause(rid: str, cause: Any) -> str:
    if not isinstance(cause, str) or not cause.strip():
        raise RuleFileError(f"{rid}: 'cause' must be a non-empty string")
    try:
        parsed = list(string.Formatter().parse(cause))
    except ValueError as e:
        raise RuleFileError(f"{rid}: cause template is not valid: {e}") from e
    for _, name, _, _ in parsed:
        if name is not None and not _IDENT.match(name):
            raise RuleFileError(f"{rid}: cause field {{{name}}} must be a plain name")
    return cause


def _check_bands(raw: Any) -> Bands:
    if not isinstance(raw, dict):
        raise RuleFileError("rules file: top-level 'bands' mapping is missing")
    for k in ("some_signals_min", "many_signals_min", "many_signals_families"):
        if k not in raw:
            raise RuleFileError(f"bands: '{k}' is missing")
    some = _check_points("bands", raw["some_signals_min"], "some_signals_min")
    many = _check_points("bands", raw["many_signals_min"], "many_signals_min")
    if many < some:
        raise RuleFileError("bands: many_signals_min must be at or above some_signals_min")
    fams = raw["many_signals_families"]
    if not isinstance(fams, list) or not fams:
        raise RuleFileError("bands: many_signals_families must be a non-empty list")
    bad = set(map(str, fams)) - MANY_FAMILIES_ALLOWED
    if bad:
        raise RuleFileError(
            f"bands: many_signals_families can contain only {sorted(MANY_FAMILIES_ALLOWED)} (FR-35), not {sorted(bad)}"
        )
    extra = set(raw) - {"some_signals_min", "many_signals_min", "many_signals_families", "overlap",
                        "many_signals_min_family_points"}
    if extra:
        raise RuleFileError(f"bands: unknown keys {sorted(extra)}")
    groups: list[tuple[str, frozenset[str], float]] = []
    seen: set[str] = set()
    ov = raw.get("overlap") or {}
    if not isinstance(ov, dict):
        raise RuleFileError("bands: 'overlap' must map a group name to {rules, max_points}")
    for name, g in ov.items():
        if not isinstance(g, dict) or set(g) != {"rules", "max_points"}:
            raise RuleFileError(f"bands.overlap.{name}: needs exactly 'rules' and 'max_points'")
        ids = g["rules"]
        if not isinstance(ids, list) or len(ids) < 2:
            raise RuleFileError(f"bands.overlap.{name}: 'rules' must list 2 or more rule IDs")
        both = seen & set(map(str, ids))
        if both:
            raise RuleFileError(f"bands.overlap.{name}: {sorted(both)} is already in another group")
        seen |= set(map(str, ids))
        groups.append((str(name), frozenset(map(str, ids)), _check_points(f"bands.overlap.{name}", g["max_points"],
                                                                       "max_points")))
    fam_min = _check_points("bands", raw.get("many_signals_min_family_points", 0), "many_signals_min_family_points")
    if fam_min > many:
        raise RuleFileError("bands: many_signals_min_family_points must be at or below many_signals_min")
    return Bands(some, many, frozenset(map(str, fams)), tuple(groups), fam_min)


def family_points(bands: Bands, hits: list["RuleHit"]) -> float:
    """Points of the many_signals families. An overlap group adds only its largest such rule:
    its rules rest on one fact, and one fact counts one time."""
    grouped: set[str] = set()
    out = 0.0
    for _, ids, _ in bands.overlap:
        pts = [h.points for h in hits if h.rule_id in ids and h.family in bands.many_signals_families]
        out += max(pts, default=0.0)
        grouped |= ids
    out += sum(h.points for h in hits if h.rule_id not in grouped and h.family in bands.many_signals_families)
    return out


def overlap_excess(bands: Bands, hits: list["RuleHit"]) -> float:
    """The points above the maximum of each overlap group. compute_state subtracts them."""
    out = 0.0
    for _, ids, cap in bands.overlap:
        g = sum(h.points for h in hits if h.rule_id in ids and h.family != "gate")
        if g > cap:
            out += g - cap
    return out


def ruleset_from_data(data: Any, source: str = "rules.yaml") -> RuleSet:
    """Check and build a RuleSet. Raises RuleFileError on any contract or policy error."""
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise RuleFileError(f"{source}: needs a top-level 'rules' list and a 'bands' mapping")
    errs = policy_errors(data)
    if errs:
        raise RuleFileError(f"{source}: policy errors: " + "; ".join(errs))
    extra_top = set(data) - TOP_KEYS
    if extra_top:
        raise RuleFileError(f"{source}: unknown top-level keys {sorted(extra_top)}")
    bands = _check_bands(data.get("bands"))
    try:
        pconf = pattern_config(data.get("patterns"))
    except PatternConfigError as e:
        raise RuleFileError(f"{source}: {e}") from e
    rules: list[Rule] = []
    ids: set[str] = set()
    for n, r in enumerate(data["rules"], 1):
        rid = str(r.get("id") or "")
        if not rid:
            raise RuleFileError(f"{source}: rule #{n} has no id")
        if rid in ids:
            raise RuleFileError(f"{source}: rule id {rid} occurs two times")
        ids.add(rid)
        extra = set(r) - RULE_KEYS
        if extra:
            raise RuleFileError(f"{rid}: unknown keys {sorted(extra)}")
        fam = str(r["family"])
        sig = str(r.get("signal") or "")
        if sig not in SIGNALS:
            raise RuleFileError(f"{rid}: signal {sig!r} is not a known signal function")
        when = _check_when(rid, r.get("when"))
        if fam == "gate":
            points = _check_points(rid, r.get("points", 0))
            if points != 0:
                raise RuleFileError(f"{rid}: a gate rule has no points")
        else:
            if "points" not in r:
                raise RuleFileError(f"{rid}: 'points' is missing")
            points = _check_points(rid, r["points"])
        hard = r.get("hard", False)
        if not isinstance(hard, bool):
            raise RuleFileError(f"{rid}: 'hard' must be true or false")
        if hard and fam not in HARD_FAMILIES_ALLOWED:
            raise RuleFileError(f"{rid}: only behavior or impersonation rules can be hard (FR-35)")
        params = r.get("params", {}) or {}
        if not isinstance(params, dict):
            raise RuleFileError(f"{rid}: 'params' must be a mapping")
        esc = None
        if r.get("escalate") is not None:
            e = r["escalate"]
            if not isinstance(e, dict) or set(e) - ESCALATE_KEYS or "when" not in e or "points" not in e:
                raise RuleFileError(f"{rid}: 'escalate' needs 'when' and 'points' (and an optional 'signal')")
            if fam == "gate":
                raise RuleFileError(f"{rid}: a gate rule cannot escalate")
            esig = e.get("signal")
            if esig is not None and esig not in SIGNALS:
                raise RuleFileError(f"{rid}: escalate signal {esig!r} is not a known signal function")
            esc = Escalation(_check_when(rid, e["when"]), _check_points(rid, e["points"], "escalate.points"), esig)
        rules.append(Rule(rid, fam, sig, when, points, _check_cause(rid, r.get("cause")), hard, dict(params), esc))
    for name, gids, _ in bands.overlap:
        unknown = gids - ids
        if unknown:
            raise RuleFileError(f"bands.overlap.{name}: unknown rule IDs {sorted(unknown)}")
    digest = hashlib.sha256(dumps(data).encode("utf-8")).hexdigest()[:16]
    return RuleSet(tuple(rules), bands, digest, pconf)


def load_rules(path: str | Path) -> RuleSet:
    p = Path(path)
    if not p.exists():
        raise RuleFileError(f"{p} not found")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    rs = ruleset_from_data(data, str(p))
    for rule in rs.rules:
        if "feed_file" in rule.params:
            load_feed(rule, p.parent)
    return rs


FEED_MAX_DOMAINS = 500_000
_FEED_HOST = re.compile(r"^[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$")


def load_feed(rule: Rule, base: Path) -> None:
    """Read the threat feed of a rule (B06, FR-56) into ``rule.params['domains']``.

    The file is one host name for each line. A "#" starts a comment. A line can also
    be a URL or a hosts-file line ("0.0.0.0 example.com"): the host is taken. If the
    file does not exist, the rule is off (``feed_missing``): it gives no result and no
    skip row for each account.
    The rule must record the feed terms (``feed_terms``) and keep the size limit
    (``max_domains``, at most FEED_MAX_DOMAINS).
    """
    path = Path(str(rule.params["feed_file"]))
    path = path if path.is_absolute() else base / path
    limit = int(rule.params.get("max_domains", FEED_MAX_DOMAINS))
    if not 1 <= limit <= FEED_MAX_DOMAINS:
        raise RuleFileError(f"{rule.id}: max_domains must be 1 to {FEED_MAX_DOMAINS}")
    if not path.is_file():
        rule.params["feed_missing"] = True
        return
    if not str(rule.params.get("feed_terms", "")).strip():
        raise RuleFileError(f"{rule.id}: the feed {path.name} has no feed_terms. Record the terms of use of the feed.")
    hosts: set[str] = set()
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.split("#", 1)[0].strip().lower()
            if not line:
                continue
            tok = line.split()[-1]
            if "://" in tok:
                tok = urlsplit(tok).hostname or ""
            tok = tok.rstrip(".")
            if tok.startswith("www."):
                tok = tok[4:]
            if _FEED_HOST.match(tok):
                hosts.add(tok)
            if len(hosts) > limit:
                raise RuleFileError(f"{rule.id}: the feed {path.name} has more than {limit} domains (max_domains)")
    rule.params["domains"] = frozenset(hosts)
    rule.params["feed_size"] = len(hosts)


def default_rules_path(cfg: Config) -> Path:
    """The rules.yaml of the configuration folder that was loaded (``--config``), review 20261102-225212."""
    if cfg.config_dir is not None:
        return Path(cfg.config_dir) / "rules.yaml"
    return cfg.root / "config" / "rules.yaml"


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def compare(value: Any, when: dict[str, Any], rid: str = "") -> bool:
    """True if ``value`` meets every condition in ``when``."""
    for op, arg in when.items():
        if op in ("lt", "lte", "gt", "gte"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuleFileError(f"{rid}: 'when: {op}' needs a numeric signal, but the value is {type(value).__name__}")
            ok = {"lt": value < arg, "lte": value <= arg, "gt": value > arg, "gte": value >= arg}[op]
        elif op == "eq":
            ok = value == arg
        elif op == "ne":
            ok = value != arg
        elif op == "in":
            ok = value in arg
        else:
            raise RuleFileError(f"{rid}: unknown operator {op!r}")
        if not ok:
            return False
    return True


def _plain(v: Any) -> Any:
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def render_cause(rule: Rule, val: Value, extra: dict[str, Any]) -> str:
    fields: dict[str, Any] = {**rule.params, **extra, **val.fields, "value": val.value}
    fields = {k: _plain(v) for k, v in fields.items()}
    # A field value can be text from X (for example the handle of a known account
    # in I02 and I04). FR-31 applies to the whole cause string, so a value with a
    # forbidden word does not go in; the value stays in value_json (review 20261103, B-08).
    fields = {k: ("[name not shown]" if isinstance(v, str) and forbidden_word_hits(v) else v)
              for k, v in fields.items()}
    try:
        return rule.cause.format_map(fields)
    except (KeyError, IndexError, ValueError) as e:
        raise RuleFileError(f"{rule.id}: cause template needs a field that the signal does not give: {e}") from e


@dataclass
class RuleHit:
    rule_id: str
    family: str
    points: float
    value_json: str
    cause: str
    hard: bool


@dataclass
class Skip:
    rule_id: str
    missing_field: str


@dataclass
class AccountResult:
    user_id: str
    state: str
    total: float
    hits: list[RuleHit]
    skips: list[Skip]
    override_note: str | None = None


def _call(sdef: SignalDef, account: Account, params: dict[str, Any]) -> Value:
    return sdef.fn(Inputs(account, sdef), params)


def evaluate_rule(rule: Rule, account: Account) -> tuple[RuleHit | None, list[Skip]]:
    """Evaluate one rule for one account. Returns (hit or None, skips)."""
    try:
        val = _call(SIGNALS[rule.signal], account, rule.params)
    except MissingInput as e:
        return None, [Skip(rule.id, e.field)]
    if not compare(val.value, rule.when, rule.id):
        return None, []
    skips: list[Skip] = []
    points = rule.points
    esc = rule.escalate
    if esc is not None:
        try:
            ev = val if esc.signal is None else _call(SIGNALS[esc.signal], account, rule.params)
        except MissingInput as e:
            skips.append(Skip(rule.id, e.field))
        else:
            if compare(ev.value, esc.when, rule.id):
                points = esc.points
    extra = {"ego_handle": (account.ego_handle or "").lstrip("@")}
    cause = render_cause(rule, val, extra)
    value_json = dumps({"value": val.value, **val.fields})
    return RuleHit(rule.id, rule.family, points, value_json, cause, rule.hard), skips


def compute_state(ruleset: RuleSet, hits: list[RuleHit], gate_fired: bool, override: dict[str, Any] | None) -> tuple[str, float]:
    b = ruleset.bands
    total = sum(h.points for h in hits if h.family != "gate") - overlap_excess(b, hits)
    if gate_fired:
        return "not_scored", total
    if override is not None:
        return str(override["state"]), total
    if any(h.hard for h in hits):
        return "many_signals", total
    if (total >= b.many_signals_min and any(h.family in b.many_signals_families for h in hits)
            and family_points(b, hits) >= b.many_signals_min_family_points):
        return "many_signals", total
    if total >= b.some_signals_min:
        return "some_signals", total
    return "no_signals", total


def evaluate_account(ruleset: RuleSet, account: Account, override: dict[str, Any] | None = None) -> AccountResult:
    """Gate rules first, in file order. The first gate that fires ends the evaluation."""
    hits: list[RuleHit] = []
    skips: list[Skip] = []
    gate_fired = False
    for rule in ruleset.gates:
        hit, sk = evaluate_rule(rule, account)
        skips.extend(sk)
        if hit is not None:
            hits.append(hit)
            gate_fired = True
            break
    if not gate_fired:
        for rule in ruleset.scored:
            if rule.params.get("feed_missing"):
                continue  # B06 with no feed file: the rule is off, not a skip for each account
            hit, sk = evaluate_rule(rule, account)
            skips.extend(sk)
            if hit is not None:
                hits.append(hit)
    state, total = compute_state(ruleset, hits, gate_fired, override)
    note = None
    used_override = override is not None and not gate_fired
    if used_override:
        note = str(override.get("note") or "")  # type: ignore[union-attr]
        # Invariant 5: the state has its cause. The owner's decision is the cause,
        # with 0 points (review 20261103, B-06). The note is the owner's own text;
        # it goes into the cause only if it has no forbidden word (FR-31).
        cause = OVERRIDE_CAUSE + (f": {note}" if note and not forbidden_word_hits(note) else "")
        hits.append(RuleHit(OVERRIDE_RULE_ID, "owner", 0.0, dumps({"state": state, "note": note}), cause[:300], False))
    # Invariant 5: a computed state other than no_signals or not_scored has its causes.
    if not used_override and state in ("some_signals", "many_signals") and not any(h.points > 0 or h.hard for h in hits):
        raise RuntimeError(f"defect: state {state} without rule results")
    if state not in SIGNAL_STATES:
        raise RuntimeError(f"defect: unknown state {state!r}")
    return AccountResult(account.user_id, state, total, hits, skips, note)


# ---------------------------------------------------------------------------
# Stage entry point
# ---------------------------------------------------------------------------


def _sync_overrides(cfg: Config, store: Store) -> dict[str, dict[str, Any]]:
    """Make the overrides table equal to config/overrides.yaml (FR-37).

    The YAML file is the owner's source of truth, so an override stays in effect
    for each run while it is in the file. A row that is not in the file is
    deleted, so a removed entry stops on the next run.
    """
    keep = [str(o["user_id"]) for o in cfg.overrides]
    with store.tx() as c:
        if keep:
            marks = ",".join("?" for _ in keep)
            c.execute(f"DELETE FROM overrides WHERE user_id NOT IN ({marks})", keep)
        else:
            c.execute("DELETE FROM overrides")
        for o in cfg.overrides:
            state = str(o.get("state"))
            if state not in SIGNAL_STATES:
                raise ConfigError(f"overrides: state {state!r} is not one of {SIGNAL_STATES}")
            c.execute(
                """INSERT INTO overrides(user_id,state,note,date) VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, note=excluded.note, date=excluded.date""",
                (str(o["user_id"]), state, None if o.get("note") is None else str(o["note"]),
                 None if o.get("date") is None else str(o["date"])),
            )
    return {r["user_id"]: dict(r) for r in store.q("SELECT user_id, state, note, date FROM overrides")}


def build_accounts(cfg: Config, store: Store, patterns: PatternResult | None = None) -> list[Account]:
    """One Account for each user ID in the interactions (sources and targets).

    ``patterns`` gives the context for rules I02, B02, B08, and B09 to B12.
    Without it, those rules have a missing input.
    """
    ego_id = store.ego_id
    ego_handle = store.get_meta("ego_handle")
    profiles = store.profiles()
    errors = frozenset(store.profile_errors())
    allow = frozenset(cfg.allowlist)
    items = store.interactions()
    ego_profile = profiles.get(ego_id) if ego_id else None
    ego_name = None
    if ego_profile is not None and ego_profile["field_status"].get("name") == "present":
        ego_name = ego_profile.get("name")
    ego_posts = None if patterns is None else patterns.ego_posts
    own: dict[str, list[dict[str, Any]]] = {}
    from_ego: dict[str, list[dict[str, Any]]] = {}
    incoming: dict[str, list[dict[str, Any]]] = {}
    ids: set[str] = set()
    for i in items:
        ids.add(i["source_user_id"])
        ids.add(i["target_user_id"])
        own.setdefault(i["source_user_id"], []).append(i)
        incoming.setdefault(i["target_user_id"], []).append(i)
        if ego_id and i["source_user_id"] == ego_id:
            from_ego.setdefault(i["target_user_id"], []).append(i)
    return [
        Account(
            user_id=uid, profile=profiles.get(uid), own=own.get(uid, []), from_ego=from_ego.get(uid, []),
            incoming=incoming.get(uid, []),
            ego_user_id=ego_id, ego_handle=ego_handle, ego_name=ego_name, allowlist=allow,
            profile_errors=errors, follows_ego=None,
            known_accounts=None if patterns is None else patterns.known_accounts,
            ego_posts=ego_posts,
            pattern_members=None if patterns is None else patterns.members,
        )
        for uid in sorted(ids)
    ]


def run_score(
    cfg: Config,
    store: Store,
    log: RunLog,
    *,
    rules_path: str | Path | None = None,
    ruleset: RuleSet | None = None,
) -> dict[str, Any]:
    """Run the stage ``score``. Returns a summary: counts per state, rules fired, rules with no input."""
    inputs = store.check_fresh("score")
    rs = ruleset if ruleset is not None else load_rules(rules_path or default_rules_path(cfg))
    overrides = _sync_overrides(cfg, store)
    pdata = load_pattern_data(store, frozenset(cfg.allowlist))
    presult = run_patterns(pdata, rs.patterns)
    for f in presult.findings:
        bad = forbidden_word_hits(f"{f.title} {f.summary}")
        if bad:
            raise RuntimeError(f"defect: finding {f.detector} text contains {bad} (FR-31)")
    accounts = build_accounts(cfg, store, presult)
    build_row = store.latest_stage_run("build")
    run_id = build_row["run_id"] if build_row is not None else None
    chash = hashlib.sha256(f"{cfg.hash()}|{rs.digest}|{dumps(overrides)}".encode()).hexdigest()[:16]
    sid = store.begin_stage("score", run_id, inputs, chash)
    results = [evaluate_account(rs, a, overrides.get(a.user_id)) for a in accounts]
    states = {s: 0 for s in SIGNAL_STATES}
    fired = 0
    skipped = 0
    for res in results:
        states[res.state] += 1
        fired += len(res.hits)
        skipped += len(res.skips)
        for sk in res.skips:
            log.write("rule_no_input", rule_id=sk.rule_id, user_id=res.user_id, missing_field=sk.missing_field)
    counts = {
        "accounts": len(results),
        "states": states,
        "rules_fired": fired,
        "rules_no_input": skipped,
        "overrides_applied": sum(1 for r in results if r.override_note is not None),
        "rules": len(rs.rules),
        "findings": presult.by_detector(),
        "findings_total": len(presult.findings),
    }
    with store.tx() as c:
        c.execute("DELETE FROM rule_results")
        c.execute("DELETE FROM rule_skips")
        c.execute("DELETE FROM node_states")
        c.execute("DELETE FROM findings")
        for f in presult.findings:
            c.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)", finding_row(f, sid))
        for res in results:
            for h in res.hits:
                c.execute(
                    "INSERT INTO rule_results(user_id,rule_id,family,points,value_json,cause,hard,stage_run_id) VALUES(?,?,?,?,?,?,?,?)",
                    (res.user_id, h.rule_id, h.family, h.points, h.value_json, h.cause, int(h.hard), sid),
                )
            for sk in res.skips:
                c.execute(
                    "INSERT OR IGNORE INTO rule_skips(user_id,rule_id,missing_field,stage_run_id) VALUES(?,?,?,?)",
                    (res.user_id, sk.rule_id, sk.missing_field, sid),
                )
            c.execute(
                "INSERT INTO node_states(user_id,state,total_points,override_note,stage_run_id) VALUES(?,?,?,?,?)",
                (res.user_id, res.state, res.total, res.override_note, sid),
            )
        store.finish_stage(sid, counts, conn=c)
    log.write("score_done", stage_run_id=sid, **counts)
    return {**counts, "stage_run_id": sid}

