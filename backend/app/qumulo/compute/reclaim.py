"""Per-file reclaimable-bytes computation. Direct port of qsnap."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.qumulo.compute import intervals
from app.qumulo.api import DiffOp, FileDiffEntry

_CREATE_OR_MODIFY = (DiffOp.CREATE, DiffOp.MODIFY)
_MODIFY_OR_DELETE = (DiffOp.MODIFY, DiffOp.DELETE)


def target_not_in_newer(
    diff_target_to_newer: Iterable[FileDiffEntry],
) -> list[intervals.Interval]:
    return _ranges_for_ops(diff_target_to_newer, _MODIFY_OR_DELETE)


@dataclass(frozen=True)
class FreedBytes:
    s1: int
    s2: int
    s3: int


def _ranges_for_ops(
    diff: Iterable[FileDiffEntry], allowed_ops: Sequence[DiffOp]
) -> list[intervals.Interval]:
    return [(e.offset, e.size) for e in diff if e.op in allowed_ops]


def compute_freed_bytes(
    diff_s1_s2: Sequence[FileDiffEntry],
    diff_s2_s3: Sequence[FileDiffEntry],
) -> FreedBytes:
    s2_not_in_s1 = _ranges_for_ops(diff_s1_s2, _CREATE_OR_MODIFY)
    s2_not_in_s3 = _ranges_for_ops(diff_s2_s3, _MODIFY_OR_DELETE)
    return FreedBytes(
        s1=intervals.total_size(_ranges_for_ops(diff_s1_s2, _MODIFY_OR_DELETE)),
        s2=intervals.intersection_size(s2_not_in_s1, s2_not_in_s3),
        s3=intervals.total_size(_ranges_for_ops(diff_s2_s3, _CREATE_OR_MODIFY)),
    )
