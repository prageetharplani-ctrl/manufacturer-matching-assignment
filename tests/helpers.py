"""Shared fixtures: tiny hand-built datasets so every test states exactly what it relies on."""
from __future__ import annotations

import csv
import io
from datetime import date

from veridia.config import load_config
from veridia.data import REQUIRED_COLUMNS, Dataset, Manufacturer
from veridia.requirement import parse_requirement

CFG = load_config()
AS_OF = date(2026, 10, 28)

BASE_REQ = {"material": "aluminium", "process": "CNC machining", "quantity": 1000,
            "deadline_weeks": 6, "certifications": ["ISO 9001"]}


def mfr(key="M1", **kw) -> Manufacturer:
    d = dict(key=key, manufacturer_id=key.split("#")[0], name=f"Mfr {key}", city="Pune", state="MH",
             materials=frozenset({"aluminium"}), processes=frozenset({"CNC milling"}),
             certifications=frozenset({"ISO 9001"}), cert_expiry=date(2028, 1, 1),
             capacity_per_month=10000, min_order=100, max_order=5000, lead_time_weeks=4,
             on_time_pct=95.0, delivery_rating="A", export_years=10, flags=(), source_line=2)
    d.update(kw)
    return Manufacturer(**d)


def dataset(*ms: Manufacturer) -> Dataset:
    return Dataset(tuple(ms), (), "testsha", "test", len(ms))


def req(**kw):
    d = dict(BASE_REQ)
    d.update(kw)
    return parse_requirement(d, CFG)


COLUMNS = sorted(REQUIRED_COLUMNS | {"country", "status"})
GOOD_ROW = {
    "manufacturer_id": "VM9001", "name": "Test Works", "country": "India", "city": "Pune", "state": "MH",
    "materials": "aluminium", "processes": "CNC milling", "certifications": "ISO 9001",
    "certification_expiry": "2028-01-01", "capacity_units_per_month": "10000", "min_order_qty": "100",
    "max_order_qty": "5000", "lead_time_weeks": "4", "lead_time_days": "28", "on_time_delivery_pct": "95.0",
    "delivery_rating": "A", "export_experience_years": "10", "status": "active",
}


def csv_text(*rows: dict, bom: bool = False, crlf: bool = False) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\r\n" if crlf else "\n")
    w.writeheader()
    for r in rows:
        w.writerow({**GOOD_ROW, **r})
    return ("\ufeff" if bom else "") + buf.getvalue()
