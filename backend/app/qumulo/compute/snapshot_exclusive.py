"""Three-way ("delete this one snapshot alone") reclaim contribution.

Unlike the pairwise engine (compute/snapshot_reclaim.py), which answers
"delete everything from here back" and assumes anything older than the pair
is also being deleted, this answers "delete exactly this one snapshot,
keeping both its neighbors" for a middle snapshot S2 in a chain S1 < S2 < S3.

The per-file math (compute/reclaim.py::compute_freed_bytes) is proven correct
by birth-epoch monotonicity: a byte absent from both S1 and S3 cannot be held
by anything else in the chain either, so S2's exclusive contribution doesn't
depend on what's further out. This module's job is only to discover which
files need that per-file computation and to feed it the right two diffs.

S2 (target) is the pivot of both diffs used here, so every tree-diff entry
that matters is already expressed in target's own path namespace -- the join
between the two scans is a plain path match, no cross-snapshot rename graph
needed. Each leg still reuses snapshot_reclaim's single-hop rename/symlink
fallbacks unchanged.
"""

import concurrent.futures
import sys
import time

from collections.abc import Callable
from dataclasses import dataclass

from app.qumulo.api import DiffOp, FileDiffEntry, Snapshot, file_diff, snapshot_file_attrs, tree_diff_pages
from app.qumulo.client import ApiError, Client
from app.qumulo.compute.reclaim import compute_freed_bytes
from app.qumulo.compute.snapshot_reclaim import (
    _NULL_PROGRESS,
    Interrupted,
    SizingProgress,
    _ScanState,
    _entry_path,
    _is_unresolvable,
    _never_stop,
    _noop_checkpoint,
    _resolve,
    _resolves_in,
    _walk_files,
)

StopFn = Callable[[], bool]
CheckpointFn = Callable[[int, int, int], None]  # sized_index, exclusive_running, files_running

_SCAN_PAGE_SIZE = 200

_S1_S2_OPS = (DiffOp.CREATE, DiffOp.MODIFY)
_S2_S3_OPS = (DiffOp.MODIFY, DiffOp.DELETE)


@dataclass(frozen=True)
class SnapshotExclusiveContribution:
    target: Snapshot
    exclusive_bytes: int
    total_files: int


@dataclass(frozen=True)
class _Cand3:
    path: str
    op_b: DiffOp | None  # MODIFY -> real S2->S3 diff; DELETE/None -> synthetic delete
    known_size: int | None = None  # set only for files discovered via an ephemeral-dir walk


def _scan_leg(
    client: Client, newer_id: int, older_id: int, wanted_ops: tuple, dir_op: DiffOp, state: _ScanState
) -> tuple[dict[str, DiffOp], list[str]]:
    files: dict[str, DiffOp] = {}
    dirs: list[str] = []
    for page_entries, _cursor in tree_diff_pages(client, newer_id, older_id, limit=_SCAN_PAGE_SIZE):
        for e in page_entries:
            state.tick_entry()
            if e.is_directory:
                if e.op == dir_op:
                    dirs.append(e.path)
                continue
            if e.op in wanted_ops:
                files[e.path] = e.op
        # Both scans must fully complete before any sizing can start (see
        # module docstring), which for a large tree-diff can take a long time
        # with nothing else to report. Tick once per page so the caller's
        # throttled progress push (SizingProgress) still shows movement
        # during that window -- this ticks faster than true candidates are
        # found, so treat the live "found" count during a run as "entries
        # scanned so far", not a final candidate total.
        state.add_file()
    return files, dirs


def _discover_candidates(
    client: Client, prev: Snapshot, target: Snapshot, next_: Snapshot, state: _ScanState
) -> dict[str, _Cand3]:
    scan_a_files, created_dirs = _scan_leg(
        client, target.id, prev.id, _S1_S2_OPS, DiffOp.CREATE, state
    )
    scan_b_files, deleted_dirs = _scan_leg(
        client, next_.id, target.id, _S2_S3_OPS, DiffOp.DELETE, state
    )

    ephemeral = set(created_dirs) & set(deleted_dirs)
    remaining_created = [d for d in created_dirs if d not in ephemeral]
    remaining_deleted = [d for d in deleted_dirs if d not in ephemeral]

    def _under_any(path: str, dirs: list[str]) -> bool:
        return any(path.startswith(d) for d in dirs)

    candidates: dict[str, _Cand3] = {}

    for d in ephemeral:
        for f in _walk_files(client, target.id, d, state):
            p = _entry_path(f, target.id)
            candidates[p] = _Cand3(path=p, op_b=None, known_size=f.data_bytes)
            state.add_file()

    for path, op_a in scan_a_files.items():
        if path in candidates:
            continue
        if path in scan_b_files:
            candidates[path] = _Cand3(path=path, op_b=scan_b_files[path])
            state.add_file()
        elif _under_any(path, remaining_deleted):
            candidates[path] = _Cand3(path=path, op_b=DiffOp.DELETE)
            state.add_file()
        # else: untouched S2->S3 -> contributes 0, skip without an API call.

    for path, op_b in scan_b_files.items():
        if path in candidates:
            continue
        if _under_any(path, remaining_created):
            candidates[path] = _Cand3(path=path, op_b=op_b)
            state.add_file()
        # else: untouched S1->S2 -> contributes 0, skip without an API call.

    return candidates


def _is_non_diffable(e: ApiError) -> bool:
    return e.status_code == 400 and ("symlink" in e.error_class or "not_a_file" in e.error_class)


def _fetch_leg(client: Client, newer_id: int, older_id: int, path: str) -> list[FileDiffEntry]:
    try:
        return list(file_diff(client, newer_id, older_id, path=path))
    except ApiError as e:
        if e.status_code != 404 or e.is_snapshot_not_found():
            raise
        file_id = _resolve(client, older_id, newer_id, path).file_id
        return list(file_diff(client, newer_id, older_id, file_id=file_id))


def _fetch_s1_s2(client: Client, prev_id: int, target_id: int, path: str) -> list[FileDiffEntry]:
    try:
        return _fetch_leg(client, target_id, prev_id, path)
    except ApiError as e:
        if _is_non_diffable(e):
            size = snapshot_file_attrs(client, target_id, path=path).data_bytes
            return [FileDiffEntry(DiffOp.CREATE, 0, size)]
        raise


def _fetch_s2_s3(
    client: Client, target_id: int, next_id: int, path: str, op_b: DiffOp | None
) -> list[FileDiffEntry]:
    if op_b is DiffOp.MODIFY:
        try:
            return _fetch_leg(client, next_id, target_id, path)
        except ApiError as e:
            if _is_non_diffable(e):
                size = snapshot_file_attrs(client, target_id, path=path).data_bytes
                return [FileDiffEntry(DiffOp.DELETE, 0, size)]
            raise
    # DELETE (direct or ancestor-directory-inferred): confirm the file is
    # truly gone at next_id rather than renamed, mirroring the pairwise
    # engine's _deleted_freed.
    attrs = _resolve(client, target_id, next_id, path)
    if _resolves_in(client, next_id, attrs.file_id):
        return []
    return [FileDiffEntry(DiffOp.DELETE, 0, attrs.data_bytes)]


def _size_candidate(
    client: Client, prev_id: int, target_id: int, next_id: int, c: _Cand3
) -> tuple[int, int]:
    if c.known_size is not None:
        return c.known_size, 1 if c.known_size > 0 else 0
    try:
        diff_s1_s2 = _fetch_s1_s2(client, prev_id, target_id, c.path)
        diff_s2_s3 = _fetch_s2_s3(client, target_id, next_id, c.path, c.op_b)
    except ApiError as e:
        if _is_unresolvable(e):
            print(
                f"[snapman] {c.path!r} unresolvable in snapshots {prev_id}/{target_id}/{next_id} "
                f"({e}) -- excluded from this snapshot's total",
                file=sys.stderr,
            )
            return 0, 0
        raise
    freed = compute_freed_bytes(diff_s1_s2, diff_s2_s3)
    return freed.s2, 1 if freed.s2 > 0 else 0


def compute_snapshot_exclusive_contribution(
    client: Client,
    prev: Snapshot,
    target: Snapshot,
    next: Snapshot,
    *,
    max_workers: int = 16,
    should_stop: StopFn = _never_stop,
    progress: SizingProgress = _NULL_PROGRESS,
    resume: tuple[int, int, int] | None = None,
    checkpoint: CheckpointFn = _noop_checkpoint,
    clock: Callable[[], float] = time.monotonic,
    checkpoint_interval: float = 10.0,
    executor: concurrent.futures.Executor | None = None,
) -> SnapshotExclusiveContribution:
    state = _ScanState(progress, should_stop)

    # Discovery is always run in full on every call (including resumes) -- it's
    # cheap relative to sizing, and re-running it deterministically (sorted by
    # path) is what makes the sized_index checkpoint valid without persisting
    # the candidate list itself.
    candidates = sorted(
        _discover_candidates(client, prev, target, next, state).values(),
        key=lambda c: c.path,
    )
    progress.enumeration_done()

    start_index = resume[0] if resume is not None else 0
    exclusive_running = resume[1] if resume is not None else 0
    files_running = resume[2] if resume is not None else 0
    pending = candidates[start_index:]

    if not pending:
        return SnapshotExclusiveContribution(target, exclusive_running, files_running)

    owned = executor is None
    ex = executor if executor is not None else concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures: dict[concurrent.futures.Future, int] = {}
    try:
        futures = {
            ex.submit(_size_candidate, client, prev.id, target.id, next.id, c): i
            for i, c in enumerate(pending, start=start_index)
        }
        results: dict[int, tuple[int, int]] = {}
        next_index = start_index
        last_checkpoint = clock()
        for fut in concurrent.futures.as_completed(futures):
            if should_stop():
                raise Interrupted()
            idx = futures[fut]
            results[idx] = fut.result()
            progress.candidate_sized()
            while next_index in results:
                b, f = results.pop(next_index)
                exclusive_running += b
                files_running += f
                next_index += 1
                if clock() - last_checkpoint >= checkpoint_interval:
                    checkpoint(next_index, exclusive_running, files_running)
                    last_checkpoint = clock()
    finally:
        if owned:
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            for f in futures:
                f.cancel()

    return SnapshotExclusiveContribution(target, exclusive_running, files_running)
