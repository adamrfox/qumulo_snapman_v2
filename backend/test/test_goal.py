"""Tests for compute/goal.py -- the cluster-wide, efficiency-based
multi-tree space-recovery allocator.

The core property being verified: cuts are ranked across trees by bytes
freed per day of recent history sacrificed, not by raw byte size and not by
a uniform cutoff applied to every tree -- a small tree that frees a little
for almost no recency given up should be cut before a big tree that frees a
lot but costs many days of history, and same-day bursts (zero recency cost)
always win regardless of how efficient anything else looks.
"""

import unittest

from app.qumulo.compute.goal import TreeInput, allocate


def _pt(
    older_id: int,
    older_age: int,
    newer_id: int,
    newer_age: int,
    freed: int,
    cumulative: int,
    *,
    newer_date: str = "",
    status: str = "computed",
) -> dict:
    return {
        "older_id": older_id,
        "older_name": f"{older_id}_S",
        "older_age_days": older_age,
        "older_date": f"age-{older_age}",
        "newer_id": newer_id,
        "newer_date": newer_date or f"age-{newer_age}",
        "newer_age_days": newer_age,
        "freed_bytes": freed,
        "cumulative_bytes": cumulative,
        "total_files": 1,
        "status": status,
    }


class AllocateTest(unittest.TestCase):
    def test_single_tree_partial_cut(self):
        points = [
            _pt(1, 100, 2, 80, 10, 10),
            _pt(2, 80, 3, 50, 20, 30, newer_date="2026-01-01"),
            _pt(3, 50, 4, 10, 5, 35),
        ]
        result = allocate(25, [TreeInput("tree", points)])

        alloc = result.allocations[0]
        self.assertTrue(result.goal_met)
        self.assertEqual(result.total_freed_bytes, 30)
        self.assertEqual(alloc.deepest_index, 1)
        self.assertEqual(alloc.delete_snapshot_ids, [1, 2])
        self.assertEqual(alloc.delete_count, 2)
        self.assertEqual(alloc.reclaim_bytes, 30)
        self.assertEqual(alloc.keep_days, 50)
        self.assertEqual(alloc.delete_before, "2026-01-01")
        self.assertEqual(alloc.days_sacrificed, 50)  # 100 - 50

    def test_efficiency_ordering_prefers_cheaper_per_day(self):
        # Same gain (100 bytes), but tree_b costs far fewer days -- it should
        # be the one used to hit a target that only needs one tree's worth.
        tree_a = TreeInput("tree_a", [_pt(1, 100, 2, 90, 100, 100)])   # 10 bytes/day
        tree_b = TreeInput("tree_b", [_pt(3, 100, 4, 99, 100, 100)])   # 100 bytes/day

        result = allocate(100, [tree_a, tree_b])

        by_id = {a.source_file_id: a for a in result.allocations}
        self.assertIsNone(by_id["tree_a"].deepest_index)
        self.assertEqual(by_id["tree_b"].deepest_index, 0)
        self.assertTrue(result.goal_met)
        self.assertEqual(result.total_freed_bytes, 100)

    def test_zero_cost_step_wins_over_high_finite_efficiency(self):
        # tree_a's single step has a very high (but non-zero) bytes/day
        # ratio; tree_b's step costs zero days (same-day burst) but frees
        # far fewer bytes. The zero-cost step must still be taken first.
        tree_a = TreeInput("tree_a", [_pt(1, 100, 2, 0, 1000, 1000)])   # 10 bytes/day
        tree_b = TreeInput("tree_b", [_pt(3, 100, 4, 100, 1, 1)])       # free

        result = allocate(1, [tree_a, tree_b])

        by_id = {a.source_file_id: a for a in result.allocations}
        self.assertEqual(by_id["tree_b"].deepest_index, 0)
        self.assertIsNone(by_id["tree_a"].deepest_index)
        self.assertEqual(result.total_freed_bytes, 1)

    def test_free_zero_gain_step_left_stranded_is_excluded(self):
        # tree_a's step is free (same-day) and reclaims nothing; tree_b's
        # step is also free and alone satisfies the goal. If tree_a's
        # worthless step gets popped first (tie-broken by source_file_id)
        # before tree_b's real one, it must not show up as a recommended
        # cut -- there's no reason to ever recommend deleting for 0 bytes.
        tree_a = TreeInput("tree_a", [_pt(1, 50, 2, 50, 0, 0)])
        tree_b = TreeInput("tree_b", [_pt(3, 50, 4, 50, 100, 100)])

        result = allocate(100, [tree_a, tree_b])

        by_id = {a.source_file_id: a for a in result.allocations}
        self.assertIsNone(by_id["tree_a"].deepest_index)
        self.assertEqual(by_id["tree_a"].delete_count, 0)
        self.assertEqual(by_id["tree_a"].delete_snapshot_ids, [])
        self.assertEqual(by_id["tree_b"].deepest_index, 0)
        self.assertTrue(result.goal_met)

    def test_unnecessary_free_step_trimmed_once_a_costly_step_alone_suffices(self):
        # tree_free's step is free and reclaims a real 150 bytes, but isn't
        # enough alone to hit the goal. tree_costly's step costs 1000 days
        # but alone clears the 200-byte goal (210 bytes). Since tree_free is
        # free it gets taken first by the forward pass -- but it turns out
        # unnecessary once tree_costly's step alone covers the goal, so the
        # final plan should drop tree_free entirely rather than recommend
        # deleting it for no reason (this is the "over-suggesting" case: a
        # 200 GiB goal that recommends 360 GiB across two trees when one
        # tree's 208 GiB alone would have done it).
        tree_free = TreeInput("tree_free", [_pt(1, 50, 2, 50, 150, 150)])
        tree_costly = TreeInput("tree_costly", [_pt(3, 2000, 4, 1000, 210, 210)])

        result = allocate(200, [tree_free, tree_costly])

        by_id = {a.source_file_id: a for a in result.allocations}
        self.assertIsNone(by_id["tree_free"].deepest_index)
        self.assertEqual(by_id["tree_costly"].deepest_index, 0)
        self.assertEqual(result.total_freed_bytes, 210)
        self.assertTrue(result.goal_met)

    def test_goal_not_met_reports_shortfall(self):
        points = [_pt(1, 100, 2, 50, 10, 10)]
        result = allocate(100, [TreeInput("tree", points)])

        self.assertFalse(result.goal_met)
        self.assertEqual(result.total_freed_bytes, 10)
        self.assertEqual(result.shortfall, 90)
        self.assertEqual(result.allocations[0].deepest_index, 0)

    def test_zero_target_takes_nothing(self):
        points = [_pt(1, 100, 2, 50, 10, 10)]
        result = allocate(0, [TreeInput("tree", points)])

        self.assertTrue(result.goal_met)
        self.assertEqual(result.total_freed_bytes, 0)
        self.assertIsNone(result.allocations[0].deepest_index)

    def test_tie_break_is_deterministic_by_source_file_id(self):
        tree_a = TreeInput("a", [_pt(1, 100, 2, 50, 10, 10)])
        tree_b = TreeInput("b", [_pt(3, 100, 4, 50, 10, 10)])

        result = allocate(10, [tree_b, tree_a])  # pass in reverse order too

        by_id = {a.source_file_id: a for a in result.allocations}
        self.assertEqual(by_id["a"].deepest_index, 0)
        self.assertIsNone(by_id["b"].deepest_index)

    def test_unmeasured_point_raises(self):
        points = [_pt(1, 100, 2, 50, None, None, status="pending")]
        with self.assertRaises(ValueError):
            allocate(10, [TreeInput("tree", points)])


if __name__ == "__main__":
    unittest.main()
