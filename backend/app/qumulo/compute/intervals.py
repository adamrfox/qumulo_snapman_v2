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


# hop event: (offset, size, starts_new_instance). Every event ends whatever
# instance currently occupies its range; starts_new_instance says whether a
# fresh instance begins there afterward (True for CREATE/MODIFY, False for
# DELETE, which just empties the range until something creates it again).
HopEvent = tuple[int, int, bool]


def run_exclusive_size(hops: Sequence[Sequence[HopEvent]]) -> int:
    """Generalizes intersection_size's two-diff "born since L, dead by R"
    check (used by the three-way engine for one deleted snapshot) to an
    arbitrary chain of len(hops)-1 deleted snapshots between two kept
    boundaries L and R.

    `hops[j]` is every changed range at hop j = chain[j] -> chain[j+1], for
    j = 0 .. len(hops)-1 (chain = [L, X1, .., Xk, R], so there are k+1 hops
    and hops[0] is L->X1, hops[-1] is Xk->R). A range not mentioned at a
    given hop is unchanged there -- whatever instance occupies it continues.

    Byte-range instances are assumed to have a single contiguous lifetime
    (the same "birth-epoch monotonicity" the rest of this codebase already
    assumes -- see reclaim.py/snapshot_exclusive.py): a range's occupant only
    changes at the exact hop a diff entry names, and stays put otherwise.
    Bytes count as freed by deleting X1..Xk exactly when the instance
    occupying them is born strictly after L (some hop's create/modify) and
    ends strictly before R survives it (some hop's modify/delete, at or
    before the last hop) -- i.e. neither L nor R ever references it.

    This can't be computed by summing/subtracting per-hop *totals* (that's
    exactly the bug this function exists to avoid -- see
    compute/deletion_estimate.py's module docstring): the same hop's single
    diff entry is simultaneously "this range's old instance died here" and
    "its new instance was born here", and only tracking actual instance
    identity across the whole chain avoids miscounting when a byte round-trips
    or when two DIFFERENT instances' events happen to land on the same range
    at different hops. So this does a real sweep instead.
    """
    k = len(hops) - 1  # number of deleted snapshots
    if k < 1:
        return 0

    breakpoints: set[int] = set()
    for hop in hops:
        for offset, size, _ in hop:
            breakpoints.add(offset)
            breakpoints.add(offset + size)
    if len(breakpoints) < 2:
        return 0
    ordered = sorted(breakpoints)

    total = 0
    for start, end in zip(ordered[:-1], ordered[1:]):
        length = end - start
        # birth_index is which chain position's content currently occupies
        # this atomic sub-range (0 == still L's); None means empty (deleted,
        # nothing created since). Every atomic sub-range is either fully
        # covered or fully uncovered by any one hop's event, by construction
        # of the breakpoints above, so checking a single point suffices.
        birth_index: int | None = 0
        for hop_index, hop in enumerate(hops):
            event = _covering_event(hop, start)
            if event is None:
                continue  # unchanged this hop -- current instance continues
            _, _, starts_new_instance = event
            if birth_index is not None and 1 <= birth_index <= k:
                total += length  # instance born within the run just died -- exclusive
            birth_index = hop_index + 1 if starts_new_instance else None
        # Whatever's still open at the end survived through to R -- not exclusive.
    return total


def _covering_event(hop: Sequence[HopEvent], point: int) -> HopEvent | None:
    for offset, size, starts_new_instance in hop:
        if offset <= point < offset + size:
            return (offset, size, starts_new_instance)
    return None
