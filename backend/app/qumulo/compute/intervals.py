"""Pure interval arithmetic over (offset, size) byte ranges. Direct port of qsnap."""

from collections.abc import Sequence

Interval = tuple[int, int]


def total_size(intervals: Sequence[Interval]) -> int:
    return sum(size for _offset, size in intervals)


def intersection_size(a: Sequence[Interval], b: Sequence[Interval]) -> int:
    total = 0
    for offset_a, size_a in a:
        end_a = offset_a + size_a
        for offset_b, size_b in b:
            start = max(offset_a, offset_b)
            end = min(end_a, offset_b + size_b)
            if end > start:
                total += end - start
    return total
