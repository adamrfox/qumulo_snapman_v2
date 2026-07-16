"""Reclaim curve model. Direct port of qsnap's compute/curve.py."""

from collections.abc import Sequence
from datetime import datetime

from app.qumulo.api import Snapshot
from app.qumulo.compute.groups import age_days

DEFAULT_GROUP_BYTES = 1 << 30


def _point(
    older: Snapshot,
    newer: Snapshot,
    now: datetime,
    freed_bytes: int | None,
    cumulative: int | None,
    total_files: int | None,
    status: str,
) -> dict:
    return {
        "older_id": older.id,
        "older_name": older.name,
        "older_age_days": age_days(older.timestamp, now),
        "older_date": older.timestamp[:10],
        "newer_id": newer.id,
        "newer_date": newer.timestamp[:10],
        "newer_age_days": age_days(newer.timestamp, now),
        "freed_bytes": freed_bytes,
        "cumulative_bytes": cumulative,
        "total_files": total_files,
        "status": status,
    }


ReclaimRow = tuple[int, str, int, int]


def reclaim_rows(
    points: Sequence[dict], *, group_bytes: int = DEFAULT_GROUP_BYTES
) -> tuple[list[ReclaimRow], int]:
    sized: list[dict] = []
    for p in points:
        if p["status"] in ("pending", "timed_out"):
            break
        sized.append(p)

    def expressible(p: dict) -> bool:
        return p["older_date"] != p["newer_date"]

    rows: list[ReclaimRow] = []
    buf: list[int] = []

    def flush() -> None:
        if not buf:
            return
        p = sized[buf[-1]]
        rows.append((p["newer_age_days"], p["newer_date"], buf[-1] + 1, p["cumulative_bytes"]))

    for j, p in enumerate(sized):
        freed = p["freed_bytes"] or 0
        if buf and freed > group_bytes and expressible(sized[buf[-1]]):
            flush()
            buf = []
        buf.append(j)
        if expressible(p) and sum((sized[k]["freed_bytes"] or 0) for k in buf) > group_bytes:
            flush()
            buf = []
    while buf and not expressible(sized[buf[-1]]):
        buf.pop()
    flush()
    return rows, len(points) - len(sized)


class CurveModel:
    def __init__(self, now: datetime) -> None:
        self._now = now
        self.points: list[dict] = []

    def add(
        self,
        older: Snapshot,
        newer: Snapshot,
        freed_bytes: int | None,
        cumulative: int | None,
        total_files: int | None,
        *,
        cached: bool,
        pending: bool,
        timed_out: bool = False,
    ) -> None:
        status = (
            "timed_out" if timed_out
            else "pending" if pending
            else "cached" if cached
            else "computed"
        )
        self.points.append(
            _point(older, newer, self._now, freed_bytes, cumulative, total_files, status)
        )
