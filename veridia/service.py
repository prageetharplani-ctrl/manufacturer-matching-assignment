"""Orchestration: validation -> matching -> hold registry -> sessions -> audit log.

Matching itself stays a pure function (matching.run_match). All shared state lives here,
behind locks: the hold registry and the per-session revision history.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from .config import Config, parse_date
from .data import Dataset
from .holds import HoldRegistry, HoldRejected
from .matching import run_match, why_not as _why_not, window_capacity
from .requirement import FieldError, Requirement, ValidationError, parse_requirement

SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class NotFound(Exception):
    pass


def make_audit_logger(path: str | Path | None) -> logging.Logger:
    logger = logging.getLogger(f"veridia.audit.{path}")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            h: logging.Handler = logging.FileHandler(p, encoding="utf-8")
        else:
            h = logging.NullHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(h)
    return logger


def diff_requirements(old: dict, new: dict) -> list[dict]:
    out = []
    for k in sorted(set(old) | set(new)):
        if k in ("inferred_fields", "process_options"):
            continue
        if old.get(k) != new.get(k):
            out.append({"field": k, "from": old.get(k), "to": new.get(k)})
    return out


def diff_results(old: dict, new: dict) -> dict:
    o = {m["key"]: m["rank"] for m in old["matches"]}
    n = {m["key"]: m["rank"] for m in new["matches"]}
    return {
        "matches_before": old["total_matches"], "matches_after": new["total_matches"],
        "added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
        "rank_changes": [{"key": k, "from": o[k], "to": n[k]} for k in sorted(set(o) & set(n)) if o[k] != n[k]],
    }


class Service:
    def __init__(self, dataset: Dataset, cfg: Config, holds: HoldRegistry | None = None,
                 audit_path: str | Path | None = None, clock=time.time):
        self.ds, self.cfg = dataset, cfg
        self.holds = holds or HoldRegistry(cfg.hold_ttl_seconds, clock)
        self._by_key = dataset.by_key()
        self._log = make_audit_logger(audit_path)
        self._sessions: dict[str, list[dict]] = {}
        self._session_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---- helpers -------------------------------------------------------------
    def _as_of(self, as_of: Any) -> date:
        if as_of in (None, ""):
            return self.cfg.as_of
        try:
            return parse_date(as_of, "as_of")
        except ValueError as exc:
            raise ValidationError([FieldError("as_of", str(exc))]) from None

    def _req(self, raw: Any) -> Requirement:
        return raw if isinstance(raw, Requirement) else parse_requirement(raw, self.cfg)

    def _audit(self, event: str, **fields) -> str:
        rid = str(uuid.uuid4())
        self._log.info(json.dumps({"request_id": rid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "event": event, "dataset_sha256": self.ds.sha256, **fields}, default=str))
        return rid

    def _lock_for(self, session_id: str) -> threading.Lock:
        if not SESSION_ID.match(str(session_id)):
            raise ValidationError([FieldError("session_id", "must be 1-64 characters of letters, digits, . _ -")])
        with self._guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    # ---- matching ------------------------------------------------------------
    def match(self, raw: Any, *, owner: str | None = None, as_of: Any = None, limit: int | None = None) -> dict:
        req, when = self._req(raw), self._as_of(as_of)
        held = self.holds.snapshot(exclude_owner=owner)
        result = run_match(req, self.ds, self.cfg, when, held, limit)
        rid = self._audit("match", owner=owner, requirement=req.to_dict(), as_of=when.isoformat(),
                          fingerprint=result["request_fingerprint"],
                          result_keys=[m["key"] for m in result["matches"]], total_matches=result["total_matches"])
        return {**result, "request_id": rid}

    def why_not(self, raw: Any, key: str, *, owner: str | None = None, as_of: Any = None) -> dict:
        req, when = self._req(raw), self._as_of(as_of)
        return _why_not(key, req, self.ds, self.cfg, when, self.holds.snapshot(exclude_owner=owner))

    # ---- sessions (a buyer who changes their mind) -------------------------------
    def submit(self, session_id: str, raw: Any, *, as_of: Any = None, limit: int | None = None) -> dict:
        lock = self._lock_for(session_id)
        req = self._req(raw)            # invalid input never touches session state
        with lock:
            history = self._sessions.setdefault(session_id, [])
            released = self.holds.release_owner(session_id) if history else 0
            result = self.match(req, owner=session_id, as_of=as_of, limit=limit)
            rev = {"revision": len(history) + 1, "requirement": req.to_dict(), "result": result}
            diff = None
            if history:
                prev = history[-1]
                diff = {"requirement_changes": diff_requirements(prev["requirement"], rev["requirement"]),
                        "result_changes": diff_results(prev["result"], result),
                        "holds_released": released}
            history.append(rev)
            return {"session_id": session_id, "revision": rev["revision"], "result": result, "diff": diff}

    def history(self, session_id: str) -> list[dict]:
        lock = self._lock_for(session_id)
        with lock:
            return [{"revision": r["revision"], "requirement": r["requirement"],
                     "total_matches": r["result"]["total_matches"],
                     "top": [m["key"] for m in r["result"]["matches"][:3]]}
                    for r in self._sessions.get(session_id, [])]

    # ---- holds -----------------------------------------------------------------
    def hold(self, session_id: str, key: str) -> dict:
        lock = self._lock_for(session_id)
        with lock:
            history = self._sessions.get(session_id)
            if not history:
                raise NotFound("submit a requirement for this session before placing a hold")
            req_dict = history[-1]["requirement"]
            req = self._req({k: req_dict[k] for k in ("material", "process", "quantity", "deadline_weeks",
                                                      "certifications", "batches", "total_quantity",
                                                      "buyer_country")})
            m = self._by_key.get(key)
            if m is None:
                raise NotFound(f"unknown manufacturer key {key!r}")
            # Static suitability is checked outside the registry; capacity is re-checked atomically inside it.
            verdict = self.why_not(req, key, owner=session_id)["manufacturers"][0]
            static_failures = [c for c in verdict["failed_criteria"] if c != "capacity"]
            if static_failures:
                raise ValidationError([FieldError("key", f"{key} does not meet: {', '.join(static_failures)}")])
            try:
                h = self.holds.try_hold(session_id, key, req.quantity, window_capacity(m, req.deadline_weeks, self.cfg))
            except HoldRejected as exc:
                self._audit("hold_rejected", owner=session_id, key=key, units=req.quantity)
                raise
            self._audit("hold_placed", owner=session_id, key=key, units=h.units)
            return {"session_id": session_id, "key": key, "units": h.units, "expires_at": h.expires_at}
