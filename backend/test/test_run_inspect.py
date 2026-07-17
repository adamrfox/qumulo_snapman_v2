"""Regression tests for run_inspect's multi-pair error isolation and
held-snapshot skipping -- this is the actual bug a user hit in production:
one pair's target snapshot expiring mid-diff was aborting the entire
multi-hundred-pair run instead of just failing that one pair.
"""

import tempfile
import unittest

from pathlib import Path

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError
from app.qumulo.compute.inspect import run_inspect
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


class _RecordingObserver:
    def __init__(self) -> None:
        self.results: list[dict] = []
        self.finished = False
        self.errored = False

    def set_overlapped(self, overlapped: bool) -> None:
        pass

    def start_pair(self, index, total, older, newer) -> None:
        pass

    def pair_finished(self, index: int) -> None:
        pass

    def progress(self, index: int, found: int, sized: int) -> None:
        pass

    def pair_sized(self, freed_bytes: int) -> None:
        pass

    def pair_result(self, older, newer, freed_bytes, cumulative, total_files, *,
                     cached, pending, timed_out=False, error=None, skipped_held=False) -> None:
        self.results.append({
            "older": older.id, "newer": newer.id, "freed_bytes": freed_bytes,
            "cached": cached, "pending": pending, "timed_out": timed_out,
            "error": error, "skipped_held": skipped_held,
        })

    def no_curve(self) -> None:
        pass

    def finish(self) -> None:
        self.finished = True


def _cache() -> Cache:
    return Cache(Path(tempfile.mkdtemp()) / "cache.db")


class OnePairFailureDoesNotAbortTest(unittest.TestCase):
    def test_other_pairs_still_complete_when_one_pair_hard_fails(self) -> None:
        c = TestClient()
        snaps = [_snap(1), _snap(2), _snap(3)]
        # Pair (1,2): fails hard (its "newer" snapshot effectively vanished).
        c.set_tree_diff(2, 1, [_t("MODIFY", "/a.bin")])
        c.set_file_diff_error(2, 1, "/a.bin", ApiError(404, "snapshot_not_found_error", "gone"))
        # Pair (2,3): perfectly fine.
        c.set_tree_diff(3, 2, [_t("MODIFY", "/b.bin")])
        c.set_file_diff(3, 2, "/b.bin", [_f("MODIFY", 0, 4096)])

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run_inspect(
                c, cache, "cluster", snaps,
                limit=50, max_workers=4, observer=observer, should_stop=lambda: False,
                pair_workers=2,
            )
        finally:
            cache.close()

        self.assertTrue(observer.finished)
        by_pair = {(r["older"], r["newer"]): r for r in observer.results}
        self.assertEqual(by_pair[(1, 2)]["error"], "[404] snapshot_not_found_error: gone")
        self.assertIsNone(by_pair[(1, 2)]["freed_bytes"])
        # The other pair's result must still be present and correct, not
        # swallowed by the first pair's failure.
        self.assertEqual(by_pair[(2, 3)]["freed_bytes"], 4096)
        self.assertFalse(by_pair[(2, 3)]["error"])


class HeldSnapshotSkipTest(unittest.TestCase):
    def test_held_older_is_skipped_by_default_and_computed_when_opted_in(self) -> None:
        c = TestClient()
        snaps = [_snap(1, held=True), _snap(2)]
        c.set_tree_diff(2, 1, [_t("MODIFY", "/a.bin")])
        c.set_file_diff(2, 1, "/a.bin", [_f("MODIFY", 0, 4096)])

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run_inspect(
                c, cache, "cluster", snaps,
                limit=50, max_workers=4, observer=observer, should_stop=lambda: False,
            )
        finally:
            cache.close()

        self.assertTrue(observer.results[0]["skipped_held"])
        self.assertIsNone(observer.results[0]["freed_bytes"])

        # Opting in computes it for real.
        cache2 = _cache()
        observer2 = _RecordingObserver()
        try:
            run_inspect(
                c, cache2, "cluster", snaps,
                limit=50, max_workers=4, observer=observer2, should_stop=lambda: False,
                include_held=True,
            )
        finally:
            cache2.close()

        self.assertFalse(observer2.results[0]["skipped_held"])
        self.assertEqual(observer2.results[0]["freed_bytes"], 4096)


if __name__ == "__main__":
    unittest.main()
