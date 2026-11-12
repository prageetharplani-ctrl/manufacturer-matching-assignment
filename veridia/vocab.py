"""Canonical vocabularies and alias resolution for materials, processes and certifications.

Used both when cleaning the dataset and when validating buyer input, so the two
sides always agree on spelling.
"""
from __future__ import annotations

import difflib
import re

MATERIALS = ("aluminium", "copper", "stainless steel", "ABS", "cast iron",
             "mild steel", "titanium", "nylon", "brass")
PROCESSES = ("CNC milling", "CNC turning", "grinding", "welding", "sheet metal",
             "EDM", "forging", "casting", "injection moulding")
CERTIFICATIONS = ("ISO 9001", "ISO 14001", "ISO 13485", "IATF 16949", "AS9100")

# A buyer term that stands for several concrete processes.
CNC_GROUP = ("CNC milling", "CNC turning")
PROCESS_GROUP_LABEL = "CNC machining"


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _compact(s: str) -> str:
    s = re.sub(r":\s*\d{4}", "", s)  # "ISO 9001:2015" -> "ISO 9001"
    return re.sub(r"[^a-z0-9]", "", s.lower())


_MATERIAL_ALIASES = {
    "aluminum": "aluminium", "alu": "aluminium", "al": "aluminium",
    "stainless": "stainless steel", "ss": "stainless steel", "inox": "stainless steel",
    "polyamide": "nylon", "pa6": "nylon", "pa66": "nylon",
    "iron": "cast iron", "ms": "mild steel", "carbon steel": "mild steel",
}
_PROCESS_ALIASES = {
    "milling": ("CNC milling",), "turning": ("CNC turning",),
    "sheetmetal": ("sheet metal",), "sheet metal fabrication": ("sheet metal",),
    "injection molding": ("injection moulding",), "moulding": ("injection moulding",),
    "molding": ("injection moulding",),
    "electrical discharge machining": ("EDM",),
    "cnc": CNC_GROUP, "cnc machining": CNC_GROUP, "cnc machined": CNC_GROUP,
    "machining": CNC_GROUP, "machined": CNC_GROUP,
}
_CERT_ALIASES = {"9001": "ISO 9001", "14001": "ISO 14001", "13485": "ISO 13485",
                 "16949": "IATF 16949", "9100": "AS9100", "as9100d": "AS9100",
                 "as9100c": "AS9100"}

_MAT_BY_KEY = {_key(c): c for c in MATERIALS}
_PROC_BY_KEY = {_key(c): (c,) for c in PROCESSES}
_CERT_BY_COMPACT = {_compact(c): c for c in CERTIFICATIONS}


def resolve_material(raw: str) -> str | None:
    k = _key(raw)
    return _MAT_BY_KEY.get(k) or _MATERIAL_ALIASES.get(k)


def resolve_process(raw: str) -> tuple[str, ...] | None:
    """Return the concrete processes a term stands for (1 for most, 2 for 'CNC machining')."""
    k = _key(raw)
    return _PROC_BY_KEY.get(k) or _PROCESS_ALIASES.get(k)


def resolve_certification(raw: str) -> str | None:
    c = _compact(raw)
    return _CERT_BY_COMPACT.get(c) or _CERT_ALIASES.get(c)


def process_label(concrete: tuple[str, ...]) -> str:
    return PROCESS_GROUP_LABEL if concrete == CNC_GROUP else concrete[0]


def suggest(kind: str, raw: str, n: int = 3) -> tuple[str, ...]:
    """'Did you mean' candidates for an unrecognised term."""
    pool = {
        "material": list(MATERIALS),
        "process": list(PROCESSES) + [PROCESS_GROUP_LABEL],
        "certification": list(CERTIFICATIONS),
    }[kind]
    norm = _compact if kind == "certification" else _key
    target = norm(raw)
    keyed = {norm(c): c for c in pool}
    hits = difflib.get_close_matches(target, list(keyed), n=n, cutoff=0.6)
    hits += [k for k in keyed if len(target) >= 3 and target in k and k not in hits]
    out: list[str] = []
    for h in hits:
        if keyed[h] not in out:
            out.append(keyed[h])
    return tuple(out[:n])
