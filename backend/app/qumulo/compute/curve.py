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


ReclaimRow = tuple[int, str, int, int, int]


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
        rows.append((p["newer_age_days"], p["newer_date"], buf[-1] + 1, p["cumulative_bytes"], p["newer_id"]))

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


def build_points(
    snaps_sorted: Sequence[Snapshot],
    pairs: dict[tuple[int, int], tuple[int, int]],
    now: datetime,
) -> tuple[list[dict], int]:
    """Build a curve's points from a tree's snapshots (sorted oldest-to-newest)
    and its cached per-pair (freed_bytes, total_files) results. Mirrors the
    original inline loop in the /curve endpoint -- cumulative stops
    accumulating (None) once the first unmeasured pair is hit, since a
    cumulative total that silently skipped an unmeasured gap would be wrong,
    not just incomplete."""
    curve = CurveModel(now)
    cumulative = 0
    known = True
    for older, newer in zip(snaps_sorted[:-1], snaps_sorted[1:]):
        pair_data = pairs.get((older.id, newer.id))
        if pair_data is None:
            known = False
            curve.add(older, newer, None, None, None, cached=False, pending=True)
        else:
            freed, files = pair_data
            if known:
                cumulative += freed
            curve.add(
                older,
                newer,
                freed,
                cumulative if known else None,
                files,
                cached=True,
                pending=False,
            )
    unmeasured = sum(1 for p in curve.points if p["status"] in ("pending", "timed_out"))
    return curve.points, unmeasured


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
