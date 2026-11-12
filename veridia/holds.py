"""Soft capacity holds.

Policy (documented in the README):
  * Matching is read-only and non-binding: seeing a supplier does not reserve it.
  * A buyer reserves capacity only by placing an explicit hold. Holds are first-come-first-served,
    applied atomically under one lock, and expire on their own (TTL from config).
  * Other buyers' later matches see the reduced capacity and are warned about it.
  * A buyer never sees who holds what, only how many units are held.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


class HoldRejected(Exception):
    def __init__(self, key: str, requested: int, available: float):
        self.key, self.requested, self.available = key, requested, available
        super().__init__(f"cannot hold {requested:,} units at {key}: only {max(available, 0):,.0f} available")


@dataclass(frozen=True)
class Hold:
    owner: str
    key: str
    units: int
    expires_at: float


class HoldRegistry:
    def __init__(self, ttl_seconds: float, clock: Callable[[], float] = time.time):
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._holds: dict[tuple[str, str], Hold] = {}

    def _purge(self, now: float) -> None:
        for k in [k for k, h in self._holds.items() if h.expires_at <= now]:
            del self._holds[k]

    def snapshot(self, exclude_owner: str | None = None) -> dict[str, int]:
        """Units currently held per supplier by everyone except `exclude_owner`, read atomically."""
        with self._lock:
            self._purge(self._clock())
            out: dict[str, int] = {}
            for (owner, key), h in self._holds.items():
                if owner != exclude_owner:
                    out[key] = out.get(key, 0) + h.units
            return out

    def try_hold(self, owner: str, key: str, units: int, window_capacity: float) -> Hold:
        """Atomically reserve `units` of `key`'s `window_capacity`. Replaces the owner's previous hold on the key."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            others = sum(h.units for (o, k), h in self._holds.items() if k == key and o != owner)
            available = window_capacity - others
            if units > available:
                raise HoldRejected(key, units, available)
            hold = Hold(owner, key, units, now + self._ttl)
            self._holds[(owner, key)] = hold
            return hold

    def release_owner(self, owner: str) -> int:
        with self._lock:
            mine = [k for k in self._holds if k[0] == owner]
            for k in mine:
                del self._holds[k]
            return len(mine)

    def active(self) -> list[Hold]:
        with self._lock:
            self._purge(self._clock())
            return sorted(self._holds.values(), key=lambda h: (h.key, h.owner))
