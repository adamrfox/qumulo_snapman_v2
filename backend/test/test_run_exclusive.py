"""Candidate-discovery and wiring tests for compute/run_exclusive.py -- the
N-snapshot generalization of snapshot_exclusive.py's three-way engine. The
core interval math (run_exclusive_size) is proven separately in
test_intervals.py; these tests exercise what's new here: scanning k+1 hops
instead of two, and generalizing the ephemeral/remaining_deleted directory
cases to a chain longer than three snapshots.
"""

import unittest

from app.qumulo.api import Snapshot
from app.qumulo.client import ApiError
from app.qumulo.compute.run_exclusive import compute_run_exclusive_contribution
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


class BasicChainTest(unittest.TestCase):
    """chain = [L, X1, X2, R] (k=2 deleted snapshots, 3 hops)."""

    def test_file_touched_at_both_ends_only(self) -> None:
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        path = "/data/db.bin"
        c.set_tree_diff(2, 1, [_t("CREATE", path)])
        c.set_tree_diff(3, 2, [])
        c.set_tree_diff(4, 3, [_t("DELETE", path)])
        c.set_file_diff(2, 1, path, [_f("CREATE", 0, 100)])
        c.set_attrs(3, path, _attrs("db-id", 100, path))
        c.set_error("db-id", ApiError(404, "fs_file_not_covered_by_snapshot_error", "gone"))

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        self.assertEqual(result.exclusive_bytes, 100)
        self.assertEqual(result.total_files, 1)

    def test_untouched_file_never_triggers_an_api_call(self) -> None:
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        c.set_tree_diff(2, 1, [])
        c.set_tree_diff(3, 2, [])
        c.set_tree_diff(4, 3, [])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        self.assertEqual(result.exclusive_bytes, 0)
        self.assertEqual(result.total_files, 0)

    def test_content_present_at_l_and_r_never_diffed_across_run(self) -> None:
        """Content that changes mid-run but is identical at L and R (e.g.
        reverted) never even needs to be examined by the offset-misaligned
        hops -- run_exclusive_size handles the math; this just confirms
        discovery still surfaces it as a candidate correctly rather than
        silently dropping it, matching the same content going to zero."""
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        path = "/data/reverted.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", path)])
        c.set_tree_diff(3, 2, [_t("MODIFY", path)])
        c.set_tree_diff(4, 3, [])
        c.set_file_diff(2, 1, path, [_f("MODIFY", 0, 4096)])
        c.set_file_diff(3, 2, path, [_f("MODIFY", 0, 4096)])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        # Born at hop0 (X1), dies at hop1 (X2) -- fully contained, counted.
        self.assertEqual(result.exclusive_bytes, 4096)
        self.assertEqual(result.total_files, 1)


class EphemeralDirectoryTest(unittest.TestCase):
    """A directory created and deleted entirely within a longer run (k=3,
    4 hops): every file under it is fully exclusive, discovered via one
    directory walk right after creation -- generalizing
    snapshot_exclusive.py's two-hop ephemeral-directory case."""

    def test_ephemeral_directory_spanning_interior_hops(self) -> None:
        c = TestClient()
        L, X1, X2, X3, R = _snap(1), _snap(2), _snap(3), _snap(4), _snap(5)
        dirpath = "/data/tmp/"
        f1, f2 = "/data/tmp/a.bin", "/data/tmp/b.bin"
        c.set_tree_diff(2, 1, [])            # hop0: untouched
        c.set_tree_diff(3, 2, [_t("CREATE", dirpath)])   # hop1: dir created
        c.set_tree_diff(4, 3, [])            # hop2: untouched (dir just sits there)
        c.set_tree_diff(5, 4, [_t("DELETE", dirpath)])   # hop3: dir deleted
        c.set_dir(3, dirpath.rstrip("/"), [_attrs("a-id", 100, f1), _attrs("b-id", 200, f2)])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, X3, R])

        self.assertEqual(result.exclusive_bytes, 300)
        self.assertEqual(result.total_files, 2)
        # No file_diff fixtures registered for f1/f2 -- if discovery had
        # tried to individually diff them instead of trusting the walk, the
        # TestClient would have raised AssertionError already.


class DirectoryDeletedMidRunTest(unittest.TestCase):
    """A directory that predates the run (not ephemeral) gets bulk-deleted
    partway through -- generalizing the pairwise/three-way engines'
    "remaining_deleted" case. A file under it, individually modified at an
    earlier hop but never re-enumerated at the bulk delete, still needs its
    death credited."""

    def test_file_modified_early_then_swept_by_a_later_bulk_delete(self) -> None:
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        dirpath = "/data/olddir/"
        filepath = "/data/olddir/f.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", filepath)])   # hop0: individually modified
        c.set_tree_diff(3, 2, [_t("DELETE", dirpath)])    # hop1: whole dir swept, not enumerated
        c.set_tree_diff(4, 3, [])                          # hop2: untouched
        c.set_file_diff(2, 1, filepath, [_f("MODIFY", 0, 1024)])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        self.assertEqual(result.exclusive_bytes, 1024)
        self.assertEqual(result.total_files, 1)

    def test_unrelated_never_touched_file_under_the_same_deleted_dir_is_ignored(self) -> None:
        """A file under the bulk-deleted directory that was NEVER
        individually touched at any hop must not be discovered at all (it
        was unchanged for the run's whole span, so it matches L and isn't
        exclusive) -- proven the same way as the untouched-file test above:
        no fixture registered for it, so any attempt to size it would raise."""
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        dirpath = "/data/olddir/"
        c.set_tree_diff(2, 1, [])
        c.set_tree_diff(3, 2, [_t("DELETE", dirpath)])
        c.set_tree_diff(4, 3, [])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        self.assertEqual(result.exclusive_bytes, 0)
        self.assertEqual(result.total_files, 0)


class UnresolvableFileTest(unittest.TestCase):
    def test_unresolvable_file_is_skipped_not_fatal(self) -> None:
        c = TestClient()
        L, X1, X2, R = _snap(1), _snap(2), _snap(3), _snap(4)
        bad, good = "/data/weird.bin", "/data/normal.bin"
        c.set_tree_diff(2, 1, [_t("MODIFY", bad), _t("MODIFY", good)])
        c.set_tree_diff(3, 2, [_t("MODIFY", bad), _t("MODIFY", good)])
        c.set_tree_diff(4, 3, [])
        c.set_file_diff_error(2, 1, bad, ApiError(404, "fs_no_such_file_version_error", "System error"))
        c.set_error("weird-id", ApiError(404, "fs_no_such_file_version_error", "System error"))
        c.set_attrs(1, bad, _attrs("weird-id", 999, bad))
        c.set_file_diff(2, 1, good, [_f("MODIFY", 0, 555)])
        c.set_file_diff(3, 2, good, [_f("MODIFY", 0, 555)])

        result = compute_run_exclusive_contribution(c, [L, X1, X2, R])

        self.assertEqual(result.exclusive_bytes, 555)
        self.assertEqual(result.total_files, 1)


class ChainTooShortTest(unittest.TestCase):
    def test_rejects_chain_without_at_least_one_deleted_snapshot(self) -> None:
        c = TestClient()
        L, R = _snap(1), _snap(2)
        with self.assertRaises(ValueError):
            compute_run_exclusive_contribution(c, [L, R])


if __name__ == "__main__":
    unittest.main()
