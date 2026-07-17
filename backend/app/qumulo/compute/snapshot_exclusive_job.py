"""Streaming, interruptible per-snapshot exclusive-size engine.

Mirrors compute/inspect.py's run_inspect, but walks snapshot *triples*
(prev, target, next) instead of pairs, since "how much does deleting this one
snapshot alone free" needs both neighbors. As a side effect it also ensures
the oldest-snapshot boundary value is cached using the existing pairwise
engine (that value needs no three-way math -- there's no older sibling to
complicate it) so this job alone produces a complete table without requiring
the user to have run "Inspect" first.
"""

import concurrent.futures
import sys
import time

from collections.abc import Callable
from typing import Protocol

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError, ApiTimeout, Client
from app.qumulo.compute.snapshot_exclusive import (
    Interrupted,
    SnapshotExclusiveContribution,
    compute_snapshot_exclusive_contribution,
)
from app.qumulo.compute.snapshot_reclaim import PairContribution, compute_pair_contribution

PushFn = Callable[[str, dict], None]


class TripleObserver(Protocol):
    def start_boundary(self, older: Snapshot, newer: Snapshot) -> None: ...
    def boundary_result(
        self,
        older: Snapshot,
        freed_bytes: int | None,
        total_files: int | None,
        *,
        cached: bool,
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None: ...
    def start_triple(self, index: int, total: int, prev: Snapshot, target: Snapshot, next: Snapshot) -> None: ...
    def triple_finished(self, index: int) -> None: ...
    def progress(self, index: int, found: int, sized: int) -> None: ...
    def triple_sized(self, exclusive_bytes: int) -> None: ...
    def triple_result(
        self,
        target: Snapshot,
        exclusive_bytes: int | None,
        total_files: int | None,
        *,
        cached: bool,
        pending: bool,
        timed_out: bool = False,
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None: ...
    def no_middle_snapshots(self) -> None: ...
    def finish(self) -> None: ...


class WebTripleObserver:
    """Translates TripleObserver calls into push(type, data) SSE events.

    Stateless by design: with triple_workers > 1, several triples' _TripleProgress
    instances call in concurrently, so found/sized/index must live on the
    per-triple caller (see _TripleProgress below), never here.
    """

    def __init__(self, push: PushFn) -> None:
        self._push = push

    def start_boundary(self, older: Snapshot, newer: Snapshot) -> None:
        self._push(
            "boundary_start",
            {
                "older_id": older.id, "older_date": older.timestamp[:10],
                "newer_id": newer.id, "newer_date": newer.timestamp[:10],
            },
        )

    def boundary_result(
        self,
        older: Snapshot,
        freed_bytes: int | None,
        total_files: int | None,
        *,
        cached: bool,
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None:
        self._push(
            "boundary_result",
            {
                "older_id": older.id, "freed_bytes": freed_bytes, "total_files": total_files,
                "cached": cached, "error": error, "skipped_held": skipped_held,
            },
        )

    def start_triple(
        self, index: int, total: int, prev: Snapshot, target: Snapshot, next: Snapshot
    ) -> None:
        self._push(
            "triple_start",
            {
                "index": index, "total": total,
                "prev_id": prev.id, "prev_name": prev.name,
                "target_id": target.id, "target_name": target.name, "target_date": target.timestamp[:10],
                "next_id": next.id, "next_name": next.name,
            },
        )

    def triple_finished(self, index: int) -> None:
        self._push("triple_finished", {"index": index})

    def progress(self, index: int, found: int, sized: int) -> None:
        self._push("progress", {"index": index, "found": found, "sized": sized})

    def triple_sized(self, exclusive_bytes: int) -> None:
        self._push("discovered", {"exclusive_bytes": exclusive_bytes})

    def triple_result(
        self,
        target: Snapshot,
        exclusive_bytes: int | None,
        total_files: int | None,
        *,
        cached: bool,
        pending: bool,
        timed_out: bool = False,
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None:
        self._push(
            "triple_result",
            {
                "target_id": target.id,
                "target_date": target.timestamp[:10],
                "target_name": target.name,
                "exclusive_bytes": exclusive_bytes,
                "total_files": total_files,
                "cached": cached,
                "pending": pending,
                "timed_out": timed_out,
                "error": error,
                "skipped_held": skipped_held,
            },
        )

    def no_middle_snapshots(self) -> None:
        self._push("no_middle_snapshots", {})

    def finish(self) -> None:
        self._push("finish", {})


_PROGRESS_PUSH_INTERVAL = 2.0  # seconds; caps how often "progress" fires during a big triple


class _TripleProgress:
    """Per-triple progress state -- see WebTripleObserver's docstring for why
    this can't live on the shared observer."""

    def __init__(self, observer: TripleObserver, index: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._observer = observer
        self._index = index
        self._found = 0
        self._sized = 0
        self._clock = clock
        self._last_push = clock()

    def candidate_found(self) -> None:
        self._found += 1
        self._maybe_push()

    def candidate_sized(self) -> None:
        self._sized += 1
        self._maybe_push()

    def _maybe_push(self) -> None:
        now = self._clock()
        if now - self._last_push >= _PROGRESS_PUSH_INTERVAL:
            self._last_push = now
            self._observer.progress(self._index, self._found, self._sized)

    def enumeration_done(self) -> None:
        self._observer.progress(self._index, self._found, self._sized)


def _ensure_boundary_cached(
    client: Client,
    cache: Cache,
    cluster_name: str,
    source_id: str,
    older: Snapshot,
    newer: Snapshot,
    *,
    max_workers: int,
    should_stop: Callable[[], bool],
    observer: TripleObserver,
    include_held: bool = False,
) -> None:
    cached = cache.get_pairs(cluster_name, source_id).get((older.id, newer.id))
    if cached is not None:
        observer.start_boundary(older, newer)
        observer.boundary_result(older, cached[0], cached[1], cached=True)
        return

    observer.start_boundary(older, newer)

    if not include_held and older.held:
        # older can't actually be deleted -- skip by default, same policy as
        # run_inspect's held-pair skip.
        observer.boundary_result(older, None, None, cached=False, skipped_held=True)
        return

    partials = cache.get_partials(cluster_name, source_id)
    resume = partials.get((older.id, newer.id))

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
    except Exception as e:
        # The boundary is one value among many this job produces -- a failure
        # here (e.g. the newer snapshot expired mid-diff) must not prevent
        # the middle-snapshot triples from being computed.
        print(f"[snapman] boundary pair failed: {e!r}", file=sys.stderr)
        observer.boundary_result(older, None, None, cached=False, error=str(e))
        return
    cache.put_pair(cluster_name, source_id, older.id, newer.id, result.freed_bytes, result.total_files)
    cache.delete_partial(cluster_name, source_id, older.id, newer.id)
    observer.boundary_result(older, result.freed_bytes, result.total_files, cached=False)


def run_snapshot_exclusive(
    client: Client,
    cache: Cache,
    cluster_name: str,
    snapshots: list[Snapshot],
    *,
    limit: int,
    max_workers: int,
    observer: TripleObserver,
    should_stop: Callable[[], bool],
    triple_workers: int = 1,
    include_held: bool = False,
) -> None:
    snaps = sorted(snapshots, key=lambda s: s.id)
    source_id = snaps[0].source_file_id if snaps else ""

    if len(snaps) >= 2:
        _ensure_boundary_cached(
            client, cache, cluster_name, source_id, snaps[0], snaps[1],
            max_workers=max_workers, should_stop=should_stop, observer=observer,
            include_held=include_held,
        )

    if len(snaps) < 3:
        observer.no_middle_snapshots()
        observer.finish()
        return

    triples = list(zip(snaps[:-2], snaps[1:-1], snaps[2:]))
    total = len(triples)

    cached_triples = cache.get_triples(cluster_name, source_id)
    partials = cache.get_triple_partials(cluster_name, source_id)
    errors: dict[int, str] = {}

    outcome: list[tuple[str, int, int] | None] = [None] * total
    uncached: list[int] = []
    for i, (prev, target, next_) in enumerate(triples):
        cached = cached_triples.get((prev.id, target.id, next_.id))
        if cached is not None:
            outcome[i] = ("cached", cached[0], cached[1])
            observer.triple_sized(cached[0])
        elif not include_held and (target.held or prev.held or next_.held):
            # target itself can't be deleted, OR one of its neighbors is held
            # -- the latter matters because a held snapshot is often an old
            # replication/lock anchor with a huge gap to its successor (e.g.
            # months), and that gap becomes one leg of this triple's diff
            # regardless of whether target itself is deletable. Skip by
            # default rather than inheriting that cost silently.
            outcome[i] = ("skipped_held", 0, 0)
        else:
            uncached.append(i)

    to_resume = [i for i in uncached if (triples[i][0].id, triples[i][1].id, triples[i][2].id) in partials]
    resume_set = set(to_resume)
    fresh = [i for i in uncached if i not in resume_set]
    pending_queue = to_resume + fresh

    sizing_ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def stop_all() -> bool:
        return should_stop()

    def _size_one(i: int, resume: tuple[int, int, int] | None) -> SnapshotExclusiveContribution:
        prev, target, next_ = triples[i]

        def _checkpoint(sized_index: int, exclusive: int, files: int) -> None:
            cache.put_triple_partial(
                cluster_name, source_id, prev.id, target.id, next_.id,
                sized_index, exclusive, files,
            )

        try:
            return compute_snapshot_exclusive_contribution(
                client, prev, target, next_,
                max_workers=max_workers, should_stop=stop_all,
                progress=_TripleProgress(observer, i + 1),
                resume=resume, checkpoint=_checkpoint, executor=sizing_ex,
            )
        except ApiError as e:
            if resume is not None and e.status_code == 400:
                cache.delete_triple_partial(cluster_name, source_id, prev.id, target.id, next_.id)
                return compute_snapshot_exclusive_contribution(
                    client, prev, target, next_,
                    max_workers=max_workers, should_stop=stop_all,
                    progress=_TripleProgress(observer, i + 1),
                    resume=None, checkpoint=_checkpoint, executor=sizing_ex,
                )
            raise

    def _job(i: int) -> None:
        if stop_all():
            return
        prev, target, next_ = triples[i]
        observer.start_triple(i + 1, total, prev, target, next_)
        try:
            key = (prev.id, target.id, next_.id)
            result = _size_one(i, partials.get(key))
            cache.put_triple(
                cluster_name, source_id, prev.id, target.id, next_.id,
                result.exclusive_bytes, result.total_files,
            )
            cache.delete_triple_partial(cluster_name, source_id, prev.id, target.id, next_.id)
            outcome[i] = ("computed", result.exclusive_bytes, result.total_files)
            observer.triple_sized(result.exclusive_bytes)
        except Interrupted:
            pass
        except ApiTimeout:
            outcome[i] = ("timed_out", 0, 0)
        except Exception as e:
            # Scoped to this triple -- must not cost the others their results.
            outcome[i] = ("error", 0, 0)
            errors[i] = str(e)
            print(f"[snapman] triple {i + 1} failed: {e!r}", file=sys.stderr)
        finally:
            observer.triple_finished(i + 1)

    producers = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, triple_workers))
    try:
        chunk_size = max(1, limit)
        for start in range(0, len(pending_queue), chunk_size):
            if stop_all():
                break
            chunk = pending_queue[start : start + chunk_size]
            concurrent.futures.wait([producers.submit(_job, i) for i in chunk])
    finally:
        producers.shutdown(wait=True, cancel_futures=True)
        sizing_ex.shutdown(wait=False, cancel_futures=True)

    for i in pending_queue:
        if outcome[i] is None:
            outcome[i] = ("pending", 0, 0)

    for i, ((prev, target, next_), out) in enumerate(zip(triples, outcome)):
        if out is None or out[0] == "pending":
            observer.triple_result(target, None, None, cached=False, pending=True)
            continue
        if out[0] == "timed_out":
            observer.triple_result(target, None, None, cached=False, pending=True, timed_out=True)
            continue
        if out[0] == "skipped_held":
            observer.triple_result(target, None, None, cached=False, pending=True, skipped_held=True)
            continue
        if out[0] == "error":
            observer.triple_result(target, None, None, cached=False, pending=False, error=errors.get(i))
            continue
        status, exclusive, files = out
        observer.triple_result(target, exclusive, files, cached=(status == "cached"), pending=False)

    observer.finish()
