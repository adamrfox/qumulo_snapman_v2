"""Streaming, interruptible reclaim curve engine.

Port of qsnap's compute/inspect.py with an added WebObserver that translates
the observer protocol to SSE events via a push callback.
"""

import concurrent.futures
import sys
import time

from collections.abc import Callable
from typing import Protocol

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError, ApiTimeout, Client
from app.qumulo.compute.snapshot_reclaim import (
    Interrupted,
    PairContribution,
    compute_pair_contribution,
)

PushFn = Callable[[str, dict], None]
ComputeFn = Callable[..., PairContribution]


class InspectObserver(Protocol):
    def set_overlapped(self, overlapped: bool) -> None: ...
    def start_pair(self, index: int, total: int, older: Snapshot, newer: Snapshot) -> None: ...
    def pair_finished(self, index: int) -> None: ...
    def progress(self, index: int, found: int, sized: int) -> None: ...
    def pair_sized(self, freed_bytes: int) -> None: ...
    def pair_result(
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
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None: ...
    def no_curve(self) -> None: ...
    def finish(self) -> None: ...


class WebObserver:
    """Translates InspectObserver calls into push(type, data) SSE events.

    Stateless by design: with pair_workers > 1, several pairs' _PairProgress
    instances call in concurrently, so found/sized/index must live on the
    per-pair caller (see _PairProgress below), never here.
    """

    def __init__(self, push: PushFn) -> None:
        self._push = push

    def set_overlapped(self, overlapped: bool) -> None:
        self._push("overlapped", {"overlapped": overlapped})

    def start_pair(self, index: int, total: int, older: Snapshot, newer: Snapshot) -> None:
        self._push(
            "pair_start",
            {
                "index": index,
                "total": total,
                "older_id": older.id,
                "older_name": older.name,
                "older_date": older.timestamp[:10],
                "newer_id": newer.id,
                "newer_name": newer.name,
                "newer_date": newer.timestamp[:10],
            },
        )

    def pair_finished(self, index: int) -> None:
        self._push("pair_finished", {"index": index})

    def progress(self, index: int, found: int, sized: int) -> None:
        self._push("progress", {"index": index, "found": found, "sized": sized})

    def pair_sized(self, freed_bytes: int) -> None:
        self._push("discovered", {"freed_bytes": freed_bytes})

    def pair_result(
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
        error: str | None = None,
        skipped_held: bool = False,
    ) -> None:
        self._push(
            "pair_result",
            {
                "older_id": older.id,
                "older_date": older.timestamp[:10],
                "older_name": older.name,
                "newer_id": newer.id,
                "newer_date": newer.timestamp[:10],
                "freed_bytes": freed_bytes,
                "cumulative_bytes": cumulative,
                "total_files": total_files,
                "cached": cached,
                "pending": pending,
                "timed_out": timed_out,
                "error": error,
                "skipped_held": skipped_held,
            },
        )

    def no_curve(self) -> None:
        self._push("no_curve", {})

    def finish(self) -> None:
        self._push("finish", {})


_PROGRESS_PUSH_INTERVAL = 2.0  # seconds; caps how often "progress" fires during a big pair


class _PairProgress:
    """Per-pair progress state. A fresh instance is created for every pair, so
    concurrent pairs (pair_workers > 1) never share found/sized/throttle
    state -- each pushes its own index correctly regardless of what else is
    running at the same time."""

    def __init__(self, observer: InspectObserver, index: int, *, clock: Callable[[], float] = time.monotonic) -> None:
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


def run_inspect(
    client: Client,
    cache: Cache,
    cluster_name: str,
    snapshots: list[Snapshot],
    *,
    limit: int,
    max_workers: int,
    observer: InspectObserver,
    should_stop: Callable[[], bool],
    overlapped: bool = False,
    cached_only: bool = False,
    pair_workers: int = 1,
    compute: ComputeFn = compute_pair_contribution,
    include_held: bool = False,
) -> None:
    observer.set_overlapped(overlapped)
    snaps = sorted(snapshots, key=lambda s: s.id)
    if len(snaps) < 2:
        observer.no_curve()
        observer.finish()
        return

    source_id = snaps[0].source_file_id
    pairs = list(zip(snaps[:-1], snaps[1:]))
    total = len(pairs)

    cached_pairs = cache.get_pairs(cluster_name, source_id)
    partials = cache.get_partials(cluster_name, source_id)
    errors: dict[int, str] = {}

    outcome: list[tuple[str, int, int] | None] = [None] * total
    uncached: list[int] = []
    for i, (older, newer) in enumerate(pairs):
        cached = cached_pairs.get((older.id, newer.id))
        if cached is not None:
            outcome[i] = ("cached", cached[0], cached[1])
            observer.pair_sized(cached[0])
        elif not include_held and older.held:
            # older can't actually be deleted (locked, or replication-owned)
            # so its exact reclaim size isn't an actionable number -- skip by
            # default rather than burning time on a potentially huge diff.
            outcome[i] = ("skipped_held", 0, 0)
        else:
            uncached.append(i)

    if cached_only:
        for i in uncached:
            outcome[i] = ("pending", 0, 0)
        pending_queue: list[int] = []
    else:
        to_resume = [i for i in uncached if (pairs[i][0].id, pairs[i][1].id) in partials]
        resume_set = set(to_resume)
        fresh = [i for i in uncached if i not in resume_set]
        pending_queue = to_resume + fresh

    sizing_ex = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def stop_all() -> bool:
        return should_stop()

    def _size_one(i: int, resume: tuple[str, int, int] | None) -> PairContribution:
        older, newer = pairs[i]

        def _checkpoint(cursor: str, freed: int, files: int) -> None:
            cache.put_partial(cluster_name, source_id, older.id, newer.id, cursor, freed, files)

        try:
            return compute(
                client,
                older,
                newer,
                max_workers=max_workers,
                should_stop=stop_all,
                progress=_PairProgress(observer, i + 1),
                resume=resume,
                checkpoint=_checkpoint,
                executor=sizing_ex,
            )
        except ApiError as e:
            if resume is not None and e.status_code == 400:
                cache.delete_partial(cluster_name, source_id, older.id, newer.id)
                return compute(
                    client,
                    older,
                    newer,
                    max_workers=max_workers,
                    should_stop=stop_all,
                    progress=_PairProgress(observer, i + 1),
                    resume=None,
                    checkpoint=_checkpoint,
                    executor=sizing_ex,
                )
            raise

    def _job(i: int) -> None:
        if stop_all():
            return
        older, newer = pairs[i]
        observer.start_pair(i + 1, total, older, newer)
        try:
            result = _size_one(i, partials.get((older.id, newer.id)))
            cache.put_pair(
                cluster_name, source_id, older.id, newer.id,
                result.freed_bytes, result.total_files,
            )
            cache.delete_partial(cluster_name, source_id, older.id, newer.id)
            outcome[i] = ("computed", result.freed_bytes, result.total_files)
            observer.pair_sized(result.freed_bytes)
        except Interrupted:
            pass
        except ApiTimeout:
            outcome[i] = ("timed_out", 0, 0)
        except Exception as e:
            # A single pair's failure (this cluster call failed, this
            # snapshot expired mid-diff, etc.) is scoped to this pair -- it
            # must not cost the other pairs their already-completed results.
            outcome[i] = ("error", 0, 0)
            errors[i] = str(e)
            print(f"[snapman] pair {i + 1} failed: {e!r}", file=sys.stderr)
        finally:
            observer.pair_finished(i + 1)

    producers = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, pair_workers))
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

    _emit_curve(observer, pairs, outcome, errors)
    observer.finish()


def _emit_curve(
    observer: InspectObserver,
    pairs: list[tuple[Snapshot, Snapshot]],
    outcome: list[tuple[str, int, int] | None],
    errors: dict[int, str],
) -> None:
    cumulative = 0
    known = True
    for i, ((older, newer), out) in enumerate(zip(pairs, outcome)):
        if out is None or out[0] == "pending":
            known = False
            observer.pair_result(older, newer, None, None, None, cached=False, pending=True)
            continue
        if out[0] == "timed_out":
            known = False
            observer.pair_result(
                older, newer, None, None, None, cached=False, pending=True, timed_out=True
            )
            continue
        if out[0] == "skipped_held":
            known = False
            observer.pair_result(
                older, newer, None, None, None, cached=False, pending=True, skipped_held=True
            )
            continue
        if out[0] == "error":
            known = False
            observer.pair_result(
                older, newer, None, None, None, cached=False, pending=False, error=errors.get(i)
            )
            continue
        status, freed, files = out
        if known:
            cumulative += freed
        observer.pair_result(
            older, newer, freed, cumulative if known else None, files,
            cached=(status == "cached"), pending=False,
        )
