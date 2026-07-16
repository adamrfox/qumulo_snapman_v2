"""Group snapshots by source path. Direct port of qsnap's compute/groups.py."""

import fnmatch

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.qumulo.api import Snapshot
from app.qumulo.paths import paths_nest

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class Group:
    source_file_id: str
    snapshots: list[Snapshot]
    count: int
    min_age_days: int
    max_age_days: int


def _parse_rfc3339(ts: str) -> datetime:
    ts = ts.strip().replace("Z", "+00:00")
    if "." in ts:
        head, frac = ts.split(".", 1)
        digits = ""
        rest = ""
        for i, ch in enumerate(frac):
            if ch.isdigit():
                digits += ch
            else:
                rest = frac[i:]
                break
        ts = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    return datetime.fromisoformat(ts)


def age_days(ts: str, now: datetime) -> int:
    return int((now - _parse_rfc3339(ts)).total_seconds() // _SECONDS_PER_DAY)


def group_snapshots(snapshots: list[Snapshot], now: datetime) -> list[Group]:
    buckets: dict[str, list[Snapshot]] = {}
    for s in snapshots:
        buckets.setdefault(s.source_file_id, []).append(s)

    groups: list[Group] = []
    for source_file_id, snaps in buckets.items():
        snaps = sorted(snaps, key=lambda s: s.id)
        ages = [age_days(s.timestamp, now) for s in snaps]
        groups.append(Group(source_file_id, snaps, len(snaps), min(ages), max(ages)))
    return groups


def overlapped_sources(groups: list[Group], path_of: Callable[[Group], str]) -> set[str]:
    overlapped: set[str] = set()
    for a in groups:
        a_path = path_of(a)
        a_newest = a.snapshots[-1].id
        for b in groups:
            if b.source_file_id == a.source_file_id:
                continue
            if paths_nest(a_path, path_of(b)) and b.snapshots[0].id < a_newest:
                overlapped.add(a.source_file_id)
                break
    return overlapped


@dataclass(frozen=True)
class PrunePrefix:
    pair_ids: list[tuple[int, int]]
    held: Snapshot | None = None

    @property
    def prunable(self) -> int:
        return len(self.pair_ids)


def prune_prefix(group: Group, now: datetime, older_than_days: float) -> PrunePrefix:
    pair_ids: list[tuple[int, int]] = []
    for older, newer in zip(group.snapshots[:-1], group.snapshots[1:]):
        if age_days(older.timestamp, now) <= older_than_days:
            break
        if older.held:
            return PrunePrefix(pair_ids=pair_ids, held=older)
        pair_ids.append((older.id, newer.id))
    return PrunePrefix(pair_ids=pair_ids)


def filter_groups(
    groups: list[Group],
    *,
    older_than: timedelta | None,
    path_glob: str | None,
    path_of: Callable[[Group], str],
) -> list[Group]:
    out: list[Group] = []
    for g in groups:
        if (
            older_than is not None
            and g.max_age_days < older_than.total_seconds() / _SECONDS_PER_DAY
        ):
            continue
        if path_glob is not None and not fnmatch.fnmatch(path_of(g), path_glob):
            continue
        out.append(g)
    return out
