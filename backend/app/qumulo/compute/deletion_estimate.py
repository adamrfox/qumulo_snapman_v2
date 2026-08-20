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

As of the fix below, the subtraction step here is never actually reached:
every run with a real left boundary (any length) is intercepted upstream by
a dedicated engine before hitting this arithmetic at all. It's kept as the
formula's derivation and as the still-genuinely-used no-subtraction path for
a run touching the group's oldest snapshot (see the edge case just below) --
not dead weight, just no longer doing double duty as the general case too.

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

CAVEAT (confirmed against a real cluster, 2026-08-20): the sum-and-subtract
formula above sums and subtracts *total byte counts* from three
independently-measured diffs. That's only equal to the true byte-range
intersection when every diff's changed extents land at identical
offsets across all three comparisons -- true for a single cleanly-aligned
extent (and for the FoxDemo-style whole-file create/delete case), but not
guaranteed for a heavily-rewritten file (a database, a VM image) whose
changed regions shift around between L->X1, X1->R, and the direct L->R
comparison. When that happens, the sum-and-subtract total can diverge
substantially -- and did: a real tree's per-row "Individual size" (the
three-way engine, byte-range-intersection-based) showed ~12 GiB for one
snapshot while "Delete selected" for that same snapshot alone reported
~29 GiB via this formula, for the exact same (prev, target, next) triple.

Fix: a run of exactly one deleted snapshot with a real left boundary
(Run.is_single_with_left_boundary) -- by far the most common shape, since
it's what "check one box, hit Delete selected" produces -- is routed
straight to compute_snapshot_exclusive_contribution (the already-correct,
already-cached, already-unit-tested three-way engine) instead of through
the arithmetic below. A run of two or more deleted snapshots with a real
left boundary (Run.needs_run_exclusive_engine) is routed to
compute/run_exclusive.py's compute_run_exclusive_contribution instead --
a proper generalization of the three-way engine's byte-range logic to an
arbitrary chain, not more total-summing (see that module's docstring for
why the same sum-and-subtract trap reappears, worse, once there's more
than one deleted snapshot in between, and why fixing it needs a real
interval sweep). A run touching the group's actual oldest snapshot
(Run.left is None) needs neither fix and keeps using the plain
adjacent-chain sum below regardless of length: with no left boundary to
subtract against, every hop's diff is measured and summed independently
with no cross-hop total-arithmetic, so the offset-alignment bug can't
arise there in the first place (see intervals.run_exclusive_size's
docstring for the precise reason mixing hop totals is unsafe in general).
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
from app.qumulo.compute.run_exclusive import compute_run_exclusive_contribution
from app.qumulo.compute.snapshot_exclusive import compute_snapshot_exclusive_contribution
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

    @property
    def is_single_with_left_boundary(self) -> bool:
        """True for exactly the case compute_snapshot_exclusive_contribution
        (the three-way engine) was built for: one deleted snapshot with a
        real kept neighbor on both sides. run_deletion_estimate routes these
        straight to that engine instead of the sum-and-subtract formula
        below -- see this module's docstring CAVEAT for why."""
        return len(self.deleted) == 1 and self.left is not None

    @property
    def needs_run_exclusive_engine(self) -> bool:
        """True for a run of two or more deleted snapshots with a real left
        boundary -- routed to compute/run_exclusive.py's engine instead of
        the sum-and-subtract formula below. See this module's docstring."""
        return len(self.deleted) >= 2 and self.left is not None


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
    def triple_start(self, older: Snapshot, target: Snapshot, newer: Snapshot) -> None: ...
    def triple_done(
        self, older: Snapshot, target: Snapshot, newer: Snapshot, exclusive_bytes: int, *, cached: bool
    ) -> None: ...
    def multi_start(self, left: Snapshot, deleted: list[Snapshot], right: Snapshot) -> None: ...
    def multi_done(
        self, left: Snapshot, deleted: list[Snapshot], right: Snapshot, exclusive_bytes: int, *, cached: bool
    ) -> None: ...
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

    def triple_start(self, older: Snapshot, target: Snapshot, newer: Snapshot) -> None:
        self._push(
            "triple_start",
            {
                "older_id": older.id, "older_name": older.name, "older_date": older.timestamp[:10],
                "target_id": target.id, "target_name": target.name, "target_date": target.timestamp[:10],
                "newer_id": newer.id, "newer_name": newer.name, "newer_date": newer.timestamp[:10],
            },
        )

    def triple_done(
        self, older: Snapshot, target: Snapshot, newer: Snapshot, exclusive_bytes: int, *, cached: bool
    ) -> None:
        self._push(
            "triple_done",
            {
                "older_id": older.id, "target_id": target.id, "newer_id": newer.id,
                "exclusive_bytes": exclusive_bytes, "cached": cached,
            },
        )

    def multi_start(self, left: Snapshot, deleted: list[Snapshot], right: Snapshot) -> None:
        self._push(
            "multi_start",
            {
                "left_id": left.id, "left_name": left.name, "left_date": left.timestamp[:10],
                "deleted_ids": [s.id for s in deleted], "deleted_names": [s.name for s in deleted],
                "right_id": right.id, "right_name": right.name, "right_date": right.timestamp[:10],
            },
        )

    def multi_done(
        self, left: Snapshot, deleted: list[Snapshot], right: Snapshot, exclusive_bytes: int, *, cached: bool
    ) -> None:
        self._push(
            "multi_done",
            {
                "left_id": left.id, "deleted_ids": [s.id for s in deleted], "right_id": right.id,
                "exclusive_bytes": exclusive_bytes, "cached": cached,
            },
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
    force_recompute: bool = False,
) -> None:
    """force_recompute skips reading any cached pair/triple/run result for
    this call -- everything gets recomputed from the cluster -- but still
    writes the fresh results back to cache afterward, same as a normal run.
    A cached value here can only ever be *correct forever* (it's a
    deterministic property of two or more specific immutable snapshots, not
    something that can drift), so this exists as a manual escape hatch for
    "I don't trust a value I'm seeing" rather than because cache entries
    actually go stale -- one forced recompute repairs it for every future
    estimate too, not just this one."""
    for i, run in enumerate(runs):
        observer.run_start(i, len(runs), run)

    cached_pairs = {} if force_recompute else cache.get_pairs(cluster_name, source_id)
    partials = cache.get_partials(cluster_name, source_id)
    cached_triples = {} if force_recompute else cache.get_triples(cluster_name, source_id)
    triple_partials = cache.get_triple_partials(cluster_name, source_id)

    # Flatten every (older, newer) pair every run needs, tagged with which
    # run(s) and role (a pair could in principle be needed by only one run --
    # runs are separated by kept boundaries so adjacent/direct pairs never
    # overlap across runs). Runs routed to the three-way engine (see module
    # docstring CAVEAT) contribute nothing here -- they need no pairwise
    # diffs at all, not even a direct L<->R one.
    work: list[tuple[Snapshot, Snapshot]] = []
    seen: set[tuple[int, int]] = set()
    triple_run_indices: list[int] = []
    multi_run_indices: list[int] = []
    for i, run in enumerate(runs):
        if run.is_single_with_left_boundary:
            triple_run_indices.append(i)
            continue
        if run.needs_run_exclusive_engine:
            multi_run_indices.append(i)
            continue
        needed = [*run.adjacent_pairs]
        if run.direct_pair is not None:
            needed.append(run.direct_pair)
        for older, newer in needed:
            key = (older.id, newer.id)
            if key not in seen:
                seen.add(key)
                work.append((older, newer))

    results: dict[tuple[int, int], tuple[int, str | None]] = {}  # key -> (freed_bytes, error)
    triple_results: dict[int, tuple[int, str | None]] = {}  # run_index -> (exclusive_bytes, error)
    multi_results: dict[int, tuple[int, str | None]] = {}  # run_index -> (exclusive_bytes, error)
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

    def _compute_triple(run_index: int) -> None:
        run = runs[run_index]
        prev, target, next_ = run.left, run.deleted[0], run.right
        assert prev is not None  # guaranteed by is_single_with_left_boundary
        key = (prev.id, target.id, next_.id)

        cached = cached_triples.get(key)
        if cached is not None:
            observer.triple_start(prev, target, next_)
            observer.triple_done(prev, target, next_, cached[0], cached=True)
            triple_results[run_index] = (cached[0], None)
            return

        observer.triple_start(prev, target, next_)
        resume = triple_partials.get(key)

        def _checkpoint(sized_index: int, exclusive: int, files: int) -> None:
            cache.put_triple_partial(
                cluster_name, source_id, prev.id, target.id, next_.id, sized_index, exclusive, files,
            )

        try:
            result = compute_snapshot_exclusive_contribution(
                client, prev, target, next_,
                max_workers=max_workers, should_stop=should_stop,
                resume=resume, checkpoint=_checkpoint,
            )
        except Interrupted:
            return
        except Exception as e:
            print(
                f"[snapman] deletion-estimate triple ({prev.id},{target.id},{next_.id}) failed: {e!r}",
                file=sys.stderr,
            )
            triple_results[run_index] = (0, str(e))
            return

        cache.put_triple(
            cluster_name, source_id, prev.id, target.id, next_.id,
            result.exclusive_bytes, result.total_files,
        )
        cache.delete_triple_partial(cluster_name, source_id, prev.id, target.id, next_.id)
        triple_results[run_index] = (result.exclusive_bytes, None)
        observer.triple_done(prev, target, next_, result.exclusive_bytes, cached=False)

    def _compute_multi(run_index: int) -> None:
        run = runs[run_index]
        assert run.left is not None  # guaranteed by needs_run_exclusive_engine
        deleted_ids = [s.id for s in run.deleted]

        if not force_recompute:
            cached = cache.get_run(cluster_name, source_id, run.left.id, run.right.id, deleted_ids)
            if cached is not None:
                observer.multi_start(run.left, run.deleted, run.right)
                multi_results[run_index] = (cached[0], None)
                observer.multi_done(run.left, run.deleted, run.right, cached[0], cached=True)
                return

        observer.multi_start(run.left, run.deleted, run.right)
        try:
            result = compute_run_exclusive_contribution(
                client, run.chain, max_workers=max_workers, should_stop=should_stop,
            )
        except Interrupted:
            return
        except Exception as e:
            print(
                f"[snapman] deletion-estimate multi-run ({run.left.id}..{run.right.id}) failed: {e!r}",
                file=sys.stderr,
            )
            multi_results[run_index] = (0, str(e))
            return

        cache.put_run(
            cluster_name, source_id, run.left.id, run.right.id, deleted_ids,
            result.exclusive_bytes, result.total_files,
        )
        multi_results[run_index] = (result.exclusive_bytes, None)
        observer.multi_done(run.left, run.deleted, run.right, result.exclusive_bytes, cached=False)

    try:
        futures = [sizing_ex.submit(_compute_one, older, newer) for older, newer in work]
        futures += [sizing_ex.submit(_compute_triple, i) for i in triple_run_indices]
        futures += [sizing_ex.submit(_compute_multi, i) for i in multi_run_indices]
        for fut in concurrent.futures.as_completed(futures):
            if should_stop():
                break
            fut.result()
    finally:
        sizing_ex.shutdown(wait=False, cancel_futures=True)

    grand_total = 0
    complete = True
    for i, run in enumerate(runs):
        if run.is_single_with_left_boundary:
            entry = triple_results.get(i)
            if entry is None or entry[1] is not None:
                complete = False
                observer.run_result(i, None, (entry[1] if entry else "not computed") or "not computed")
                continue
            freed = entry[0]
            grand_total += freed
            observer.run_result(i, freed, None)
            continue

        if run.needs_run_exclusive_engine:
            entry = multi_results.get(i)
            if entry is None or entry[1] is not None:
                complete = False
                observer.run_result(i, None, (entry[1] if entry else "not computed") or "not computed")
                continue
            freed = entry[0]
            grand_total += freed
            observer.run_result(i, freed, None)
            continue

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
