"""N-snapshot generalization of the three-way "delete this one snapshot
alone" engine (compute/snapshot_exclusive.py) to "delete this whole
contiguous run of two-or-more snapshots together, keeping the kept boundary
on each side." Replaces compute/deletion_estimate.py's sum-and-subtract
fallback for exactly the run shapes that formula gets wrong -- see that
module's docstring CAVEAT for the bug this exists to fix, and
compute/intervals.py's run_exclusive_size docstring for why a real interval
sweep (not more total-summing) is required once there's more than one
deleted snapshot in between.

Chain = [L, X1, .., Xk, R], k >= 2 deleted snapshots (k == 1 stays on the
existing three-way engine -- see deletion_estimate.py's routing). There are
k+1 adjacent hops between chain positions. A file is a candidate if it's
touched (any op) at any hop, discovered via one tree-diff scan per hop
(_scan_hop) -- generalizing snapshot_exclusive.py's two-scan discovery.
Each candidate's per-hop tree-diff op decides how its byte-range events are
fetched (mirroring the three-way engine's own op-based branching: a MODIFY
gets a real byte-range file_diff, a CREATE/DELETE gets one whole-file
synthetic event, since there's no earlier/later version to diff against).

Directory handling (deliberately scoped, matching this codebase's existing
"don't chase every possible edge case" pattern elsewhere -- see e.g.
snapshot_reclaim.py's single-hop rename fallback):
  - A directory created AND deleted within the run (ephemeral) has its
    contents walked once, right after creation, synthesizing a CREATE event
    for each file at its creation hop (and a DELETE at its deletion hop,
    unless that file also happens to be individually re-enumerated there) --
    directly generalizing snapshot_exclusive.py's ephemeral-directory case.
    Only the earliest create/delete pair is used; a directory recreated or
    deleted more than once within one run isn't specially handled.
  - A directory deleted within the run (whether or not it was also created
    within the run) gets a synthetic DELETE injected, at its earliest delete
    hop, for any ALREADY-discovered candidate file under it that isn't
    individually re-enumerated there -- generalizing the pairwise/three-way
    engines' "remaining_deleted" case. This deliberately does NOT walk a
    directory that predates the run just because it gets deleted: any file
    under it that was never individually touched at any hop was unchanged
    for the run's entire span, so it matches L and is correctly not
    exclusive -- skipping it only skips computing a guaranteed zero.
  - A directory created (but not deleted) within the run does *not* get a
    symmetric synthetic-create pass. A file born there that's never touched
    again has no death event either, so it never gets counted regardless of
    what its tracked birth looks like (see intervals.run_exclusive_size);
    the only way this omission could matter is a file born via that bulk
    create, left otherwise untouched, whose parent directory is *later*
    bulk-deleted without individual re-enumeration -- already covered by
    the delete-side handling above, since that requires no exact birth
    range for the safe, oversized synthetic-delete injection it uses
    (_SENTINEL_SIZE). Net effect: any gap here can only undercount, never
    overcount -- the safe direction, unlike the bug this module fixes.

No caching: unlike the pairwise/three-way engines, there's no fixed-arity
cache table for an arbitrary-length run, and this is a heavier, far less
frequently exercised path (a user explicitly multi-selecting snapshots to
delete together) than the sweep/goal-solver-driven pairwise and triple
engines -- not worth a new migration for.
"""

import concurrent.futures
import sys

from collections.abc import Callable
from dataclasses import dataclass

from app.qumulo.api import DiffOp, FileDiffEntry, Snapshot, file_diff, snapshot_file_attrs, tree_diff_pages
from app.qumulo.client import ApiError, Client
from app.qumulo.compute import intervals
from app.qumulo.compute.snapshot_reclaim import (
    _NULL_PROGRESS,
    Interrupted,
    SizingProgress,
    _ScanState,
    _entry_path,
    _is_unresolvable,
    _never_stop,
    _resolve,
    _resolves_in,
    _walk_files,
)

StopFn = Callable[[], bool]

_SCAN_PAGE_SIZE = 200

# A safe (never-too-small) extent for a synthetic delete injected onto an
# already-discovered candidate whose real birth range came from elsewhere --
# see the module docstring's "remaining_deleted" case. Oversizing it can't
# overcount (see intervals.run_exclusive_size: an event only ever *ends* an
# instance that's genuinely open within the run's eligible range; touching
# extra, otherwise-untouched offsets just closes out default/non-exclusive
# state there, contributing nothing).
_SENTINEL_SIZE = 1 << 62


@dataclass(frozen=True)
class RunExclusiveContribution:
    chain: list[Snapshot]  # [L, X1, .., Xk, R]
    exclusive_bytes: int
    total_files: int


@dataclass(frozen=True)
class _Cand:
    path: str
    touched_hops: tuple[tuple[int, DiffOp], ...]  # (hop_index, tree-diff op), individually enumerated
    # hop_index -> (offset, size, starts_new_instance), for a directory
    # episode's synthetic events (see module docstring).
    synthetic: dict[int, tuple[int, int, bool]] | None = None


def _scan_hop(
    client: Client, newer_id: int, older_id: int, state: _ScanState
) -> tuple[dict[str, DiffOp], list[str], list[str]]:
    """One hop's full tree-diff: every touched file (any op), plus which
    directories were created/deleted at this hop."""
    files: dict[str, DiffOp] = {}
    created_dirs: list[str] = []
    deleted_dirs: list[str] = []
    for page_entries, _cursor in tree_diff_pages(client, newer_id, older_id, limit=_SCAN_PAGE_SIZE):
        for e in page_entries:
            state.tick_entry()
            if e.is_directory:
                if e.op == DiffOp.CREATE:
                    created_dirs.append(e.path)
                elif e.op == DiffOp.DELETE:
                    deleted_dirs.append(e.path)
                continue
            files[e.path] = e.op
        # Ticks once per page rather than per file -- see snapshot_exclusive.py's
        # _scan_leg, which this mirrors, for why.
        state.add_file()
    return files, created_dirs, deleted_dirs


def _discover_candidates(
    client: Client, chain: list[Snapshot], state: _ScanState, *, max_workers: int = 16
) -> dict[str, _Cand]:
    k = len(chain) - 2  # number of deleted snapshots

    # Each hop's tree-diff scan is independent of every other hop's -- fire
    # them off concurrently instead of waiting on one before starting the
    # next. This was the single biggest avoidable cost for a long run (many
    # deleted snapshots): with k+1 hops to scan serially, wall-clock time
    # scaled with the run's length even though nothing about the scans
    # actually depends on each other.
    scan_ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, k + 1))
    try:
        futures = [
            scan_ex.submit(_scan_hop, client, chain[i + 1].id, chain[i].id, state)
            for i in range(k + 1)
        ]
        hop_results = [fut.result() for fut in futures]
    finally:
        scan_ex.shutdown(wait=False, cancel_futures=True)

    hop_files: list[dict[str, DiffOp]] = [r[0] for r in hop_results]
    hop_created_dirs: list[list[str]] = [r[1] for r in hop_results]
    hop_deleted_dirs: list[list[str]] = [r[2] for r in hop_results]

    candidates: dict[str, _Cand] = {}
    for j in range(k + 1):
        for path, op in hop_files[j].items():
            if path not in candidates:
                touched = tuple((jj, hop_files[jj][path]) for jj in range(k + 1) if path in hop_files[jj])
                candidates[path] = _Cand(path=path, touched_hops=touched)

    all_dirs = {d for dirs in hop_created_dirs for d in dirs} | {d for dirs in hop_deleted_dirs for d in dirs}
    for d in sorted(all_dirs):
        create_hops = [j for j in range(k + 1) if d in hop_created_dirs[j]]
        delete_hops = [j for j in range(k + 1) if d in hop_deleted_dirs[j]]

        if create_hops:
            c = min(create_hops)
            later_deletes = [j for j in delete_hops if j > c]
            if later_deletes:
                # Ephemeral: created and deleted within the run. Walk its
                # contents once, right after creation.
                dd = min(later_deletes)
                for f in _walk_files(client, chain[c + 1].id, d, state):
                    p = _entry_path(f, chain[c + 1].id)
                    state.add_file()
                    synth: dict[int, tuple[int, int, bool]] = {c: (0, f.data_bytes, True)}
                    if p not in hop_files[dd]:
                        synth[dd] = (0, f.data_bytes, False)
                    existing = candidates.get(p)
                    if existing is not None:
                        candidates[p] = _Cand(
                            path=p, touched_hops=existing.touched_hops,
                            synthetic={**(existing.synthetic or {}), **synth},
                        )
                    else:
                        candidates[p] = _Cand(path=p, touched_hops=(), synthetic=synth)
                continue  # this directory's episode is fully accounted for

        if delete_hops:
            dd = min(delete_hops)
            for path, cand in list(candidates.items()):
                if not path.startswith(d):
                    continue
                if any(hop_index == dd for hop_index, _ in cand.touched_hops):
                    continue
                if cand.synthetic and dd in cand.synthetic:
                    continue
                candidates[path] = _Cand(
                    path=path, touched_hops=cand.touched_hops,
                    synthetic={**(cand.synthetic or {}), dd: (0, _SENTINEL_SIZE, False)},
                )

    return candidates


def _is_non_diffable(e: ApiError) -> bool:
    return e.status_code == 400 and ("symlink" in e.error_class or "not_a_file" in e.error_class)


def _fetch_leg(client: Client, newer_id: int, older_id: int, path: str) -> list[FileDiffEntry]:
    """Identical to snapshot_exclusive.py's own _fetch_leg: try by path,
    fall back to a file-id lookup on an unresolvable-by-path 404."""
    try:
        return list(file_diff(client, newer_id, older_id, path=path))
    except ApiError as e:
        if e.status_code != 404 or e.is_snapshot_not_found():
            raise
        file_id = _resolve(client, older_id, newer_id, path).file_id
        return list(file_diff(client, newer_id, older_id, file_id=file_id))


def _fetch_hop_events(
    client: Client, newer_id: int, older_id: int, path: str, op: DiffOp
) -> list[FileDiffEntry]:
    """Mirrors snapshot_exclusive.py's per-leg fetches exactly: DELETE needs
    a rename-check (a path-based diff can't tell "genuinely gone" from
    "moved elsewhere", so a false DELETE would wrongly credit content that
    actually survives) before crediting it as freed -- same as
    _fetch_s2_s3's non-MODIFY branch. CREATE and MODIFY both just call
    _fetch_leg and let it report which -- it already handles "didn't exist
    in older" (CREATE) the same way it reports "existed in both, changed"
    (MODIFY); _fetch_s1_s2 never branches on op either, and its own tests
    (test_snapshot_exclusive.py's AncestorJoinTest) register a CREATE
    file_diff for exactly this case, not a raw attrs lookup."""
    if op is DiffOp.DELETE:
        attrs = _resolve(client, older_id, newer_id, path)
        if _resolves_in(client, newer_id, attrs.file_id):
            return []  # actually a rename, not a real delete
        return [FileDiffEntry(DiffOp.DELETE, 0, attrs.data_bytes)]
    try:
        return _fetch_leg(client, newer_id, older_id, path)
    except ApiError as e:
        if _is_non_diffable(e):
            size = snapshot_file_attrs(client, older_id if op is DiffOp.MODIFY else newer_id, path=path).data_bytes
            return [FileDiffEntry(op, 0, size)]
        raise


def _size_candidate(client: Client, chain: list[Snapshot], cand: _Cand) -> tuple[int, int]:
    hops: list[list[intervals.HopEvent]] = [[] for _ in range(len(chain) - 1)]
    try:
        for hop_index, op in cand.touched_hops:
            entries = _fetch_hop_events(client, chain[hop_index + 1].id, chain[hop_index].id, cand.path, op)
            hops[hop_index].extend((e.offset, e.size, e.op in (DiffOp.CREATE, DiffOp.MODIFY)) for e in entries)
    except ApiError as e:
        if _is_unresolvable(e):
            print(f"[snapman] {cand.path!r} unresolvable across run -- excluded from this run's total", file=sys.stderr)
            return 0, 0
        raise

    if cand.synthetic:
        for hop_index, event in cand.synthetic.items():
            hops[hop_index].append(event)

    exclusive = intervals.run_exclusive_size(hops)
    return exclusive, 1 if exclusive > 0 else 0


def compute_run_exclusive_contribution(
    client: Client,
    chain: list[Snapshot],
    *,
    max_workers: int = 16,
    should_stop: StopFn = _never_stop,
    progress: SizingProgress = _NULL_PROGRESS,
    executor: concurrent.futures.Executor | None = None,
) -> RunExclusiveContribution:
    if len(chain) < 3:
        raise ValueError(
            "compute_run_exclusive_contribution needs a left boundary, at least one "
            "deleted snapshot, and a right boundary"
        )

    state = _ScanState(progress, should_stop)
    candidates = sorted(
        _discover_candidates(client, chain, state, max_workers=max_workers).values(), key=lambda c: c.path
    )
    progress.enumeration_done()

    if not candidates:
        return RunExclusiveContribution(chain, 0, 0)

    owned = executor is None
    ex = executor if executor is not None else concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    exclusive_total = 0
    files_total = 0
    futures: list[concurrent.futures.Future[tuple[int, int]]] = []
    try:
        futures = [ex.submit(_size_candidate, client, chain, c) for c in candidates]
        for fut in concurrent.futures.as_completed(futures):
            if should_stop():
                raise Interrupted()
            b, f = fut.result()
            exclusive_total += b
            files_total += f
            progress.candidate_sized()
    finally:
        if owned:
            ex.shutdown(wait=False, cancel_futures=True)
        else:
            for fut in futures:
                fut.cancel()

    return RunExclusiveContribution(chain, exclusive_total, files_total)
