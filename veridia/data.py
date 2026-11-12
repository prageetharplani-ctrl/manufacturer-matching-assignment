"""Load and clean the manufacturer CSV. Nothing is dropped or repaired silently:
every normalisation, flag and exclusion becomes an Issue in the quality report."""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from . import vocab

REQUIRED_COLUMNS = {
    "manufacturer_id", "name", "city", "state", "materials", "processes",
    "certifications", "certification_expiry", "capacity_units_per_month",
    "min_order_qty", "max_order_qty", "lead_time_weeks", "lead_time_days",
    "on_time_delivery_pct", "delivery_rating", "export_experience_years",
}

# issue code -> what we did about it
ISSUE_ACTIONS = {
    "DUPLICATE_ROW": "excluded",
    "MISSING_ID": "excluded",
    "INVALID_CAPACITY": "excluded",
    "INVALID_ORDER_RANGE": "excluded",
    "DUPLICATE_ID": "flagged (kept, unique internal key assigned)",
    "TOKEN_NORMALISED": "normalised",
    "UNKNOWN_TOKEN": "dropped token, row kept",
    "LEAD_TIME_MISSING": "flagged (cannot satisfy a deadline)",
    "LEAD_TIME_INVALID": "flagged (cannot satisfy a deadline)",
    "LEAD_TIME_FROM_DAYS": "derived from lead_time_days",
    "OTD_OUT_OF_RANGE": "treated as missing for scoring",
    "OTD_MISSING": "treated as missing for scoring",
    "CERT_EXPIRY_INVALID": "flagged (certification cannot be verified)",
    "CERT_EXPIRY_SHARED": "flagged (one date for several certifications; applied to all)",
    "MIN_ORDER_EXCEEDS_CAPACITY": "flagged only",
}

FLAG_MESSAGES = {
    "DUPLICATE_ID": "manufacturer_id is shared with another company in the file; verify identity",
    "LEAD_TIME_FROM_DAYS": "lead time derived from the days column",
    "OTD_OUT_OF_RANGE": "on-time delivery % in the data is impossible (>100 or <0) and was ignored",
    "OTD_MISSING": "on-time delivery % missing",
    "CERT_EXPIRY_INVALID": "certificate expiry date missing or unreadable",
    "CERT_EXPIRY_SHARED": "one expiry date is listed for several certifications; applied to all of them",
    "MIN_ORDER_EXCEEDS_CAPACITY": "minimum order is larger than one month of capacity",
    "UNKNOWN_TOKEN": "some listed values were not recognised and were ignored",
}


@dataclass(frozen=True)
class Issue:
    code: str
    key: str | None
    line: int
    detail: str


@dataclass(frozen=True)
class Manufacturer:
    key: str
    manufacturer_id: str
    name: str
    city: str
    state: str
    materials: frozenset
    processes: frozenset
    certifications: frozenset
    cert_expiry: date | None
    capacity_per_month: int
    min_order: int
    max_order: int
    lead_time_weeks: int | None
    on_time_pct: float | None
    delivery_rating: str
    export_years: int | None
    flags: tuple
    source_line: int


@dataclass(frozen=True)
class Dataset:
    manufacturers: tuple
    issues: tuple
    sha256: str
    source: str
    rows_read: int

    def by_key(self) -> dict:
        return {m.key: m for m in self.manufacturers}

    def quality_report(self) -> dict:
        counts = Counter(i.code for i in self.issues)
        return {
            "source": self.source,
            "sha256": self.sha256,
            "rows_read": self.rows_read,
            "rows_usable": len(self.manufacturers),
            "rows_excluded": sum(1 for i in self.issues if ISSUE_ACTIONS[i.code] == "excluded"),
            "issue_counts": [
                {"code": c, "count": counts[c], "action": ISSUE_ACTIONS[c]}
                for c in ISSUE_ACTIONS if counts[c]
            ],
            "standing_note": (
                "Materials and processes are listed independently in the source data; "
                "nothing says which material a supplier machines with which process. "
                "Matching trusts declared capabilities and cannot verify the pairing."
            ),
            "issues": [
                {"code": i.code, "key": i.key, "line": i.line, "detail": i.detail}
                for i in self.issues
            ],
        }


class DataError(ValueError):
    pass


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _whole(s: str | None) -> int | None:
    v = _num(s)
    return int(v) if v is not None and v == int(v) else None


def _tokens(raw: str, resolver, kind: str, line: int, key_hint: str, issues: list):
    out = set()
    for part in re.split(r"[;,|]", raw or ""):
        part = part.strip()
        if not part:
            continue
        got = resolver(part)
        if got is None:
            issues.append(Issue("UNKNOWN_TOKEN", key_hint, line, f"{kind} {part!r} not recognised"))
            continue
        got = (got,) if isinstance(got, str) else got
        canon = set(got)
        if not (len(got) == 1 and part == got[0]):
            issues.append(Issue("TOKEN_NORMALISED", key_hint, line, f"{kind} {part!r} -> {sorted(canon)}"))
        out |= canon
    return frozenset(out)


def _parse_row(row: dict, line: int, issues: list) -> Manufacturer | None:
    g = lambda c: (row.get(c) or "").strip()
    mid, name = g("manufacturer_id"), g("name")
    if not mid:
        issues.append(Issue("MISSING_ID", None, line, "manufacturer_id is empty"))
        return None
    cap = _whole(g("capacity_units_per_month"))
    if cap is None or cap <= 0:
        issues.append(Issue("INVALID_CAPACITY", mid, line, f"capacity {g('capacity_units_per_month')!r}"))
        return None
    lo, hi = _whole(g("min_order_qty")), _whole(g("max_order_qty"))
    if lo is None or hi is None or lo <= 0 or hi < lo:
        issues.append(Issue("INVALID_ORDER_RANGE", mid, line,
                            f"min {g('min_order_qty')!r}, max {g('max_order_qty')!r}"))
        return None

    flags: list[str] = []
    local: list[Issue] = []
    materials = _tokens(g("materials"), vocab.resolve_material, "material", line, mid, local)
    processes = _tokens(g("processes"), vocab.resolve_process, "process", line, mid, local)
    certs = _tokens(g("certifications"), vocab.resolve_certification, "certification", line, mid, local)
    if any(i.code == "UNKNOWN_TOKEN" for i in local):
        flags.append("UNKNOWN_TOKEN")

    # certificate expiry (ISO only; anything else is flagged, never guessed)
    expiry = None
    raw_exp = g("certification_expiry")
    try:
        expiry = date.fromisoformat(raw_exp)
    except ValueError:
        local.append(Issue("CERT_EXPIRY_INVALID", mid, line, f"expiry {raw_exp!r} is not YYYY-MM-DD"))
        flags.append("CERT_EXPIRY_INVALID")
    if expiry and len(certs) > 1:
        local.append(Issue("CERT_EXPIRY_SHARED", mid, line, f"{len(certs)} certifications share {raw_exp}"))
        flags.append("CERT_EXPIRY_SHARED")

    # lead time
    lead = _whole(g("lead_time_weeks"))
    if lead is not None and lead < 0:
        local.append(Issue("LEAD_TIME_INVALID", mid, line, f"lead time {lead} weeks is negative"))
        lead = None
    elif lead is None and g("lead_time_weeks"):
        local.append(Issue("LEAD_TIME_INVALID", mid, line, f"lead time {g('lead_time_weeks')!r}"))
    if lead is None:
        days = _num(g("lead_time_days"))
        if days is not None and days > 0:
            lead = math.ceil(days / 7)
            local.append(Issue("LEAD_TIME_FROM_DAYS", mid, line, f"{days:g} days -> {lead} weeks"))
            flags.append("LEAD_TIME_FROM_DAYS")
        elif not g("lead_time_weeks") and not g("lead_time_days"):
            local.append(Issue("LEAD_TIME_MISSING", mid, line, "weeks and days both empty"))

    otd = _num(g("on_time_delivery_pct"))
    if otd is None:
        local.append(Issue("OTD_MISSING", mid, line, "on_time_delivery_pct empty/unreadable"))
        flags.append("OTD_MISSING")
    elif not 0 <= otd <= 100:
        local.append(Issue("OTD_OUT_OF_RANGE", mid, line, f"on_time_delivery_pct {otd:g}"))
        flags.append("OTD_OUT_OF_RANGE")
        otd = None

    if lo > cap:
        local.append(Issue("MIN_ORDER_EXCEEDS_CAPACITY", mid, line, f"min order {lo} > monthly capacity {cap}"))
        flags.append("MIN_ORDER_EXCEEDS_CAPACITY")

    issues.extend(local)
    years = _whole(g("export_experience_years"))
    return Manufacturer(
        key=mid, manufacturer_id=mid, name=name, city=g("city"), state=g("state"),
        materials=materials, processes=processes, certifications=certs, cert_expiry=expiry,
        capacity_per_month=cap, min_order=lo, max_order=hi, lead_time_weeks=lead,
        on_time_pct=otd, delivery_rating=g("delivery_rating"),
        export_years=years if years is not None and years >= 0 else None,
        flags=tuple(dict.fromkeys(flags)), source_line=line,
    )


def load_text(text: str, source: str = "<memory>", sha: str | None = None) -> Dataset:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    reader.fieldnames = [f.strip() for f in (reader.fieldnames or [])]
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise DataError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

    issues: list[Issue] = []
    parsed: list[Manufacturer] = []
    seen_rows: set = set()
    rows_read = 0
    for line, row in enumerate(reader, start=2):
        rows_read += 1
        signature = tuple((v or "").strip().lower() for k, v in row.items() if k)
        if signature in seen_rows:
            issues.append(Issue("DUPLICATE_ROW", (row.get("manufacturer_id") or "").strip() or None,
                                line, "identical to an earlier row"))
            continue
        seen_rows.add(signature)
        m = _parse_row(row, line, issues)
        if m:
            parsed.append(m)

    # Same id on different companies: keep all, give each a unique internal key.
    counts = Counter(m.manufacturer_id for m in parsed)
    nth: Counter = Counter()
    final: list[Manufacturer] = []
    for m in parsed:
        if counts[m.manufacturer_id] > 1:
            nth[m.manufacturer_id] += 1
            key = f"{m.manufacturer_id}#{nth[m.manufacturer_id]}"
            issues.append(Issue("DUPLICATE_ID", key, m.source_line,
                                f"{m.manufacturer_id} also used by another company ({m.name})"))
            m = replace(m, key=key, flags=tuple(dict.fromkeys(m.flags + ("DUPLICATE_ID",))))
        final.append(m)

    # Re-key issues raised before the key was known to the same company
    issues.sort(key=lambda i: (i.line, i.code))
    return Dataset(tuple(final), tuple(issues),
                   sha or hashlib.sha256(text.encode("utf-8")).hexdigest(), source, rows_read)


def load_dataset(path: str | Path) -> Dataset:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise DataError(f"cannot read dataset {p}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError(f"dataset {p} is not valid UTF-8: {exc}") from exc
    return load_text(text, str(p), hashlib.sha256(raw).hexdigest())
