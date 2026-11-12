"""Deterministic matching engine. A pure function of (requirement, dataset, as_of, config, holds snapshot).

Every manufacturer is evaluated against every criterion (no short-circuiting), which is what
makes the funnel, the 'why not X' lookup and the relaxation suggestions cheap and consistent.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Mapping

from . import ENGINE_VERSION
from .config import Config
from .data import FLAG_MESSAGES, Dataset, Manufacturer
from .requirement import Requirement
from .scoring import score_manufacturer

CRITERIA = ("material", "process", "certification", "certificate_validity",
            "lead_time", "order_range", "capacity")

STANDING_NOTES = (
    "Materials and processes are listed independently in the source data, so a supplier that lists "
    "both 'aluminium' and 'CNC milling' is not proven to machine aluminium. Confirm with the supplier.",
    "Capacity check: monthly capacity x deadline weeks / weeks-per-month, minus units held by other "
    "buyers, must cover the quantity. Matching is non-binding; capacity is only reserved by an explicit hold.",
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    requirement: str
    actual: str


def window_capacity(m: Manufacturer, deadline_weeks: float, cfg: Config) -> float:
    return m.capacity_per_month * deadline_weeks / cfg.weeks_per_month


def delivery_date(m: Manufacturer, as_of: date) -> date | None:
    return as_of + timedelta(weeks=m.lead_time_weeks) if m.lead_time_weeks is not None else None


def evaluate(m: Manufacturer, req: Requirement, as_of: date, cfg: Config,
             held: Mapping[str, int]) -> list[Check]:
    checks = []
    checks.append(Check("material", req.material in m.materials, req.material,
                        ", ".join(sorted(m.materials)) or "none listed"))
    matched = sorted(p for p in req.process_options if p in m.processes)
    checks.append(Check("process", bool(matched), req.process,
                        ", ".join(sorted(m.processes)) or "none listed"))

    need = set(req.certifications)
    if need:
        have = need <= m.certifications
        checks.append(Check("certification", have, ", ".join(req.certifications),
                            ", ".join(sorted(m.certifications)) or "none listed"))
        if m.cert_expiry is None:
            ok, actual = False, "expiry date missing or unreadable"
        else:
            ok = have and m.cert_expiry >= as_of
            actual = f"expires {m.cert_expiry.isoformat()}"
            if not have:
                actual += " (required certification not held)"
        checks.append(Check("certificate_validity", ok, f"valid on {as_of.isoformat()}", actual))
    else:
        checks.append(Check("certification", True, "none required", ", ".join(sorted(m.certifications))))
        checks.append(Check("certificate_validity", True, "none required",
                            m.cert_expiry.isoformat() if m.cert_expiry else "n/a"))

    lead = m.lead_time_weeks
    checks.append(Check("lead_time", lead is not None and lead <= req.deadline_weeks,
                        f"<= {req.deadline_weeks:g} weeks",
                        f"{lead} weeks" if lead is not None else "unknown"))
    checks.append(Check("order_range", m.min_order <= req.quantity <= m.max_order,
                        f"{req.quantity:,} units",
                        f"accepts {m.min_order:,}-{m.max_order:,}"))
    window = window_capacity(m, req.deadline_weeks, cfg)
    avail = window - held.get(m.key, 0)
    checks.append(Check("capacity", avail >= req.quantity, f"{req.quantity:,} units",
                        f"{avail:,.0f} available ({m.capacity_per_month:,}/month over "
                        f"{req.deadline_weeks:g} weeks, {held.get(m.key, 0):,} held by others)"))
    return checks


def _label(name: str, req: Requirement, as_of: date) -> str:
    return {
        "material": f"material: {req.material}",
        "process": f"process: {req.process}",
        "certification": ("certifications held: " + ", ".join(req.certifications))
        if req.certifications else "certifications: none required",
        "certificate_validity": f"certificate valid on {as_of.isoformat()}",
        "lead_time": f"lead time <= {req.deadline_weeks:g} weeks",
        "order_range": f"order range includes {req.quantity:,} units",
        "capacity": f"capacity covers {req.quantity:,} units (after holds)",
    }[name]


def _warnings(m: Manufacturer, req: Requirement, as_of: date, cfg: Config, held: int) -> list[dict]:
    out = []
    if req.certifications and m.cert_expiry:
        deliv = delivery_date(m, as_of)
        days_left = (m.cert_expiry - as_of).days
        if deliv and m.cert_expiry < deliv:
            out.append({"code": "CERT_EXPIRES_BEFORE_DELIVERY",
                        "message": f"certificate expires {m.cert_expiry.isoformat()}, before expected "
                                   f"delivery on {deliv.isoformat()}"})
        elif days_left <= cfg.expiry_warning_days:
            out.append({"code": "CERT_EXPIRING_SOON",
                        "message": f"certificate expires {m.cert_expiry.isoformat()} ({days_left} days after "
                                   f"{as_of.isoformat()}); confirm renewal"})
    if held:
        out.append({"code": "CAPACITY_HELD_BY_OTHERS",
                    "message": f"{held:,} units of this supplier's capacity are currently held by other buyers"})
    return out


def _caveats(m: Manufacturer, req: Requirement) -> list[dict]:
    out = []
    for f in m.flags:
        if f == "CERT_EXPIRY_SHARED" and not req.certifications:
            continue
        if f == "OTD_MISSING" or f in FLAG_MESSAGES:
            out.append({"code": f, "message": FLAG_MESSAGES.get(f, f)})
    return out


def _funnel(all_checks: dict, ds: Dataset, req: Requirement, as_of: date):
    alive = list(ds.manufacturers)
    steps = [{"step": "all usable manufacturers", "remaining": len(alive), "eliminated": 0}]
    for i, name in enumerate(CRITERIA):
        before = len(alive)
        alive = [m for m in alive if all_checks[m.key][i].ok]
        steps.append({"step": _label(name, req, as_of), "remaining": len(alive),
                      "eliminated": before - len(alive)})
    return steps


def _diagnose(ds: Dataset, req: Requirement, as_of: date, cfg: Config, held: Mapping[str, int],
              all_checks: dict) -> dict:
    solo = {n: sum(1 for m in ds.manufacturers if all_checks[m.key][i].ok)
            for i, n in enumerate(CRITERIA)}
    most = min(CRITERIA, key=lambda n: (solo[n], CRITERIA.index(n)))

    def passes(m, skip):
        return all(c.ok for c in all_checks[m.key] if c.name not in skip)

    relax = []
    # 1) later deadline (covers lead time and capacity)
    for w in range(math.floor(req.deadline_weeks) + 1, cfg.max_deadline_weeks + 1):
        r2 = replace(req, deadline_weeks=w)
        n = sum(1 for m in ds.manufacturers if all(c.ok for c in evaluate(m, r2, as_of, cfg, held)))
        if n:
            relax.append({"relax": "deadline", "change": {"deadline_weeks": w}, "matches_if_relaxed": n,
                          "suggestion": f"extend the deadline to {w} weeks"})
            break
    # 2) quantity closest to the request that some otherwise-suitable supplier can take
    static = [m for m in ds.manufacturers if passes(m, {"order_range", "capacity"})]
    best = None
    for m in static:
        hi = min(m.max_order, math.floor(window_capacity(m, req.deadline_weeks, cfg) - held.get(m.key, 0)))
        if m.min_order <= hi:
            cand = hi if req.quantity > hi else m.min_order
            if best is None or abs(cand - req.quantity) < abs(best - req.quantity):
                best = cand
    if best is not None:
        r2 = replace(req, quantity=best)
        n = sum(1 for m in ds.manufacturers if all(c.ok for c in evaluate(m, r2, as_of, cfg, held)))
        relax.append({"relax": "quantity", "change": {"quantity": best}, "matches_if_relaxed": n,
                      "suggestion": f"change the quantity to {best:,}"})
    # 3) drop certification requirement, 4) process, 5) material
    for name, skip, text in (
        ("certification", {"certification", "certificate_validity"}, "drop the certification requirement"),
        ("process", {"process"}, f"accept processes other than {req.process}"),
        ("material", {"material"}, f"accept materials other than {req.material}"),
    ):
        n = sum(1 for m in ds.manufacturers if passes(m, skip))
        if n:
            relax.append({"relax": name, "change": {name: None}, "matches_if_relaxed": n, "suggestion": text})
    return {
        "most_restrictive_requirement": most,
        "suppliers_passing_each_requirement_alone": solo,
        "relaxations": relax,
        "smallest_relaxation": relax[0] if relax else None,
    }


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def run_match(req: Requirement, ds: Dataset, cfg: Config, as_of: date,
              held: Mapping[str, int] | None = None, limit: int | None = None) -> dict:
    held = dict(held or {})
    limit = cfg.result_limit if limit is None else limit
    all_checks = {m.key: evaluate(m, req, as_of, cfg, held) for m in ds.manufacturers}
    matched = [m for m in ds.manufacturers if all(c.ok for c in all_checks[m.key])]

    scored = []
    for m in matched:
        total, breakdown = score_manufacturer(m, req, as_of, cfg, held.get(m.key, 0))
        scored.append((m, total, breakdown))
    scored.sort(key=lambda t: (-t[1], t[0].key))

    matches = []
    for rank, (m, total, breakdown) in enumerate(scored[:limit], start=1):
        matches.append({
            "rank": rank, "key": m.key, "manufacturer_id": m.manufacturer_id, "name": m.name,
            "city": m.city, "state": m.state, "score": total, "score_breakdown": breakdown,
            "reasons": [{"criterion": c.name, "requirement": c.requirement, "actual": c.actual}
                        for c in all_checks[m.key]],
            "warnings": _warnings(m, req, as_of, cfg, held.get(m.key, 0)),
            "caveats": _caveats(m, req),
            "held_by_others_units": held.get(m.key, 0),
        })

    notes = list(STANDING_NOTES)
    if req.batches and req.batches > 1:
        notes.append(f"Requirement has {req.batches} batches: only the first delivery of "
                     f"{req.quantity:,} units is matched; ongoing cadence is not modelled.")
    result = {
        "as_of": as_of.isoformat(),
        "requirement": req.to_dict(),
        "dataset_sha256": ds.sha256,
        "engine_version": ENGINE_VERSION,
        "total_candidates": len(ds.manufacturers),
        "total_matches": len(matched),
        "returned": len(matches),
        "matches": matches,
        "funnel": _funnel(all_checks, ds, req, as_of),
        "diagnosis": None if matched else _diagnose(ds, req, as_of, cfg, held, all_checks),
        "notes": notes,
    }
    result["request_fingerprint"] = hashlib.sha256(canonical_json({
        "req": req.to_dict(), "as_of": as_of.isoformat(), "dataset": ds.sha256,
        "weights": dict(cfg.weights), "limit": limit, "held": sorted(held.items()),
        "engine": ENGINE_VERSION, "cfg": [cfg.weeks_per_month, cfg.expiry_warning_days],
    }).encode()).hexdigest()
    return result


def why_not(key_or_id: str, req: Requirement, ds: Dataset, cfg: Config, as_of: date,
            held: Mapping[str, int] | None = None) -> dict:
    """Explain how one manufacturer fares against every criterion. Accepts an internal key or a bare id."""
    held = dict(held or {})
    by_key = ds.by_key()
    targets = [by_key[key_or_id]] if key_or_id in by_key else \
        [m for m in ds.manufacturers if m.manufacturer_id == key_or_id]
    if not targets:
        import difflib
        near = difflib.get_close_matches(key_or_id, list(by_key), n=3, cutoff=0.6)
        return {"found": False, "message": f"no manufacturer with id {key_or_id!r}", "did_you_mean": near}
    out = []
    for m in targets:
        checks = evaluate(m, req, as_of, cfg, held)
        failing = [c.name for c in checks if not c.ok]
        out.append({
            "key": m.key, "name": m.name, "matches": not failing, "failed_criteria": failing,
            "criteria": [{"criterion": c.name, "passed": c.ok, "requirement": c.requirement,
                          "actual": c.actual} for c in checks],
            "caveats": _caveats(m, req),
        })
    return {"found": True, "as_of": as_of.isoformat(), "manufacturers": out}
