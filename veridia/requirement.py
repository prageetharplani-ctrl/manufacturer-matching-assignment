"""Buyer requirement: strict validation. Never guesses; every problem is reported with a suggestion."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import vocab
from .config import Config

REQUIRED_FIELDS = ("material", "process", "quantity", "deadline_weeks")
OPTIONAL_FIELDS = ("certifications", "batches", "total_quantity", "buyer_country")
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
MAX_TEXT = 100


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str
    suggestions: tuple = ()

    def to_dict(self) -> dict:
        d = {"field": self.field, "message": self.message}
        if self.suggestions:
            d["did_you_mean"] = list(self.suggestions)
        return d


class ValidationError(Exception):
    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))

    def to_dict(self) -> dict:
        return {"type": "validation_error", "errors": [e.to_dict() for e in self.errors]}


@dataclass(frozen=True)
class Requirement:
    material: str
    process: str                      # buyer-facing label, e.g. "CNC machining"
    process_options: tuple            # concrete processes it stands for
    quantity: int                     # units for the first delivery
    deadline_weeks: float
    certifications: tuple = ()
    batches: int | None = None
    total_quantity: int | None = None
    buyer_country: str | None = None
    inferred: tuple = field(default=(), compare=False)  # fields filled from free text, not stated

    def to_dict(self) -> dict:
        return {
            "material": self.material,
            "process": self.process,
            "process_options": list(self.process_options),
            "quantity": self.quantity,
            "deadline_weeks": self.deadline_weeks,
            "certifications": list(self.certifications),
            "batches": self.batches,
            "total_quantity": self.total_quantity,
            "buyer_country": self.buyer_country,
            "inferred_fields": list(self.inferred),
        }


def _int_value(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d[\d,]*", v.strip()):
        return int(v.strip().replace(",", ""))
    return None


def _num_value(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
        return float(v.strip())
    return None


def _text(v: Any, name: str, errors: list[FieldError]) -> str | None:
    if not isinstance(v, str):
        errors.append(FieldError(name, f"must be text, got {type(v).__name__}"))
        return None
    v = v.strip()
    if not v:
        errors.append(FieldError(name, "must not be empty"))
        return None
    if len(v) > MAX_TEXT:
        errors.append(FieldError(name, f"too long ({len(v)} characters; max {MAX_TEXT})"))
        return None
    return v


def parse_requirement(raw: Any, cfg: Config, inferred: tuple = ()) -> Requirement:
    errors: list[FieldError] = []
    if not isinstance(raw, dict):
        raise ValidationError([FieldError("requirement", "must be a JSON object of fields")])

    for k in raw:
        if k not in ALL_FIELDS:
            errors.append(FieldError(str(k)[:40], "unexpected field (accepted: " + ", ".join(ALL_FIELDS) + ")"))
    for name in REQUIRED_FIELDS:
        if raw.get(name) in (None, ""):
            errors.append(FieldError(name, "required but missing"))

    # material
    material = None
    if raw.get("material") not in (None, ""):
        t = _text(raw["material"], "material", errors)
        if t:
            material = vocab.resolve_material(t)
            if material is None:
                errors.append(FieldError("material", f"unknown material {t!r}",
                                         vocab.suggest("material", t) or vocab.MATERIALS[:5]))

    # process
    process, options = None, ()
    if raw.get("process") not in (None, ""):
        t = _text(raw["process"], "process", errors)
        if t:
            options = vocab.resolve_process(t) or ()
            if not options:
                errors.append(FieldError("process", f"unknown process {t!r}",
                                         vocab.suggest("process", t) or vocab.PROCESSES[:5]))
            else:
                process = vocab.process_label(options)

    # quantity
    quantity = None
    if raw.get("quantity") not in (None, ""):
        q = _int_value(raw["quantity"])
        if q is None:
            errors.append(FieldError("quantity", "must be a whole number of units"))
        elif q <= 0:
            errors.append(FieldError("quantity", f"must be at least 1 (got {q})"))
        elif q > cfg.max_quantity:
            errors.append(FieldError("quantity", f"{q:,} is above the supported maximum of {cfg.max_quantity:,}"))
        else:
            quantity = q

    # deadline
    deadline = None
    if raw.get("deadline_weeks") not in (None, ""):
        d = _num_value(raw["deadline_weeks"])
        if d is None:
            errors.append(FieldError("deadline_weeks", "must be a number of weeks"))
        elif d <= 0:
            errors.append(FieldError("deadline_weeks", f"must be greater than 0 (got {d:g})"))
        elif d > cfg.max_deadline_weeks:
            errors.append(FieldError("deadline_weeks",
                                     f"{d:g} weeks is above the supported maximum of {cfg.max_deadline_weeks}"))
        else:
            deadline = int(d) if d == int(d) else d

    # certifications
    certs: list[str] = []
    rc = raw.get("certifications")
    if rc not in (None, ""):
        items = [x for x in re.split(r"[;,]", rc)] if isinstance(rc, str) else rc
        if not isinstance(items, (list, tuple)):
            errors.append(FieldError("certifications", "must be a list of names or a comma-separated string"))
            items = []
        for item in items:
            t = _text(item, "certifications", errors)
            if not t:
                continue
            c = vocab.resolve_certification(t)
            if c is None:
                errors.append(FieldError("certifications", f"unknown certification {t!r}",
                                         vocab.suggest("certification", t) or vocab.CERTIFICATIONS))
            elif c not in certs:
                certs.append(c)

    # optional counts + contradiction check
    batches = total = None
    if raw.get("batches") not in (None, ""):
        batches = _int_value(raw["batches"])
        if batches is None or batches < 1:
            errors.append(FieldError("batches", "must be a whole number >= 1"))
            batches = None
    if raw.get("total_quantity") not in (None, ""):
        total = _int_value(raw["total_quantity"])
        if total is None or total < 1:
            errors.append(FieldError("total_quantity", "must be a whole number >= 1"))
            total = None
    if quantity and total and total < quantity:
        errors.append(FieldError("total_quantity", f"total ({total:,}) is smaller than one batch ({quantity:,})"))
    if quantity and batches and total and total != quantity * batches:
        errors.append(FieldError("total_quantity",
                                 f"contradiction: {batches} batches x {quantity:,} = {quantity * batches:,}, "
                                 f"not {total:,}"))

    country = None
    if raw.get("buyer_country") not in (None, ""):
        t = _text(raw["buyer_country"], "buyer_country", errors)
        if t and not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", t):
            errors.append(FieldError("buyer_country", "must be a country name"))
        else:
            country = t

    if errors:
        raise ValidationError(errors)
    return Requirement(material, process, options, quantity, deadline, tuple(certs),
                       batches, total, country, tuple(inferred))
