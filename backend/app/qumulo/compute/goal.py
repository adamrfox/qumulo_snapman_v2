"""Cluster-wide space-recovery goal allocator.

Given a target byte count and, for each candidate tree, its full reclaim
curve (see compute/curve.py's `points`), decide how deep to cut in each tree
to reach the target while sacrificing as little total recency as possible.
Cuts are uneven across trees on purpose: a tree that frees a lot of bytes for
very little history given up is cut deeper before a tree that only frees a
little for the same amount of history sacrificed.
"""

import heapq
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TreeInput:
    source_file_id: str
    points: list[dict]  # oldest -> newest, every point fully measured


@dataclass
class TreeAllocation:
    source_file_id: str
    deepest_index: int | None
    delete_snapshot_ids: list[int] = field(default_factory=list)
    keep_days: int | None = None
    delete_before: str | None = None
    delete_count: int = 0
    reclaim_bytes: int = 0
    days_sacrificed: int = 0


@dataclass
class GoalResult:
    goal_met: bool
    target_bytes: int
    total_freed_bytes: int
    shortfall: int
    allocations: list[TreeAllocation]


def _cost_gain(points: list[dict], cursor: int, prev_retained_days: int) -> tuple[int, int]:
    p = points[cursor]
    gain = p["freed_bytes"]
    cost = prev_retained_days - p["newer_age_days"]
    return cost, gain


def _priority(cost: int, gain: int, source_file_id: str, cursor: int) -> tuple:
    # Free steps (no recency actually sacrificed) always win. Among the rest,
    # rank by descending bytes-per-day; the source_file_id/cursor tail makes
    # the ordering fully deterministic when efficiency ties.
    if cost == 0:
        return (0, 0.0, source_file_id, cursor)
    return (1, -(gain / cost), source_file_id, cursor)


def allocate(target_bytes: int, trees: list[TreeInput]) -> GoalResult:
    for tree in trees:
        for p in tree.points:
            if p["status"] in ("pending", "timed_out"):
                raise ValueError(
                    f"tree {tree.source_file_id} has an unmeasured point -- "
                    "only fully-measured trees can be allocated"
                )

    allocations = {
        t.source_file_id: TreeAllocation(source_file_id=t.source_file_id, deepest_index=None)
        for t in trees
    }
    retained_days = {
        t.source_file_id: (t.points[0]["older_age_days"] if t.points else 0) for t in trees
    }
    cursors = {t.source_file_id: 0 for t in trees}
    by_id = {t.source_file_id: t for t in trees}

    heap: list[tuple] = []
    for t in trees:
        if t.points:
            cost, gain = _cost_gain(t.points, 0, retained_days[t.source_file_id])
            heapq.heappush(heap, _priority(cost, gain, t.source_file_id, 0) + (cost, gain))

    taken: list[tuple[str, int, int]] = []  # (source_file_id, cursor, gain), in take order
    total = 0
    while heap and total < target_bytes:
        *priority_key, cost, gain = heapq.heappop(heap)
        source_file_id = priority_key[2]
        cursor = priority_key[3]
        tree = by_id[source_file_id]

        # Stale entry (tree already advanced past this cursor since it was
        # pushed) -- skip it rather than double-counting.
        if cursor != cursors[source_file_id]:
            continue

        total += gain
        taken.append((source_file_id, cursor, gain))
        retained_days[source_file_id] = tree.points[cursor]["newer_age_days"]
        cursors[source_file_id] = cursor + 1

        alloc = allocations[source_file_id]
        alloc.deepest_index = cursor
        alloc.delete_snapshot_ids = [p["older_id"] for p in tree.points[: cursor + 1]]
        alloc.keep_days = tree.points[cursor]["newer_age_days"]
        alloc.delete_before = tree.points[cursor]["newer_date"]
        alloc.delete_count = cursor + 1
        alloc.reclaim_bytes = tree.points[cursor]["cumulative_bytes"]
        alloc.days_sacrificed = tree.points[0]["older_age_days"] - alloc.keep_days

        next_cursor = cursor + 1
        if next_cursor < len(tree.points):
            next_cost, next_gain = _cost_gain(tree.points, next_cursor, retained_days[source_file_id])
            heapq.heappush(
                heap,
                _priority(next_cost, next_gain, source_file_id, next_cursor) + (next_cost, next_gain),
            )

    # The forward pass takes whichever step is cheapest (by recency cost)
    # next, without ever checking whether it'll turn out to be needed --
    # "costs nothing" isn't the same as "necessary." Walk the steps actually
    # taken in reverse (i.e. the greedy algorithm's own least-preferred picks
    # first) and drop any step that turns out redundant: one that reclaimed
    # nothing at all, or one whose bytes weren't actually needed once a
    # later, possibly costlier step already covers the goal alone. Reverse
    # order matters -- it's what keeps a genuinely necessary big cut from
    # being dropped in favor of a smaller "free" one that only looked
    # necessary before the real cut was counted.
    for source_file_id, cursor, gain in reversed(taken):
        alloc = allocations[source_file_id]
        if alloc.deepest_index != cursor:
            continue  # superseded by a later step in this same tree that was already dropped
        if gain != 0 and total - gain < target_bytes:
            continue  # still needed to reach the goal
        total -= gain
        prev_cursor = cursor - 1
        if prev_cursor < 0:
            alloc.deepest_index = None
            alloc.delete_snapshot_ids = []
            alloc.keep_days = None
            alloc.delete_before = None
            alloc.delete_count = 0
            alloc.reclaim_bytes = 0
            alloc.days_sacrificed = 0
        else:
            tree = by_id[source_file_id]
            p = tree.points[prev_cursor]
            alloc.deepest_index = prev_cursor
            alloc.delete_snapshot_ids = [pp["older_id"] for pp in tree.points[: prev_cursor + 1]]
            alloc.keep_days = p["newer_age_days"]
            alloc.delete_before = p["newer_date"]
            alloc.delete_count = prev_cursor + 1
            alloc.reclaim_bytes = p["cumulative_bytes"]
            alloc.days_sacrificed = tree.points[0]["older_age_days"] - alloc.keep_days

    goal_met = total >= target_bytes
    shortfall = max(0, target_bytes - total)
    return GoalResult(
        goal_met=goal_met,
        target_bytes=target_bytes,
        total_freed_bytes=total,
        shortfall=shortfall,
        allocations=list(allocations.values()),
    )
