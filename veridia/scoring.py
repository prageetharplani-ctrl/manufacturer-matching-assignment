"""Transparent weighted scoring of manufacturers that already passed every hard filter.

Each component is normalised to 0..1. A component whose input is unavailable (e.g. impossible
on-time %) is left out and the remaining weights are re-normalised, so missing data is neither
rewarded nor punished. The score is 0..100.
"""
from __future__ import annotations

from datetime import date, timedelta

from .config import Config
from .data import Manufacturer
from .requirement import Requirement


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_manufacturer(m: Manufacturer, req: Requirement, as_of: date, cfg: Config,
                       held_units: int) -> tuple[float, dict]:
    components: dict[str, tuple[float | None, str]] = {}

    lead = m.lead_time_weeks
    components["lead_time"] = (
        _clip((req.deadline_weeks - lead) / req.deadline_weeks) if lead is not None else None,
        f"{lead} weeks vs {req.deadline_weeks:g} allowed",
    )
    components["delivery"] = (
        _clip((m.on_time_pct - 70) / 30) if m.on_time_pct is not None else None,
        f"{m.on_time_pct:g}% on time" if m.on_time_pct is not None else "unavailable",
    )
    if req.certifications and m.cert_expiry and lead is not None:
        runway = (m.cert_expiry - (as_of + timedelta(weeks=lead))).days
        components["cert_runway"] = (_clip(runway / 365), f"{runway} days of validity after expected delivery")
    else:
        components["cert_runway"] = (None, "not applicable (no certification required)")
    window = m.capacity_per_month * req.deadline_weeks / cfg.weeks_per_month - held_units
    components["capacity"] = (
        _clip((window - req.quantity) / window) if window > 0 else None,
        f"{window:,.0f} units available vs {req.quantity:,} needed",
    )
    components["export_experience"] = (
        _clip(m.export_years / 25) if m.export_years is not None else None,
        f"{m.export_years} years" if m.export_years is not None else "unavailable",
    )

    avail_weight = sum(cfg.weights[k] for k, (v, _) in components.items() if v is not None)
    breakdown, total = {}, 0.0
    for k in cfg.weights:
        value, note = components[k]
        if value is None or avail_weight == 0:
            breakdown[k] = {"value": note, "normalised": None, "weight": cfg.weights[k],
                            "effective_weight": 0.0, "points": 0.0}
            continue
        eff = cfg.weights[k] / avail_weight
        pts = 100 * eff * value
        total += pts
        breakdown[k] = {"value": note, "normalised": round(value, 4), "weight": cfg.weights[k],
                        "effective_weight": round(eff, 4), "points": round(pts, 2)}
    return round(total, 2), breakdown
