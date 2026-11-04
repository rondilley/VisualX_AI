"""Load and check the configuration files (MR-01).

``config/visualx.toml`` holds the defaults. ``config/visualx.local.toml`` (not in
version control) overrides any key. ``rules.yaml``, ``overrides.yaml``, and
``allowlist.yaml`` sit in the same folder.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import INTERACTION_TYPES, SIGNAL_STATES, dumps

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class ConfigError(ValueError):
    pass


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    root: Path
    raw: dict[str, Any]
    rules: list[dict[str, Any]] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    config_dir: Path | None = None  # the folder that load() read (rules.yaml lives here)
    topics: dict[str, Any] = field(default_factory=dict)  # topics.yaml (FR-28), display labels only

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def data_dir(self) -> Path:
        d = Path(self.get("data.dir", "data"))
        return d if d.is_absolute() else (self.root / d)

    @property
    def store_path(self) -> Path:
        return self.data_dir / "store.sqlite"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def hash(self) -> str:
        blob = dumps({"raw": self.raw, "rules": self.rules, "topics": self.topics})
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _load_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return default if data is None else data


def validate(raw: dict[str, Any]) -> None:
    for k in ("post_read", "user_read", "owned_read"):
        v = raw.get("prices", {}).get(k)
        if not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"prices.{k} must be a number 0 or more")
    cap = raw.get("collect", {}).get("cap_usd")
    if not isinstance(cap, (int, float)) or cap <= 0:
        raise ConfigError("collect.cap_usd must be more than 0")
    for t in INTERACTION_TYPES:
        v = raw.get("weights", {}).get(t)
        if not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"weights.{t} must be a number 0 or more")
    for section, keys in (("render.state_colors", SIGNAL_STATES), ("render.edge_colors", INTERACTION_TYPES)):
        sec = raw
        for p in section.split("."):
            sec = sec.get(p, {})
        for k in keys:
            if not _HEX.match(str(sec.get(k, ""))):
                raise ConfigError(f"{section}.{k} must be a color in #RRGGBB form (no value from X goes into CSS)")
    shapes = raw.get("render", {}).get("state_shapes", {})
    for k in SIGNAL_STATES:
        if not isinstance(shapes.get(k), str) or not (1 <= len(shapes[k]) <= 2):
            raise ConfigError(f"render.state_shapes.{k} must be one or two characters")
    if raw.get("render", {}).get("size_mode") not in ("weighted_degree", "followers", "activity"):
        # The same three modes as the Node size control of the page (review 20261103, C-20).
        raise ConfigError("render.size_mode must be 'weighted_degree', 'followers', or 'activity'")
    if raw.get("billing_assumptions", {}).get("expansions_billed", False) is not False:
        # Invariant 3: the plan reserves the maximum cost of a call. X gives no
        # maximum for "includes", so this setting cannot keep the cap (review 20261103, A-08).
        raise ConfigError("billing_assumptions.expansions_billed must be false: the X API has no limit on the "
                          "objects in includes, so the tool cannot reserve the maximum cost of a call")


def load(config_dir: str | Path = "config", root: str | Path | None = None) -> Config:
    cdir = Path(config_dir)
    base_path = cdir / "visualx.toml"
    if not base_path.exists():
        raise ConfigError(f"{base_path} not found")
    with base_path.open("rb") as f:
        raw = tomllib.load(f)
    local = cdir / "visualx.local.toml"
    if local.exists():
        with local.open("rb") as f:
            raw = _merge(raw, tomllib.load(f))
    validate(raw)
    rules = _load_yaml(cdir / "rules.yaml", [])
    if isinstance(rules, dict):
        rules = rules.get("rules", [])
    overrides = _load_yaml(cdir / "overrides.yaml", [])
    if isinstance(overrides, dict):
        overrides = overrides.get("overrides", [])
    for o in overrides:
        if str(o.get("state")) not in SIGNAL_STATES:
            raise ConfigError(f"overrides.yaml: state {o.get('state')!r} is not one of {SIGNAL_STATES}")
        o["user_id"] = str(o["user_id"])
    allow = _load_yaml(cdir / "allowlist.yaml", [])
    if isinstance(allow, dict):
        allow = allow.get("allowlist", [])
    allowlist = [str(a.get("user_id") if isinstance(a, dict) else a) for a in allow]
    topics = _load_yaml(cdir / "topics.yaml", {})
    if isinstance(topics, dict) and isinstance(topics.get("topics"), dict):
        topics = topics["topics"]
    if not isinstance(topics, dict):
        raise ConfigError("topics.yaml must map each topic name to a list of keywords")
    from .groups import TopicError, check_topics

    try:
        check_topics(topics)  # TR-06 and pattern checks before any stage runs
    except TopicError as e:
        raise ConfigError(f"topics.yaml: {e}") from e
    r = Path(root) if root else cdir.resolve().parent
    return Config(root=r, raw=raw, rules=rules, overrides=overrides, allowlist=allowlist, config_dir=cdir,
                  topics=topics)
