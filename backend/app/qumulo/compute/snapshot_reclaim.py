"""Per-pair reclaim contribution. Port of qsnap's compute/snapshot_reclaim.py.

Only import path changes — the algorithm is identical.
"""

import concurrent.futures
import sys
import time

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from app.qumulo.compute import intervals, reclaim
from app.qumulo.api import (
    DiffOp,
    FileAttrs,
    Snapshot,
    TreeDiffEntry,
    file_diff,
    read_dir_in_snapshot,
    snapshot_file_attrs,
    tree_diff_pages,
)
from app.qumulo.client import ApiError, Client
from app.qumulo.paths import normalize_path

StopFn = Callable[[], bool]
CheckpointFn = Callable[[str, int, int], None]


class SizingProgress(Protocol):
    def candidate_found(self) -> None: ...
    def candidate_sized(self) -> None: ...
    def enumeration_done(self) -> None: ...


class _NullProgress:
    def candidate_found(self) -> None: ...
    def candidate_sized(self) -> None: ...
    def enumeration_done(self) -> None: ...


_NULL_PROGRESS = _NullProgress()

_SCAN_PAGE_SIZE = 200


def _never_stop() -> bool:
    return False


def _noop_checkpoint(cursor: str, freed: int, files: int) -> None:
    pass


class Interrupted(Exception):
    pass


class _ScanState:
    def __init__(self, progress: SizingProgress, should_stop: StopFn) -> None:
        self._progress = progress
        self._should_stop = should_stop

    def tick_entry(self) -> None:
        if self._should_stop():
            raise Interrupted()

    def add_file(self) -> None:
        self._progress.candidate_found()


class _CandidateKind(Enum):
    MODIFIED = auto()
    DELETED_FILE = auto()
    KNOWN_SIZE = auto()


@dataclass(frozen=True)
class _Candidate:
    kind: _CandidateKind
    older_id: int
    newer_id: int
    path: str
    size: int


@dataclass(frozen=True)
class PairContribution:
    older: Snapshot
    newer: Snapshot
    freed_bytes: int
    total_files: int


def compute_pair_contribution(
    client: Client,
    older: Snapshot,
    newer: Snapshot,
    *,
    max_workers: int = 16,
    should_stop: StopFn = _never_stop,
    progress: SizingProgress = _NULL_PROGRESS,
    resume: tuple[str, int, int] | None = None,
    checkpoint: CheckpointFn = _noop_checkpoint,
    clock: Callable[[], float] = time.monotonic,
    checkpoint_interval: float = 10.0,
    executor: concurrent.futures.Executor | None = None,
) -> PairContribution:
    start_cursor = resume[0] if resume is not None else None
    freed_running = resume[1] if resume is not None else 0
    files_running = resume[2] if resume is not None else 0

    state = _ScanState(progress, should_stop)
    owned = executor is None
    ex = (
        executor
        if executor is not None
        else concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    )
    futures: list[concurrent.futures.Future[int]] = []
    folded: set[concurrent.futures.Future[int]] = set()

    def _fold(future: concurrent.futures.Future[int]) -> None:
        nonlocal freed_running, files_running
        b = future.result()
        freed_running += b
        files_running += 1 if b > 0 else 0
        folded.add(future)

    try:
        last_checkpoint = clock()
        for page_entries, cursor in tree_diff_pages(
            client, newer.id, older.id, limit=_SCAN_PAGE_SIZE, start_cursor=start_cursor
        ):
            for cand in _candidates_in_page(client, older, newer, page_entries, state):
                fut = ex.submit(_size_candidate, client, cand)
                fut.add_done_callback(lambda f: progress.candidate_sized())
                futures.append(fut)
            if cursor is not None and clock() - last_checkpoint >= checkpoint_interval:
                for f in futures:
                    if f not in folded:
                        _fold(f)
                checkpoint(cursor, freed_running, files_running)
                last_checkpoint = clock()
        progress.enumeration_done()

        for future in concurrent.futures.as_completed(futures):
            if should_stop():
                raise Interrupted()
            if future not in folded:
                _fold(future)
    finally:
        if owned:
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            for f in futures:
                f.cancel()

    return PairContribution(older, newer, freed_running, files_running)


def _is_unresolvable(e: ApiError) -> bool:
    """A 404 that isn't 'the snapshot itself is gone' (that's a systemic
    failure worth aborting for) -- e.g. fs_no_such_file_version_error, where a
    file's identity no longer resolves in a particular snapshot for reasons
    unrelated to the diff we're trying to compute. Once every known fallback
    (path lookup, file_id lookup, the other snapshot) has already failed, this
    is the last-resort classification: treat the file as unmeasurable rather
    than aborting the whole run over one uncooperative path."""
    return e.status_code == 404 and not e.is_snapshot_not_found()


def _size_candidate(client: Client, c: _Candidate) -> int:
    if c.kind is _CandidateKind.KNOWN_SIZE:
        return c.size
    try:
        if c.kind is _CandidateKind.DELETED_FILE:
            return _deleted_freed(client, c)
        return _modified_freed(client, c)
    except ApiError as e:
        if _is_unresolvable(e):
            print(
                f"[snapman] {c.path!r} unresolvable in snapshots {c.older_id}/{c.newer_id} "
                f"({e}) -- excluded from this pair's total",
                file=sys.stderr,
            )
            return 0
        raise


def _resolves_in(client: Client, snapshot_id: int, file_id: str) -> bool:
    try:
        snapshot_file_attrs(client, snapshot_id, file_id=file_id)
        return True
    except ApiError as e:
        if e.status_code != 404 or e.is_snapshot_not_found():
            raise
        return False


def _deleted_freed(client: Client, c: _Candidate) -> int:
    attrs = _resolve(client, c.older_id, c.newer_id, c.path)
    return 0 if _resolves_in(client, c.newer_id, attrs.file_id) else attrs.data_bytes


def _modified_freed(client: Client, c: _Candidate) -> int:
    try:
        return _diff_freed(client, c.newer_id, c.older_id, path=c.path)
    except ApiError as e:
        if e.status_code != 404 or e.is_snapshot_not_found():
            raise
    file_id = _resolve(client, c.older_id, c.newer_id, c.path).file_id
    return _diff_freed(client, c.newer_id, c.older_id, file_id=file_id)


def _diff_freed(
    client: Client,
    newer_id: int,
    older_id: int,
    *,
    path: str | None = None,
    file_id: str | None = None,
) -> int:
    try:
        return intervals.total_size(
            reclaim.target_not_in_newer(
                file_diff(client, newer_id, older_id, path=path, file_id=file_id)
            )
        )
    except ApiError as e:
        if e.status_code == 400 and ("symlink" in e.error_class or "not_a_file" in e.error_class):
            return snapshot_file_attrs(client, older_id, path=path, file_id=file_id).size
        raise


def _resolve(client: Client, primary_id: int, other_id: int, path: str) -> FileAttrs:
    try:
        return snapshot_file_attrs(client, primary_id, path=path)
    except ApiError as e:
        if e.status_code != 404 or e.is_snapshot_not_found():
            raise
        return snapshot_file_attrs(client, other_id, path=path)


def _candidates_in_page(
    client: Client,
    older: Snapshot,
    newer: Snapshot,
    page_entries: list[TreeDiffEntry],
    state: _ScanState,
) -> Iterator[_Candidate]:
    for e in page_entries:
        state.tick_entry()
        if e.is_directory:
            if e.op == DiffOp.DELETE and not _dir_moved(client, older.id, newer.id, e.path):
                for f in _walk_files(client, older.id, e.path, state):
                    state.add_file()
                    yield _Candidate(
                        _CandidateKind.KNOWN_SIZE,
                        older.id,
                        newer.id,
                        _entry_path(f, older.id),
                        f.data_bytes,
                    )
        elif e.op == DiffOp.DELETE:
            state.add_file()
            yield _Candidate(_CandidateKind.DELETED_FILE, older.id, newer.id, e.path, 0)
        elif e.op == DiffOp.MODIFY:
            state.add_file()
            yield _Candidate(_CandidateKind.MODIFIED, older.id, newer.id, e.path, 0)


def _dir_moved(client: Client, older_id: int, newer_id: int, path: str) -> bool:
    try:
        dir_id = snapshot_file_attrs(client, older_id, path=normalize_path(path)).file_id
        return _resolves_in(client, newer_id, dir_id)
    except ApiError as e:
        if _is_unresolvable(e):
            # Can't tell whether this directory was truly deleted or just
            # moved -- treat it as moved (i.e. don't recurse into it as a
            # deletion candidate) so an unresolvable path never inflates the
            # reclaim total, only ever potentially undercounts it.
            print(
                f"[snapman] {path!r} unresolvable in snapshots {older_id}/{newer_id} "
                f"({e}) -- not counted as deleted",
                file=sys.stderr,
            )
            return True
        raise


def _entry_path(f: FileAttrs, snapshot_id: int) -> str:
    if f.path is None:
        raise ValueError(f"cluster response for file {f.file_id} in snapshot {snapshot_id} has no path")
    return f.path


def _walk_files(
    client: Client, snapshot_id: int, dir_path: str, state: _ScanState
) -> Iterator[FileAttrs]:
    for child in read_dir_in_snapshot(
        client, snapshot_id, path=normalize_path(dir_path), page_size=_SCAN_PAGE_SIZE
    ):
        state.tick_entry()
        if child.is_directory:
            yield from _walk_files(client, snapshot_id, _entry_path(child, snapshot_id), state)
        elif child.is_file:
            yield child
