"""Shared collector loop: fetch -> normalize -> write (PLAN.md §5.1).

Every collector (market, on-chain, sentiment) is the same shape — talk to a
source, reshape the reading into table rows, write the rows, sleep, repeat:

    raw  = self.fetch()         # talk to the source
    rows = self.normalize(raw)  # shape into table columns
    self.write(rows)            # insert into SQLite
    time.sleep(self.interval)   # each subclass sets its own pace

Two non-negotiables this base class enforces (PLAN.md §2):
  * **Collectors observe, the brain decides** (FR-DC-5). A collector ONLY writes
    rows; there is no trading/decision code here or in any subclass.
  * **Sources operate independently** (FR-DC-4 / NFR-REL). An exception in one
    cycle is caught and logged, and the loop keeps going — one failing source (or
    pair) never crashes the others.

All cross-component communication goes through the DB (FR-DP-2): a collector's
only output is rows inserted via ``data.db``.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Mapping, Sequence

from data import db


class BaseCollector(ABC):
    """Abstract fetch->normalize->write poller. Subclasses set ``table`` and the
    three steps; everything else (the loop, error isolation, the DB write) is
    shared."""

    #: target table in storage.db — subclasses override (e.g. "market_data").
    table: str = ""

    def __init__(self, conn, interval: float, name: str | None = None) -> None:
        self.conn = conn
        self.interval = interval
        self.name = name or self.__class__.__name__
        self.log = logging.getLogger(f"collector.{self.name}")

    # ── the three steps a subclass implements ──────────────────────
    @abstractmethod
    def fetch(self) -> Any:
        """Talk to the source and return its raw reading. No DB access."""

    @abstractmethod
    def normalize(self, raw: Any) -> list[Mapping[str, Any]]:
        """Shape a raw reading into a list of ``table`` rows (column->value).

        Returns an empty list when there is nothing new to write (e.g. no candle
        has closed since the last cycle) — a normal, non-error outcome.
        """

    # ── shared machinery ───────────────────────────────────────────
    def write(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert normalized rows into ``self.table``; return the count written."""
        if not self.table:
            raise NotImplementedError(f"{self.name} did not set a target table")
        if not rows:
            return 0
        return db.insert_many(self.conn, self.table, rows)

    def run_once(self) -> int:
        """One fetch->normalize->write cycle. Returns rows written this cycle."""
        rows = self.normalize(self.fetch())
        written = self.write(rows)
        if written:
            self.log.info("wrote %d new row(s) to %s", written, self.table)
        return written

    def run(self, stop: "threading.Event | None" = None) -> None:  # noqa: F821
        """Loop forever (or until ``stop`` is set), isolating per-cycle failures.

        A raised exception is logged and swallowed so a transient source outage
        (FR-DC-4) degrades to "skip this cycle", never a crash. ``stop`` lets a
        supervisor (scripts/run_collectors.py) shut the loop down cleanly.
        """
        self.log.info("starting %s (every %ss)", self.name, self.interval)
        while stop is None or not stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — isolate this source; keep others alive
                self.log.exception("cycle failed; continuing")
            if stop is not None:
                stop.wait(self.interval)
            else:
                time.sleep(self.interval)
