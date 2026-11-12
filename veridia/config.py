"""Configuration loading and validation (config.toml, overridable via VERIDIA_CONFIG)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent
WEIGHT_KEYS = ("lead_time", "delivery", "cert_runway", "capacity", "export_experience")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    as_of: date
    expiry_warning_days: int
    weeks_per_month: float
    max_quantity: int
    max_deadline_weeks: int
    hold_ttl_seconds: int
    result_limit: int
    weights: Mapping[str, float]


def parse_date(value: object, what: str = "date") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{what} must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ConfigError(f"{what} {value!r} is not a valid YYYY-MM-DD date") from None


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("VERIDIA_CONFIG") or ROOT / "config.toml")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc

    def num(key: str, kind=int, lo=0):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= lo:
            raise ConfigError(f"config key {key!r} must be a number > {lo}")
        return kind(v)

    weights = raw.get("weights", {})
    if set(weights) != set(WEIGHT_KEYS):
        raise ConfigError(f"[weights] must define exactly: {', '.join(WEIGHT_KEYS)}")
    for k, v in weights.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"weight {k!r} must be a non-negative number")
    if sum(weights.values()) <= 0:
        raise ConfigError("weights must not all be zero")

    return Config(
        as_of=parse_date(raw.get("as_of"), "as_of"),
        expiry_warning_days=num("expiry_warning_days", int, -1),
        weeks_per_month=num("weeks_per_month", float),
        max_quantity=num("max_quantity"),
        max_deadline_weeks=num("max_deadline_weeks"),
        hold_ttl_seconds=num("hold_ttl_seconds"),
        result_limit=num("result_limit"),
        weights=MappingProxyType({k: float(weights[k]) for k in WEIGHT_KEYS}),
    )
