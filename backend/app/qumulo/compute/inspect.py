"""Streaming, interruptible reclaim curve engine.

Port of qsnap's compute/inspect.py with an added WebObserver that translates
the observer protocol to SSE events via a push callback.
"""

import concurrent.futures

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
    def candidate_found(self) -> None: ...
    def candidate_sized(self) -> None: ...
    def enumeration_done(self, index: int) -> None: ...
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
    ) -> None: ...
    def no_curve(self) -> None: ...
    def finish(self) -> None: ...


class WebObserver:
    """Translates InspectObserver calls into push(type, data) SSE events."""

    def __init__(self, push: PushFn) -> None:
        self._push = push
        self._found = 0
        self._sized = 0

    def set_overlapped(self, overlapped: bool) -> None:
        self._push("overlapped", {"overlapped": overlapped})

    def start_pair(self, index: int, total: int, older: Snapshot, newer: Snapshot) -> None:
        self._push(
            "pair_start",
            {
                "index": index,
                "total": total,
                "older_id": older.id,
                "older_date": older.timestamp[:10],
                "newer_id": newer.id,
                "newer_date": newer.timestamp[:10],
            },
        )

    def pair_finished(self, index: int) -> None:
        pass

    def candidate_found(self) -> None:
        self._found += 1

    def candidate_sized(self) -> None:
        self._sized += 1

    def enumeration_done(self, index: int) -> None:
        self._push("progress", {"index": index, "found": self._found, "sized": self._sized})

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
            },
        )

    def no_curve(self) -> None:
        self._push("no_curve", {})

    def finish(self) -> None:
        self._push("finish", {})


class _PairProgress:
    def __init__(self, observer: InspectObserver, index: int) -> None:
        self._observer = observer
        self._index = index

    def candidate_found(self) -> None:
        self._observer.candidate_found()

    def candidate_sized(self) -> None:
        self._observer.candidate_sized()

    def enumeration_done(self) -> None:
        self._observer.enumeration_done(self._index)


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

    outcome: list[tuple[str, int, int] | None] = [None] * total
    uncached: list[int] = []
    for i, (older, newer) in enumerate(pairs):
        cached = cached_pairs.get((older.id, newer.id))
        if cached is not None:
            outcome[i] = ("cached", cached[0], cached[1])
            observer.pair_sized(cached[0])
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
    abort: list[BaseException] = []

    def stop_all() -> bool:
        return should_stop() or bool(abort)

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
            abort.append(e)
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
    if abort:
        raise abort[0]

    for i in pending_queue:
        if outcome[i] is None:
            outcome[i] = ("pending", 0, 0)

    _emit_curve(observer, pairs, outcome)
    observer.finish()


def _emit_curve(
    observer: InspectObserver,
    pairs: list[tuple[Snapshot, Snapshot]],
    outcome: list[tuple[str, int, int] | None],
) -> None:
    cumulative = 0
    known = True
    for (older, newer), out in zip(pairs, outcome):
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
        status, freed, files = out
        if known:
            cumulative += freed
        observer.pair_result(
            older, newer, freed, cumulative if known else None, files,
            cached=(status == "cached"), pending=False,
        )
