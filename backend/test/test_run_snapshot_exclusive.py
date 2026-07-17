"""Regression tests for run_snapshot_exclusive's held-neighbor skipping and
multi-triple error isolation -- covers the second production bug: a triple
whose *neighbor* (not the target itself) was a held snapshot with a huge
gap to its successor was silently stuck in the scan phase, and its chunk
blocked later, cheaper triples from even being reported as stuck-on.
"""

import tempfile
import unittest

from pathlib import Path

from app.qumulo.api import Snapshot
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError
from app.qumulo.compute.snapshot_exclusive_job import run_snapshot_exclusive
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
        self.boundary: dict | None = None
        self.triples: list[dict] = []
        self.finished = False

    def start_boundary(self, older, newer) -> None:
        pass

    def boundary_result(self, older, freed_bytes, total_files, *, cached, error=None, skipped_held=False) -> None:
        self.boundary = {"older": older.id, "freed_bytes": freed_bytes, "error": error, "skipped_held": skipped_held}

    def start_triple(self, index, total, prev, target, next) -> None:
        pass

    def triple_finished(self, index: int) -> None:
        pass

    def progress(self, index, found, sized) -> None:
        pass

    def triple_sized(self, exclusive_bytes: int) -> None:
        pass

    def triple_result(self, target, exclusive_bytes, total_files, *, cached, pending,
                       timed_out=False, error=None, skipped_held=False) -> None:
        self.triples.append({
            "target": target.id, "exclusive_bytes": exclusive_bytes,
            "pending": pending, "error": error, "skipped_held": skipped_held,
        })

    def no_middle_snapshots(self) -> None:
        pass

    def finish(self) -> None:
        self.finished = True


def _cache() -> Cache:
    return Cache(Path(tempfile.mkdtemp()) / "cache.db")


class HeldNeighborSkipTest(unittest.TestCase):
    def test_triple_skipped_when_prev_is_held_even_if_target_is_not(self) -> None:
        # S1 (held, huge gap) -> S2 (target, not held) -> S3 (not held).
        c = TestClient()
        snaps = [_snap(1, held=True), _snap(2), _snap(3), _snap(4)]
        # Boundary pair (1,2) would also be expensive -- registering nothing
        # for it proves it's skipped too (TestClient would raise if queried).

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run_snapshot_exclusive(
                c, cache, "cluster", snaps,
                limit=50, max_workers=4, observer=observer, should_stop=lambda: False,
            )
        finally:
            cache.close()

        self.assertTrue(observer.finished)
        self.assertTrue(observer.boundary["skipped_held"])
        target_results = {t["target"]: t for t in observer.triples}
        # snaps[1] (id=2) is the first middle snapshot: not held itself, but
        # its prev (id=1) is -- this is the exact production bug shape.
        self.assertTrue(target_results[2]["skipped_held"])
        # snaps[2] (id=3): neither it nor its neighbors (id=2, id=4) are
        # held, so it's a normal candidate (would attempt a real diff; no
        # fixtures registered for it here, so it's expected to error rather
        # than being wrongly marked skipped_held).
        self.assertFalse(target_results[3]["skipped_held"])

    def test_include_held_computes_it_anyway(self) -> None:
        c = TestClient()
        snaps = [_snap(1, held=True), _snap(2), _snap(3)]
        c.set_tree_diff(2, 1, [_t("MODIFY", "/a.bin")])
        c.set_tree_diff(3, 2, [_t("MODIFY", "/a.bin")])
        c.set_file_diff(2, 1, "/a.bin", [_f("MODIFY", 0, 100)])
        c.set_file_diff(3, 2, "/a.bin", [_f("MODIFY", 0, 100)])
        # Boundary pair also needs fixtures since include_held computes it too.
        c.set_file_diff(2, 1, "/a.bin", [_f("MODIFY", 0, 100)])

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run_snapshot_exclusive(
                c, cache, "cluster", snaps,
                limit=50, max_workers=4, observer=observer, should_stop=lambda: False,
                include_held=True,
            )
        finally:
            cache.close()

        self.assertFalse(observer.boundary["skipped_held"])
        self.assertFalse(observer.triples[0]["skipped_held"])
        # Same byte range rewritten at S2 and again at S3: S2's version is
        # its own distinct epoch, born after S1 and gone by S3 -- exclusive.
        self.assertEqual(observer.triples[0]["exclusive_bytes"], 100)


class TripleFailureDoesNotAbortTest(unittest.TestCase):
    def test_other_triples_still_complete_when_one_hard_fails(self) -> None:
        c = TestClient()
        snaps = [_snap(1), _snap(2), _snap(3), _snap(4), _snap(5)]
        # Boundary pair (1,2).
        c.set_tree_diff(2, 1, [_t("MODIFY", "/x.bin")])
        c.set_file_diff(2, 1, "/x.bin", [_f("MODIFY", 0, 10)])

        # Triple targeting snaps[1]=2 (prev=1, next=3): "/x.bin" matches on
        # both legs, but its S1->S2 fetch hard-fails.
        c.set_tree_diff(3, 2, [_t("MODIFY", "/x.bin"), _t("MODIFY", "/y.bin")])
        c.set_file_diff_error(2, 1, "/x.bin", ApiError(404, "snapshot_not_found_error", "gone"))

        # Triple targeting snaps[2]=3 (prev=2, next=4): "/y.bin" matches on
        # both legs cleanly. Its scan_a is the SAME tree_diff(3,2) as above
        # (adjacent triples share a leg) but "/x.bin" there is untouched on
        # this triple's scan_b, so it's filtered out without an API call --
        # only "/y.bin" gets diffed.
        c.set_tree_diff(4, 3, [_t("MODIFY", "/y.bin")])
        c.set_file_diff(3, 2, "/y.bin", [_f("MODIFY", 0, 4096)])
        c.set_file_diff(4, 3, "/y.bin", [_f("MODIFY", 0, 4096)])

        cache = _cache()
        observer = _RecordingObserver()
        try:
            run_snapshot_exclusive(
                c, cache, "cluster", snaps,
                limit=50, max_workers=4, observer=observer, should_stop=lambda: False,
                triple_workers=2,
            )
        finally:
            cache.close()

        self.assertTrue(observer.finished)
        by_target = {t["target"]: t for t in observer.triples}
        self.assertIsNotNone(by_target[2]["error"])
        self.assertEqual(by_target[3]["exclusive_bytes"], 4096)
        self.assertIsNone(by_target[3]["error"])


if __name__ == "__main__":
    unittest.main()
