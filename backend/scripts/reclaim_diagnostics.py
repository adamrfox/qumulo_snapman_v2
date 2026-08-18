"""Diagnose "Qumulo's UI says N TB of snapshot capacity, snapman says we can
only reclaim M TB" reports, where N >> M.

Run inside the backend container:

    docker compose exec -T backend python scripts/reclaim_diagnostics.py [display_name]

With no argument, reports on every registered cluster. Read-only: it only
lists snapshots and reads the local pair-contribution cache -- it never
deletes anything or launches an Inspect run, so it's always safe to run
against production.

Background: snapman's per-tree "reclaim" figure (Dashboard's Reclaim~
column, the goal solver's ceiling, a tree's curve total) is NOT the same
quantity as Qumulo's cluster-wide "snapshot capacity" figure, even on a
perfectly healthy system, for two structural reasons this script checks for
directly:

  1. Scope: Qumulo's figure covers every snapshot on the cluster. snapman's
     figure only covers pairs it has actually computed -- a source_file_id
     "tree" nobody has ever Inspected (manually, via keep-warm, or via the
     goal solver) contributes 0 no matter how much space its snapshots hold.

  2. The curve's cumulative total FREEZES at the first unmeasured/held pair
     in a tree's oldest-to-newest sequence (app/qumulo/compute/curve.py's
     build_points -- deliberately, since a running total that silently
     skipped a gap would be wrong, not just incomplete). If pair 3 of 30 in
     some tree is blocked by a held/replication-owned snapshot, that tree's
     displayed cumulative reclaim is capped at whatever pair 1-2 freed, even
     if pairs 4-30 are fully computed and represent real, additional,
     already-known reclaimable bytes. This script surfaces that hidden
     amount separately (raw_computed_bytes) so it's visible even when the
     UI's own number can't show it.

Read both totals this script prints for each tree, and skew between them
across many trees is exactly the shape of gap this script exists to find.
"""

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select

sys.path.insert(0, "/app")

from app.database import SessionLocal  # noqa: E402
from app.models import Cluster  # noqa: E402
from app.qumulo import api  # noqa: E402
from app.qumulo.compute.groups import group_snapshots  # noqa: E402
from app.routers.inspect import _open_cache, checked_cluster_name, make_qclient  # noqa: E402


def _fmt_bytes(n: int) -> str:
    tb = n / 1e12
    if tb >= 0.01:
        return f"{tb:.3f} TB"
    return f"{n / 1e9:.3f} GB"


async def _diagnose_cluster(cluster: Cluster) -> None:
    loop = asyncio.get_event_loop()
    cache = _open_cache()

    qclient = make_qclient(cluster)
    cluster_name = await loop.run_in_executor(None, checked_cluster_name, qclient)
    snaps = await loop.run_in_executor(None, api.list_snapshots, qclient)
    now = datetime.now(timezone.utc)
    groups = group_snapshots(snaps, now)

    total_locked = sum(1 for s in snaps if s.locked)
    total_owned = sum(1 for s in snaps if s.has_owners)

    print(f"=== {cluster.display_name} ({cluster_name}) ===")
    print(f"Total snapshots: {len(snaps)}  |  locked: {total_locked}  |  replication-owned: {total_owned}")
    print(f"Total groups (distinct trees with a snapshot history): {len(groups)}")
    print()

    never_inspected = 0
    fully_measured = 0
    partially_measured = 0
    grand_displayed = 0  # what the UI's cumulative total would show today (post-freeze)
    grand_raw = 0  # sum of every individually-computed pair, ignoring the freeze
    rows = []

    for g in groups:
        snaps_sorted = sorted(g.snapshots, key=lambda s: s.id)
        pairs = cache.get_pairs(cluster_name, g.source_file_id)
        total_pairs = len(snaps_sorted) - 1
        if total_pairs <= 0:
            continue  # single-snapshot group, nothing to prune

        # Walk the CURRENT live adjacent-pair sequence only. cache.get_pairs()
        # can hold stale rows for snapshots that have since been deleted (the
        # cache is never pruned when a snapshot goes away) -- summing pairs
        # blindly by dict value, or counting len(pairs) as "measured", would
        # double-count deleted history that isn't part of today's curve at
        # all. Matching build_points' own iteration keeps both totals honest.
        raw_bytes = 0  # every live pair snapman has already computed, gaps or not
        displayed_bytes = 0  # freezes at the first gap, exactly like build_points
        measured_pairs = 0
        first_gap_index = None
        first_gap_reason = None
        seen_gap = False
        for i, (older, newer) in enumerate(zip(snaps_sorted[:-1], snaps_sorted[1:])):
            pair_data = pairs.get((older.id, newer.id))
            if pair_data is None:
                if not seen_gap:
                    first_gap_index = i
                    first_gap_reason = older.held_reason if older.held else "not yet measured"
                    seen_gap = True
                continue
            measured_pairs += 1
            raw_bytes += pair_data[0]
            if not seen_gap:
                displayed_bytes += pair_data[0]

        if measured_pairs == 0:
            never_inspected += 1
        elif measured_pairs >= total_pairs:
            fully_measured += 1
        else:
            partially_measured += 1

        grand_displayed += displayed_bytes
        grand_raw += raw_bytes

        hidden = raw_bytes - displayed_bytes
        if hidden > 0 or measured_pairs < total_pairs:
            rows.append({
                "source_file_id": g.source_file_id,
                "total_pairs": total_pairs,
                "measured_pairs": measured_pairs,
                "first_gap_index": first_gap_index,
                "first_gap_reason": first_gap_reason,
                "displayed_bytes": displayed_bytes,
                "raw_bytes": raw_bytes,
                "hidden_bytes": hidden,
            })

    print(f"Trees never Inspected at all: {never_inspected}")
    print(f"Trees fully measured:         {fully_measured}")
    print(f"Trees partially measured:     {partially_measured}")
    print()
    print(f"Sum of displayed cumulative reclaim (matches the UI today): {_fmt_bytes(grand_displayed)}")
    print(f"Sum of every individually-computed pair, ignoring gaps:     {_fmt_bytes(grand_raw)}")
    if grand_raw > grand_displayed:
        print(f"  -> {_fmt_bytes(grand_raw - grand_displayed)} is already computed and known, but hidden")
        print("     from every total/report because it sits behind an earlier gap in its tree.")
    print()

    if rows:
        rows.sort(key=lambda r: r["hidden_bytes"], reverse=True)
        print("Trees with a gap (partially measured) or hidden bytes behind one, worst first:")
        print(f"{'source_file_id':<32} {'measured':>10} {'gap@':>6} {'gap reason':<24} {'displayed':>12} {'raw':>12} {'hidden':>12}")
        for r in rows[:25]:
            gap_at = str(r["first_gap_index"]) if r["first_gap_index"] is not None else "-"
            reason = r["first_gap_reason"] or "-"
            measured_str = f"{r['measured_pairs']}/{r['total_pairs']}"
            print(
                f"{r['source_file_id']:<32} "
                f"{measured_str:>10} "
                f"{gap_at:>6} {reason:<24} "
                f"{_fmt_bytes(r['displayed_bytes']):>12} "
                f"{_fmt_bytes(r['raw_bytes']):>12} "
                f"{_fmt_bytes(r['hidden_bytes']):>12}"
            )
        if len(rows) > 25:
            print(f"... and {len(rows) - 25} more")
    print()


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else None
    async with SessionLocal() as db:
        clusters = list((await db.execute(select(Cluster))).scalars().all())
    if target:
        clusters = [c for c in clusters if c.display_name == target]
        if not clusters:
            print(f"No cluster registered with display name {target!r}")
            return
    for cluster in clusters:
        try:
            await _diagnose_cluster(cluster)
        except Exception as e:
            print(f"=== {cluster.display_name}: FAILED ({e!r}) ===\n")


if __name__ == "__main__":
    asyncio.run(main())
