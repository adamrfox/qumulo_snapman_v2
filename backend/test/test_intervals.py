"""Tests for compute/intervals.py's pure interval arithmetic, focused on
run_exclusive_size -- the N-hop generalization of the three-way engine's
byte-range intersection (compute/run_exclusive.py's sizing primitive)."""

import unittest

from app.qumulo.compute.intervals import (
    intersection_size,
    run_exclusive_size,
    total_size,
)


class BasicsTest(unittest.TestCase):
    def test_total_size_sums_sizes(self) -> None:
        self.assertEqual(total_size([(0, 100), (500, 50)]), 150)

    def test_intersection_size_overlap_only(self) -> None:
        self.assertEqual(intersection_size([(0, 100)], [(50, 100)]), 50)
        self.assertEqual(intersection_size([(0, 100)], [(200, 100)]), 0)


class RunExclusiveSizeBasicsTest(unittest.TestCase):
    def test_no_deleted_snapshots_is_zero(self) -> None:
        # len(hops) - 1 == 0 deleted snapshots -- degenerate, contributes 0.
        self.assertEqual(run_exclusive_size([[(0, 100, True)]]), 0)

    def test_no_events_anywhere_is_zero(self) -> None:
        self.assertEqual(run_exclusive_size([[], [], []]), 0)

    def test_single_deleted_snapshot_matches_three_way_intersection(self) -> None:
        # k=1 (2 hops): born [0,100) at hop0, dies [50,150) at hop1 --
        # exactly the three-way engine's intersection, [50,100) = 50 bytes.
        hops = [[(0, 100, True)], [(50, 100, False)]]
        self.assertEqual(run_exclusive_size(hops), 50)


class ThreeHopChainTest(unittest.TestCase):
    """k=2 (3 hops): L, X1, X2, R -- exercises the case a plain
    sum-and-subtract formula can't get right (see deletion_estimate.py's
    CAVEAT and run_exclusive.py's module docstring)."""

    def test_born_at_x1_survives_through_x2_dies_before_r(self) -> None:
        # hop0 (L->X1): created. hop1 (X1->X2): untouched. hop2 (X2->R): deleted.
        hops = [[(0, 4096, True)], [], [(0, 4096, False)]]
        self.assertEqual(run_exclusive_size(hops), 4096)

    def test_born_at_x2_only_dies_at_r(self) -> None:
        # hop0: untouched. hop1 (X1->X2): created. hop2 (X2->R): deleted.
        hops = [[], [(0, 2048, True)], [(0, 2048, False)]]
        self.assertEqual(run_exclusive_size(hops), 2048)

    def test_content_already_at_l_dying_mid_run_is_not_exclusive(self) -> None:
        # Present at L, dies at hop1 (X1->X2) -- L still holds a copy of
        # nothing here (this instance was L's own), so it's not freed by
        # deleting just X1,X2 -- it was already gone from L's perspective
        # only in the sense that L itself IS the survivor being kept, and
        # this data simply isn't referenced by anything after it dies.
        # Not exclusive: birth_index stays 0 (matches L) the whole time.
        hops = [[], [(0, 4096, False)], []]
        self.assertEqual(run_exclusive_size(hops), 0)

    def test_content_born_within_run_surviving_to_r_is_not_exclusive(self) -> None:
        # Born at hop0, never dies within the run -- R still holds it.
        hops = [[(0, 4096, True)], [], []]
        self.assertEqual(run_exclusive_size(hops), 0)

    def test_two_different_instances_at_same_offset_neither_exclusive(self) -> None:
        """The exact false-positive a naive "sum born events, sum died
        events, then intersect" approach produces (and the reason this
        function does a real sweep instead -- see its docstring): L="A",
        X1="A" (unchanged), X2="B" (modified), R="B" (unchanged from X2).
        hop1 (X1->X2)'s single MODIFY entry is simultaneously "A's instance
        ends here" and "B's instance begins here" -- but neither instance is
        actually exclusive: A survives via L (matches at hop0, so never
        entered the eligible-birth window), B survives via R (never dies
        within the run). A naive independent union-then-intersect over
        "any hop shows a create/modify" and "any hop shows a modify/delete"
        would flag this range in both sets from hop1 alone and wrongly
        count it; run_exclusive_size must not."""
        hops = [[], [(0, 4096, True)], []]
        self.assertEqual(run_exclusive_size(hops), 0)

    def test_round_trip_back_to_ls_original_content_not_exclusive(self) -> None:
        # L="A", X1="B" (modified), X2="A" again (modified back) -- content
        # at X2 differs from what X1 had, but if it's genuinely the same as
        # L's original, R inherits it unchanged from X2. B (born at hop0,
        # dies at hop1) is exclusive; the "A again" instance (born at hop1,
        # never dies within the run) is not.
        hops = [[(0, 4096, True)], [(0, 4096, True)], []]
        self.assertEqual(run_exclusive_size(hops), 4096)  # only B's instance

    def test_multiple_non_overlapping_ranges_sum_independently(self) -> None:
        hops = [
            [(0, 100, True), (1000, 50, True)],
            [],
            [(0, 100, False)],  # only the first range dies within the run
        ]
        self.assertEqual(run_exclusive_size(hops), 100)

    def test_overlapping_ranges_at_different_hops_use_finest_breakpoints(self) -> None:
        # Born [0,100) at hop0, dies [50,150) at hop2 -- the exclusive
        # overlap is [50,100), 50 bytes, same reasoning as the two-hop case
        # but with an untouched hop1 in between.
        hops = [[(0, 100, True)], [], [(50, 100, False)]]
        self.assertEqual(run_exclusive_size(hops), 50)


class FourHopChainTest(unittest.TestCase):
    """k=3: confirms interior hops correctly serve double duty (ending one
    instance, starting the next) across a longer chain."""

    def test_instance_reborn_partway_through_still_tracked_correctly(self) -> None:
        # hop0: created (instance A, exclusive-eligible). hop1: untouched.
        # hop2: modified (A dies, instance B born -- also eligible).
        # hop3: deleted (B dies). Both A and B are fully within the run.
        hops = [[(0, 100, True)], [], [(0, 100, True)], [(0, 100, False)]]
        self.assertEqual(run_exclusive_size(hops), 200)  # A (100) + B (100)


if __name__ == "__main__":
    unittest.main()
