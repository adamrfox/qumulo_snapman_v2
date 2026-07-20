"""Combined space-freed estimate for an arbitrary set of snapshots.

Generalizes the three-way "delete this one snapshot alone" engine
(compute/snapshot_exclusive.py) to an arbitrary selected SET of snapshots,
answering "if I delete exactly these together, how much do I get back" --
which is not the same question as the reclaim curve (delete everything older
than X) or the per-snapshot exclusive size (delete just one, alone), and
critically is NOT simply the sum of each selected snapshot's individual
exclusive size when two selected snapshots are adjacent to each other.

The math (derived and verified against a real cluster case): split the
selection into maximal contiguous runs, each bounded by two kept snapshots
L (immediately before) and R (immediately after). For one run {X1..Xk}:

    freed(run) = [pair(L,X1) + pair(X1,X2) + ... + pair(Xk,R)]   (adjacent chain)
                 - pair(L,R) computed directly, endpoint-to-endpoint

The adjacent-chain sum captures every byte that dies anywhere in (L, R],
regardless of birth -- including bytes already alive at L that just happen
to die inside the run. Those aren't actually freed (L survives and still
holds them), so the direct L<->R diff -- exactly "bytes present in L, gone
by R" -- is the correction term. For a run of length 1 this algebraically
reduces to exactly compute_freed_bytes(...).s2 from the three-way engine
(pair(L,X1) + pair(X1,R) - pair(L,R) == "exclusive to X1"), so this is a
strict generalization, not a competing formula.

Runs never share data with each other (a kept boundary sits between any
two runs), so totals across runs simply add -- no new math needed there.

Edge case: a run can reach all the way back to the group's actual oldest
snapshot, in which case there's no L to serve as a boundary -- Run.left is
None, and freed(run) is just the plain adjacent-chain sum with no
subtraction. This is legal (unlike touching the newest snapshot, which is
never legal -- there's no live-filesystem diff to compare it against): with
no L, there's nothing before the run that could still be holding the data
alive, so every byte the adjacent-chain sum counts is genuinely freed. This
mirrors why the oldest snapshot's individual size already reuses the plain
pairwise number instead of a three-way diff.

No new diff engine: both terms are just calls to the existing
compute_pair_contribution, cached in the existing pair_contribution table
(keyed by (cluster_name, source_file_id, older_id, newer_id) already --
it doesn't care whether the two ids are "adjacent" in any chain).
"""

import concurrent.futures
import sys
import time

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError, ApiTimeout, Client
from app.qumulo.compute.snapshot_reclaim import (
    Interrupted,
    PairContribution,
    compute_pair_contribution,
)

StopFn = Callable[[], bool]


@dataclass(frozen=True)
class Run:
    # None means the run reaches all the way back to the group's actual oldest
    # snapshot -- there's no kept snapshot before it to serve as a boundary.
    left: Snapshot | None
    right: Snapshot
    deleted: list[Snapshot]

    @property
    def chain(self) -> list[Snapshot]:
        if self.left is None:
            return [*self.deleted, self.right]
        return [self.left, *self.deleted, self.right]

    @property
    def adjacent_pairs(self) -> list[tuple[Snapshot, Snapshot]]:
        chain = self.chain
        return list(zip(chain[:-1], chain[1:]))

    @property
    def direct_pair(self) -> tuple[Snapshot, Snapshot] | None:
        # No left boundary means no correction term: everything that dies
        # anywhere in the run is genuinely freed, since there's nothing
        # before the run to still be holding onto it (same reasoning as why
        # the oldest snapshot's individual size reuses the plain pairwise
        # number instead of a three-way diff).
        if self.left is None:
            return None
        return (self.left, self.right)


class SelectionError(Exception):
    pass


def partition_into_runs(snapshots_sorted: list[Snapshot], selected_ids: set[int]) -> list[Run]:
    """Group selected snapshots into maximal contiguous runs by position in
    the sorted chain. A snapshot NOT in selected_ids (e.g. because it's held,
    or simply unselected) acts as a boundary -- including a held snapshot
    that happens to sit between two selected ones, which naturally splits
    them into two independent runs with no special-casing required here.
    """
    n = len(snapshots_sorted)
    runs: list[Run] = []
    i = 0
    while i < n:
        if snapshots_sorted[i].id not in selected_ids:
            i += 1
            continue
        start = i
        while i < n and snapshots_sorted[i].id in selected_ids:
            i += 1
        end = i - 1
        if end == n - 1:
            raise SelectionError(
                "Selection must not include the newest snapshot -- there's no later "
                "snapshot to diff against."
            )
        # A run reaching all the way back to the group's actual oldest
        # snapshot has no kept left boundary -- that's fine, see Run.direct_pair.
        left = snapshots_sorted[start - 1] if start > 0 else None
        runs.append(
            Run(
                left=left,
                right=snapshots_sorted[end + 1],
                deleted=snapshots_sorted[start : end + 1],
            )
        )
    return runs


def validate_selection(
    snapshots_sorted: list[Snapshot], selected_ids: set[int]
) -> None:
    if not selected_ids:
        raise SelectionError("No snapshots selected.")
    by_id = {s.id: s for s in snapshots_sorted}
    unknown = selected_ids - set(by_id)
    if unknown:
        raise SelectionError(f"Unknown snapshot id(s): {sorted(unknown)}")
    if snapshots_sorted and snapshots_sorted[-1].id in selected_ids:
        raise SelectionError("The newest snapshot can't be included -- there's no later snapshot to diff against.")
    held_selected = [sid for sid in selected_ids if by_id[sid].held]
    if held_selected:
        raise SelectionError(f"Locked/replication-held snapshot(s) can't be deleted: {sorted(held_selected)}")


class DeletionEstimateObserver(Protocol):
    def run_start(self, run_index: int, total_runs: int, run: Run) -> None: ...
    def pair_start(self, older: Snapshot, newer: Snapshot) -> None: ...
    def pair_done(self, older: Snapshot, newer: Snapshot, freed_bytes: int, *, cached: bool) -> None: ...
    def run_result(self, run_index: int, freed_bytes: int | None, error: str | None) -> None: ...
    def estimate_result(self, total_bytes: int, complete: bool) -> None: ...
    def finish(self) -> None: ...


class WebDeletionEstimateObserver:
    """Translates DeletionEstimateObserver calls into push(type, data) SSE events."""

    def __init__(self, push: Callable[[str, dict], None]) -> None:
        self._push = push

    def run_start(self, run_index: int, total_runs: int, run: Run) -> None:
        self._push(
            "run_start",
            {
                "run_index": run_index,
                "total_runs": total_runs,
                "left_id": run.left.id if run.left is not None else None,
                "left_name": run.left.name if run.left is not None else None,
                "right_id": run.right.id, "right_name": run.right.name,
                "deleted_ids": [s.id for s in run.deleted],
                "deleted_names": [s.name for s in run.deleted],
            },
        )

    def pair_start(self, older: Snapshot, newer: Snapshot) -> None:
        self._push(
            "pair_start",
            {
                "older_id": older.id, "older_name": older.name, "older_date": older.timestamp[:10],
                "newer_id": newer.id, "newer_name": newer.name, "newer_date": newer.timestamp[:10],
            },
        )

    def pair_done(self, older: Snapshot, newer: Snapshot, freed_bytes: int, *, cached: bool) -> None:
        self._push(
            "pair_done",
            {"older_id": older.id, "newer_id": newer.id, "freed_bytes": freed_bytes, "cached": cached},
        )

    def run_result(self, run_index: int, freed_bytes: int | None, error: str | None) -> None:
        self._push("run_result", {"run_index": run_index, "freed_bytes": freed_bytes, "error": error})

    def estimate_result(self, total_bytes: int, complete: bool) -> None:
        self._push("estimate_result", {"total_bytes": total_bytes, "complete": complete})

    def finish(self) -> None:
        self._push("finish", {})


def run_deletion_estimate(
    client: Client,
    cache: Cache,
    cluster_name: str,
    source_id: str,
    runs: list[Run],
    *,
    max_workers: int,
    observer: DeletionEstimateObserver,
    should_stop: StopFn,
) -> None:
    for i, run in enumerate(runs):
        observer.run_start(i, len(runs), run)

    cached_pairs = cache.get_pairs(cluster_name, source_id)
    partials = cache.get_partials(cluster_name, source_id)

    # Flatten every (older, newer) pair every run needs, tagged with which
    # run(s) and role (a pair could in principle be needed by only one run --
    # runs are separated by kept boundaries so adjacent/direct pairs never
    # overlap across runs).
    work: list[tuple[Snapshot, Snapshot]] = []
    seen: set[tuple[int, int]] = set()
    for run in runs:
        needed = [*run.adjacent_pairs]
        if run.direct_pair is not None:
            needed.append(run.direct_pair)
        for older, newer in needed:
            key = (older.id, newer.id)
            if key not in seen:
                seen.add(key)
                work.append((older, newer))

    results: dict[tuple[int, int], tuple[int, str | None]] = {}  # key -> (freed_bytes, error)
    sizing_ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def _compute_one(older: Snapshot, newer: Snapshot) -> None:
        key = (older.id, newer.id)
        cached = cached_pairs.get(key)
        if cached is not None:
            observer.pair_start(older, newer)
            observer.pair_done(older, newer, cached[0], cached=True)
            results[key] = (cached[0], None)
            return

        observer.pair_start(older, newer)
        resume = partials.get(key)

        def _checkpoint(cursor: str, freed: int, files: int) -> None:
            cache.put_partial(cluster_name, source_id, older.id, newer.id, cursor, freed, files)

        try:
            result: PairContribution = compute_pair_contribution(
                client, older, newer,
                max_workers=max_workers, should_stop=should_stop,
                resume=resume, checkpoint=_checkpoint,
            )
        except Interrupted:
            return
        except ApiTimeout:
            results[key] = (0, "timed out")
            return
        except Exception as e:
            print(f"[snapman] deletion-estimate pair ({older.id},{newer.id}) failed: {e!r}", file=sys.stderr)
            results[key] = (0, str(e))
            return

        cache.put_pair(cluster_name, source_id, older.id, newer.id, result.freed_bytes, result.total_files)
        cache.delete_partial(cluster_name, source_id, older.id, newer.id)
        results[key] = (result.freed_bytes, None)
        observer.pair_done(older, newer, result.freed_bytes, cached=False)

    try:
        futures = [sizing_ex.submit(_compute_one, older, newer) for older, newer in work]
        for fut in concurrent.futures.as_completed(futures):
            if should_stop():
                break
            fut.result()
    finally:
        sizing_ex.shutdown(wait=False, cancel_futures=True)

    grand_total = 0
    complete = True
    for i, run in enumerate(runs):
        adjacent_sum = 0
        run_error: str | None = None
        for older, newer in run.adjacent_pairs:
            entry = results.get((older.id, newer.id))
            if entry is None or entry[1] is not None:
                run_error = (entry[1] if entry else "not computed") or "not computed"
                break
            adjacent_sum += entry[0]
        direct_freed = 0
        direct_pair = run.direct_pair
        if direct_pair is not None:
            direct_entry = results.get((direct_pair[0].id, direct_pair[1].id))
            if run_error is None and (direct_entry is None or direct_entry[1] is not None):
                run_error = (direct_entry[1] if direct_entry else "not computed") or "not computed"
            elif direct_entry is not None:
                direct_freed = direct_entry[0]

        if run_error is not None:
            complete = False
            observer.run_result(i, None, run_error)
            continue

        freed = adjacent_sum - direct_freed
        grand_total += freed
        observer.run_result(i, freed, None)

    observer.estimate_result(grand_total, complete)
    observer.finish()
