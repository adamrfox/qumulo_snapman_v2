"""Tests for compute/deletion_estimate.py -- the combined multi-snapshot
space-freed estimate.

The critical thing being verified here is the formula itself: summing each
selected snapshot's independently-computed "exclusive" size UNDERCOUNTS when
two selected snapshots are adjacent, because data shared only between them
also gets freed once neither survives. This is exactly the real bug a user
hit: a file created one snapshot earlier than intended, present unchanged
across two consecutive snapshots, then deleted -- each snapshot individually
shows 0 (the three-way engine correctly says neither is the exclusive
owner), but deleting both together must report the full file size.
"""

import tempfile
import unittest

from pathlib import Path

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError
from app.qumulo.compute.deletion_estimate import (
    Run,
    SelectionError,
    partition_into_runs,
    run_deletion_estimate,
    validate_selection,
)
from app.qumulo.compute.snapshot_exclusive import compute_snapshot_exclusive_contribution
from test.client import TestClient

SRC = "95200597194320772806937149443"


def _snap(id_: int, *, held: bool = False) -> Snapshot:
    return Snapshot(
        id=id_,
        name=f"{id_}_S",
        timestamp=f"2026-05-{20 + id_:02d}T00:00:00Z",
        source_file_id=SRC,
        policy_id=None,
        expiration="",
        in_delete=False,
        has_owners=held,
    )


def _t(op: str, path: str) -> dict:
    return {"op": op, "path": path}


def _f(op: str, offset: int, size: int) -> dict:
    return {"op": op, "offset": str(offset), "size": str(size)}


def _attrs(id_: str, size: int, path: str) -> dict:
    return {"id": id_, "size": str(size), "logical_datablocks": str(-(-size // 4096)), "type": "FS_FILE_TYPE_FILE", "path": path}


class _RecordingObserver:
    def __init__(self) -> None:
        self.run_results: dict[int, tuple[int | None, str | None]] = {}
        self.estimate: tuple[int, bool] | None = None
        self.finished = False

    def run_start(self, run_index, total_runs, run) -> None:
        pass

    def pair_start(self, older, newer) -> None:
        pass

    def pair_done(self, older, newer, freed_bytes, *, cached) -> None:
        pass

    def run_result(self, run_index, freed_bytes, error) -> None:
        self.run_results[run_index] = (freed_bytes, error)

    def estimate_result(self, total_bytes, complete) -> None:
        self.estimate = (total_bytes, complete)

    def finish(self) -> None:
        self.finished = True


def _cache() -> Cache:
    return Cache(Path(tempfile.mkdtemp()) / "cache.db")


class PartitionTest(unittest.TestCase):
    def test_contiguous_selection_is_one_run(self) -> None:
        snaps = [_snap(i) for i in range(6)]  # ids 0..5
        runs = partition_into_runs(snaps, {1, 2, 3})
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].left.id, 0)
        self.assertEqual(runs[0].right.id, 4)
        self.assertEqual([s.id for s in runs[0].deleted], [1, 2, 3])

    def test_gap_splits_into_two_runs(self) -> None:
        """Mirrors a held snapshot sitting in the middle of an otherwise
        contiguous pick -- the caller simply excludes it from selected_ids,
        and partitioning does the rest with no special-casing."""
        snaps = [_snap(i) for i in range(6)]
        runs = partition_into_runs(snaps, {1, 2, 4})  # id 3 excluded (e.g. held)
        self.assertEqual(len(runs), 2)
        run1, run2 = runs
        self.assertEqual((run1.left.id, [s.id for s in run1.deleted], run1.right.id), (0, [1, 2], 3))
        self.assertEqual((run2.left.id, [s.id for s in run2.deleted], run2.right.id), (3, [4], 5))


class ValidationTest(unittest.TestCase):
    def test_rejects_newest(self) -> None:
        snaps = [_snap(i) for i in range(4)]
        with self.assertRaises(SelectionError):
            validate_selection(snaps, {3})

    def test_rejects_held(self) -> None:
        snaps = [_snap(0), _snap(1, held=True), _snap(2), _snap(3)]
        with self.assertRaises(SelectionError):
            validate_selection(snaps, {1})

    def test_rejects_empty(self) -> None:
        snaps = [_snap(i) for i in range(4)]
        with self.assertRaises(SelectionError):
            validate_selection(snaps, set())

    def test_accepts_valid_middle_selection(self) -> None:
        snaps = [_snap(i) for i in range(4)]
        validate_selection(snaps, {1, 2})  # should not raise


class RunLengthOneMatchesThreeWayTest(unittest.TestCase):
    """For a single-snapshot run, the general formula must agree exactly
    with the already-shipped three-way engine -- this is the regression
    guard proving the two formulas are the same math."""

    def test_matches_compute_snapshot_exclusive_contribution(self) -> None:
        c = TestClient()
        s1, s2, s3 = _snap(1), _snap(2), _snap(3)
        path = "/data/block.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", path)])
        c.set_tree_diff(3, 2, [_t("MODIFY", path)])
        c.set_file_diff(2, 1, path, [_f("MODIFY", 0, 4096)])
        c.set_file_diff(3, 2, path, [_f("MODIFY", 0, 4096)])
        # Direct S1<->S3 diff needed by the run formula (not needed by the
        # three-way engine, which never compares S1 to S3 directly). S1 and
        # S3 hold genuinely different rewrites of this block (each MODIFY is
        # a distinct extent), so the direct comparison must ALSO show a
        # MODIFY here -- it would only be empty if S1 and S3 ended up with
        # the same net data (e.g. a create-then-delete cancelling out, as in
        # the fox-demo case below). Getting this fixture wrong is exactly
        # the trap the formula's subtraction term exists to guard against.
        c.set_tree_diff(3, 1, [_t("MODIFY", path)])
        c.set_file_diff(3, 1, path, [_f("MODIFY", 0, 4096)])

        three_way = compute_snapshot_exclusive_contribution(c, s1, s2, s3)

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run = Run(left=s1, right=s3, deleted=[s2])
            run_deletion_estimate(
                c, cache, "cluster", SRC, [run],
                max_workers=4, observer=observer, should_stop=lambda: False,
            )
        finally:
            cache.close()

        self.assertEqual(observer.estimate, (three_way.exclusive_bytes, True))
        self.assertEqual(three_way.exclusive_bytes, 4096)


class FoxDemoRegressionTest(unittest.TestCase):
    """The exact production scenario: a file created one snapshot earlier
    than intended, present unchanged across two consecutive snapshots, then
    deleted. Each snapshot individually is 0; combined must be the full size."""

    def test_two_adjacent_zero_snapshots_combine_to_full_file_size(self) -> None:
        c = TestClient()
        L, A, B, R = _snap(1), _snap(2), _snap(3), _snap(4)
        path = "/snap_testing/p0/copy.pdf"
        FILE_SIZE = 10_737_418_240  # 10 GiB, matching the real case

        c.set_tree_diff(2, 1, [_t("CREATE", path)])   # file created between L and A
        c.set_tree_diff(3, 2, [])                      # unchanged between A and B
        c.set_tree_diff(4, 3, [_t("DELETE", path)])    # deleted between B and R
        c.set_tree_diff(4, 1, [])                       # direct L<->R: net no-op, invisible

        c.set_attrs(3, path, _attrs("copy-id", FILE_SIZE, path))
        # Confirms the file is truly gone by R (not renamed) via a file_id
        # probe at R, mirroring the pairwise engine's _deleted_freed.
        c.set_error("copy-id", ApiError(404, "fs_file_not_covered_by_snapshot_error", "gone"))

        # Individually: three-way for A (prev=L, next=B) and for B (prev=A, next=R)
        # both see the file present on the "shared" side -> 0 each. Confirm.
        exclusive_a = compute_snapshot_exclusive_contribution(c, L, A, B)
        exclusive_b = compute_snapshot_exclusive_contribution(c, A, B, R)
        self.assertEqual(exclusive_a.exclusive_bytes, 0)
        self.assertEqual(exclusive_b.exclusive_bytes, 0)

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run = Run(left=L, right=R, deleted=[A, B])
            run_deletion_estimate(
                c, cache, "cluster", SRC, [run],
                max_workers=4, observer=observer, should_stop=lambda: False,
            )
        finally:
            cache.close()

        self.assertEqual(observer.estimate, (FILE_SIZE, True))


class MultiRunTest(unittest.TestCase):
    def test_two_independent_runs_sum_with_no_interaction(self) -> None:
        c = TestClient()
        # Run 1: snapshots 1,2,3 -- middle snapshot 2 exclusive of 4096 bytes.
        s1, s2, s3 = _snap(1), _snap(2), _snap(3)
        p1 = "/data/a.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", p1)])
        c.set_tree_diff(3, 2, [_t("MODIFY", p1)])
        c.set_file_diff(2, 1, p1, [_f("MODIFY", 0, 4096)])
        c.set_file_diff(3, 2, p1, [_f("MODIFY", 0, 4096)])
        c.set_tree_diff(3, 1, [_t("MODIFY", p1)])
        c.set_file_diff(3, 1, p1, [_f("MODIFY", 0, 4096)])

        # Run 2: snapshots 10,11,12 -- middle snapshot 11 exclusive of 2048 bytes.
        s10, s11, s12 = _snap(10), _snap(11), _snap(12)
        p2 = "/data/b.bin"
        c.set_tree_diff(11, 10, [_t("MODIFY", p2)])
        c.set_tree_diff(12, 11, [_t("MODIFY", p2)])
        c.set_file_diff(11, 10, p2, [_f("MODIFY", 0, 2048)])
        c.set_file_diff(12, 11, p2, [_f("MODIFY", 0, 2048)])
        c.set_tree_diff(12, 10, [_t("MODIFY", p2)])
        c.set_file_diff(12, 10, p2, [_f("MODIFY", 0, 2048)])

        cache = _cache()
        observer = _RecordingObserver()
        try:
            runs = [
                Run(left=s1, right=s3, deleted=[s2]),
                Run(left=s10, right=s12, deleted=[s11]),
            ]
            run_deletion_estimate(
                c, cache, "cluster", SRC, runs,
                max_workers=4, observer=observer, should_stop=lambda: False,
            )
        finally:
            cache.close()

        self.assertEqual(observer.estimate, (4096 + 2048, True))
        self.assertEqual(observer.run_results[0][0], 4096)
        self.assertEqual(observer.run_results[1][0], 2048)


if __name__ == "__main__":
    unittest.main()
