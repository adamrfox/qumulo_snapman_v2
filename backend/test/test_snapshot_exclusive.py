"""Candidate-discovery and wiring tests for compute/snapshot_exclusive.py.

The pure per-file math (compute_freed_bytes) is already proven correct by
qsnap's own test suite (test/test_reclaim.py) and is imported unmodified here.
These tests exercise the part that's actually new: which files get diffed at
all, how the two tree-diff scans get joined, and that untouched files never
trigger an API call.
"""

import unittest

from app.qumulo.api import Snapshot
from app.qumulo.client import ApiError
from app.qumulo.compute.snapshot_exclusive import compute_snapshot_exclusive_contribution
from test.client import TestClient

SRC = "95200597194320772806937149443"


def _snap(id_: int) -> Snapshot:
    return Snapshot(
        id=id_,
        name=f"{id_}_S",
        timestamp=f"2026-05-{20 + id_:02d}T00:00:00Z",
        source_file_id=SRC,
        policy_id=None,
        expiration="",
        in_delete=False,
    )


def _t(op: str, path: str) -> dict:
    return {"op": op, "path": path}


def _f(op: str, offset: int, size: int) -> dict:
    return {"op": op, "offset": str(offset), "size": str(size)}


def _attrs(id_: str, size: int, path: str) -> dict:
    return {"id": id_, "size": str(size), "logical_datablocks": str(-(-size // 4096)), "type": "FS_FILE_TYPE_FILE", "path": path}


class DirectMatchTest(unittest.TestCase):
    """A file touched on both legs: only the intersection of ranges is exclusive to S2."""

    def test_middle_write_is_exclusive_to_s2(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        path = "/data/block.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", path)])
        c.set_tree_diff(3, 2, [_t("MODIFY", path)])
        # S2 rewrote the whole 4096-byte block; S3 rewrites it again.
        c.set_file_diff(2, 1, path, [_f("MODIFY", 0, 4096)])
        c.set_file_diff(3, 2, path, [_f("MODIFY", 0, 4096)])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 4096)
        self.assertEqual(result.total_files, 1)

    def test_untouched_file_never_triggers_an_api_call(self) -> None:
        """A file with no tree-diff entry on either leg must be skipped without
        ever calling file_diff -- proven by not registering a fixture for it:
        TestClient raises AssertionError if it's requested anyway."""
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        c.set_tree_diff(2, 1, [])
        c.set_tree_diff(3, 2, [])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 0)
        self.assertEqual(result.total_files, 0)

    def test_touched_only_on_s1_s2_leg_contributes_nothing(self) -> None:
        """File changed between S1 and S2 but untouched S2->S3 is fully shared
        forward -- must be skipped without a file_diff call on either leg."""
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        path = "/data/only_a.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", path)])
        c.set_tree_diff(3, 2, [])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 0)
        self.assertEqual(result.total_files, 0)


class AncestorJoinTest(unittest.TestCase):
    """A file individually touched on only one leg because its ancestor
    directory was wholesale created/deleted on the other leg."""

    def test_file_under_dir_created_at_s2_is_synthesized_on_s1_side(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        dirpath = "/data/newdir/"
        filepath = "/data/newdir/f.bin"
        c.set_tree_diff(2, 1, [_t("CREATE", dirpath)])  # whole dir collapses to one entry
        c.set_tree_diff(3, 2, [_t("MODIFY", filepath)])  # file individually touched S2->S3
        # S1->S2 side: the real diff API call for a file whose ancestor didn't
        # exist at S1 still returns a proper CREATE range.
        c.set_file_diff(2, 1, filepath, [_f("CREATE", 0, 2048)])
        c.set_file_diff(3, 2, filepath, [_f("MODIFY", 0, 2048)])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 2048)
        self.assertEqual(result.total_files, 1)

    def test_file_under_dir_deleted_at_s3_is_synthesized_on_s2_side(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        dirpath = "/data/olddir/"
        filepath = "/data/olddir/f.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", filepath)])
        c.set_tree_diff(3, 2, [_t("DELETE", dirpath)])  # whole dir collapses to one entry
        c.set_file_diff(2, 1, filepath, [_f("MODIFY", 0, 1024)])
        # S2->S3 side is synthetic: file has no presence at S3, confirmed via
        # attrs-at-target + a 404 probe at next (not just renamed).
        c.set_attrs(2, filepath, _attrs("f-id", 1024, filepath))
        c.set_error("f-id", ApiError(404, "fs_file_not_covered_by_snapshot_error", "gone"))

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 1024)
        self.assertEqual(result.total_files, 1)


class EphemeralDirectoryTest(unittest.TestCase):
    """A directory created after S1 and wholly gone by S3: every file under
    it is 100% exclusive to S2, discovered via a directory walk, no diffing."""

    def test_ephemeral_directory_is_fully_exclusive(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        dirpath = "/data/tmp/"
        f1, f2 = "/data/tmp/a.bin", "/data/tmp/b.bin"
        c.set_tree_diff(2, 1, [_t("CREATE", dirpath)])
        c.set_tree_diff(3, 2, [_t("DELETE", dirpath)])
        # read_dir_in_snapshot normalizes the path (strips the trailing slash)
        # before requesting it, so the fixture must be registered without one.
        c.set_dir(2, dirpath.rstrip("/"), [_attrs("a-id", 100, f1), _attrs("b-id", 200, f2)])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        self.assertEqual(result.exclusive_bytes, 300)
        self.assertEqual(result.total_files, 2)
        # No file_diff fixtures were registered for f1/f2 -- if the code had
        # tried to diff them instead of trusting the ephemeral-dir walk, the
        # TestClient would have raised AssertionError already.


class UnresolvableFileTest(unittest.TestCase):
    """A file that 404s with something other than 'snapshot not found' (e.g.
    fs_no_such_file_version_error) must be excluded from the total, not crash
    the whole run -- regression test for a real production failure."""

    def test_unresolvable_file_is_skipped_not_fatal(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        bad, good = "/data/weird.bin", "/data/normal.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", bad), _t("MODIFY", good)])
        c.set_tree_diff(3, 2, [_t("MODIFY", bad), _t("MODIFY", good)])
        c.set_file_diff_error(
            2, 1, bad, ApiError(404, "fs_no_such_file_version_error", "System error")
        )
        # _fetch_s1_s2's fallback path also needs a place to fail cleanly:
        # the file_id resolution attempt on both snapshots.
        c.set_error("weird-id", ApiError(404, "fs_no_such_file_version_error", "System error"))
        c.set_attrs(1, bad, _attrs("weird-id", 999, bad))
        c.set_file_diff(2, 1, good, [_f("MODIFY", 0, 555)])
        c.set_file_diff(3, 2, good, [_f("MODIFY", 0, 555)])

        result = compute_snapshot_exclusive_contribution(c, prev, target, next_)

        # The good file still gets counted; the bad one is silently excluded
        # rather than raising and losing the whole run.
        self.assertEqual(result.exclusive_bytes, 555)
        self.assertEqual(result.total_files, 1)

    def test_snapshot_not_found_still_aborts(self) -> None:
        """A 404 that means the snapshot itself is gone is a systemic problem,
        not a per-file quirk -- it must still raise."""
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        path = "/data/x.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", path)])
        c.set_tree_diff(3, 2, [_t("MODIFY", path)])
        c.set_file_diff_error(2, 1, path, ApiError(404, "snapshot_not_found_error", "gone"))

        with self.assertRaises(ApiError):
            compute_snapshot_exclusive_contribution(c, prev, target, next_)


class ResumeTest(unittest.TestCase):
    def test_resume_skips_already_folded_candidates(self) -> None:
        c = TestClient()
        prev, target, next_ = _snap(1), _snap(2), _snap(3)
        pa, pb = "/data/a.bin", "/data/b.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", pa), _t("MODIFY", pb)])
        c.set_tree_diff(3, 2, [_t("MODIFY", pa), _t("MODIFY", pb)])
        c.set_file_diff(2, 1, pa, [_f("MODIFY", 0, 111)])
        c.set_file_diff(3, 2, pa, [_f("MODIFY", 0, 111)])
        c.set_file_diff(2, 1, pb, [_f("MODIFY", 0, 222)])
        c.set_file_diff(3, 2, pb, [_f("MODIFY", 0, 222)])

        # Candidates are sorted by path: a.bin (index 0) is treated as already
        # folded via resume=(1, 111, 1); only b.bin should be sized.
        result = compute_snapshot_exclusive_contribution(
            c, prev, target, next_, resume=(1, 111, 1)
        )

        self.assertEqual(result.exclusive_bytes, 111 + 222)
        self.assertEqual(result.total_files, 2)


if __name__ == "__main__":
    unittest.main()
