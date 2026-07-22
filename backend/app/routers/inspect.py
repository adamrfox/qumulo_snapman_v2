"""Inspect router: groups overview, start/stream inspect jobs, delete snapshots."""

import asyncio
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs as job_registry
from app.auth import CurrentUser, RequireOperator
from app.config import settings
from app.database import SessionLocal, get_db
from app.models import Cluster, InspectJob, WarmTree, to_utc_iso
from app.qumulo.api import UnsupportedVersionError
from app.qumulo.client import ApiError
from app.routers.clusters import decrypt_token, get_authorized_cluster

router = APIRouter()

# Distinct from the app's own session-expiry 401 (see api.ts's global handler) so the
# frontend can tell "your snapman login expired" apart from "this cluster's stored
# Qumulo token expired" and prompt for fresh cluster credentials instead of logging
# the user out of the app.
CLUSTER_AUTH_ERROR_STATUS = 424
CLUSTER_AUTH_ERROR_MESSAGE = (
    "This cluster's stored credentials have expired or are no longer valid. "
    "Update the cluster's credentials, then retry."
)

# Distinct from both of the above: the cluster is reachable and the credentials
# are valid, but the Qumulo Core release itself predates the APIs/fields this
# tool depends on (see UnsupportedVersionError) -- no amount of re-authenticating
# fixes this, so the frontend shows it as a plain, non-actionable notice.
UNSUPPORTED_VERSION_STATUS = 426


# ---------------------------------------------------------------------------
# Helpers — run sync Qumulo/cache work off the event loop
# ---------------------------------------------------------------------------


def make_qclient(cluster: Cluster):
    from app.qumulo.client import QumuloClient

    token = decrypt_token(cluster.token_encrypted)
    return QumuloClient(cluster.host, cluster.port, token, insecure=cluster.insecure)


def _open_cache():
    from app.qumulo.cache import Cache

    return Cache(Path(settings.cache_path))


def checked_cluster_name(qclient) -> str:
    """api.get_cluster_name, but gated on the cluster meeting MIN_CORE_VERSION
    first -- this is the first real API call every worker makes, so it's the
    natural place to fail fast with a clear reason instead of hitting a raw
    KeyError deep in response parsing later (e.g. a missing logical_datablocks
    field on file attributes, which older Qumulo Core releases don't return)."""
    from app.qumulo import api

    api.check_min_version(qclient)
    return api.get_cluster_name(qclient)


async def _run_qumulo_worker(worker):
    """Run a sync Qumulo-calling worker in the executor, translating an expired/
    invalid cluster token -- or an unsupported Qumulo Core version -- into a
    clean, distinguishable error instead of a 500."""
    try:
        return await asyncio.get_event_loop().run_in_executor(None, worker)
    except UnsupportedVersionError as e:
        raise HTTPException(UNSUPPORTED_VERSION_STATUS, str(e)) from e
    except ApiError as e:
        if e.status_code == 401:
            raise HTTPException(
                CLUSTER_AUTH_ERROR_STATUS,
                "This cluster's stored credentials have expired or are no longer valid. "
                "Update the cluster's credentials to continue.",
            ) from e
        raise HTTPException(502, f"Qumulo API error: {e}") from e


# ---------------------------------------------------------------------------
# POST /{cluster_id}/refresh — bypass the snapshot-listing TTL and re-fetch now
# ---------------------------------------------------------------------------


@router.post("/{cluster_id}/refresh")
async def refresh_snapshots(
    cluster_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _worker():
        from app.qumulo import api

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            snaps = api.list_snapshots(qclient)
            cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            return cluster_name, len(snaps)
        finally:
            cache.close()

    cluster_name, count = await _run_qumulo_worker(_worker)
    return {"cluster_name": cluster_name, "snapshot_count": count}


# ---------------------------------------------------------------------------
# GET /{cluster_id}/groups
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/groups")
async def get_groups(
    cluster_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    older_than_days: float = 90.0,
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _worker():
        from app.qumulo import api, paths
        from app.qumulo.compute.groups import (
            filter_groups,
            group_snapshots,
            overlapped_sources,
            prune_prefix,
        )

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                raw = [asdict(s) for s in snaps]
                cache.put_listing(cluster_name, raw)
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            now = datetime.now(timezone.utc)
            groups = group_snapshots(snaps, now)

            def path_of(g):
                return paths.resolve_source_path(
                    qclient, cache, cluster_name, g.source_file_id, g.snapshots[-1].id
                )

            overlapped = overlapped_sources(groups, path_of)
            result = []
            for g in groups:
                path = path_of(g)
                prefix = prune_prefix(g, now, older_than_days)
                reclaim_bytes, measured = cache.get_reclaim_prefix(
                    cluster_name, g.source_file_id, prefix.pair_ids
                )
                result.append(
                    {
                        "source_file_id": g.source_file_id,
                        "path": path,
                        "count": g.count,
                        "max_age_days": g.max_age_days,
                        "min_age_days": g.min_age_days,
                        "prunable": prefix.prunable,
                        "measured_pairs": measured,
                        "total_pairs": max(0, g.count - 1),
                        "reclaim_bytes": reclaim_bytes,
                        "is_upper_bound": g.source_file_id in overlapped,
                        "held_reason": prefix.held.held_reason if prefix.held else None,
                    }
                )
            return cluster_name, result
        finally:
            cache.close()

    cluster_name, groups = await _run_qumulo_worker(_worker)
    return {"cluster_name": cluster_name, "groups": groups}


# ---------------------------------------------------------------------------
# GET/PUT/DELETE /{cluster_id}/warm-trees[/{source_file_id}] -- opt a tree in
# or out of the background warm-inspect sweep (see app/warm_sweep.py)
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/warm-trees")
async def list_warm_trees(
    cluster_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    result = await db.execute(select(WarmTree).where(WarmTree.cluster_id == cluster_id))
    return {"source_file_ids": [w.source_file_id for w in result.scalars().all()]}


@router.put("/{cluster_id}/warm-trees/{source_file_id}")
async def add_warm_tree(
    cluster_id: str,
    source_file_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    existing = await db.execute(
        select(WarmTree).where(
            WarmTree.cluster_id == cluster_id, WarmTree.source_file_id == source_file_id
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(WarmTree(cluster_id=cluster_id, source_file_id=source_file_id, created_by=user.id))
        await db.commit()
    return {"ok": True}


@router.delete("/{cluster_id}/warm-trees/{source_file_id}")
async def remove_warm_tree(
    cluster_id: str,
    source_file_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    existing = await db.execute(
        select(WarmTree).where(
            WarmTree.cluster_id == cluster_id, WarmTree.source_file_id == source_file_id
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /{cluster_id}/groups/{source_file_id}/curve  (cached curve, no new work)
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/groups/{source_file_id}/curve")
async def get_curve(
    cluster_id: str,
    source_file_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _worker():
        from app.qumulo import api
        from app.qumulo.compute.curve import build_points, reclaim_rows
        from app.qumulo.compute.groups import group_snapshots

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            now = datetime.now(timezone.utc)
            groups = group_snapshots(snaps, now)
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                return None

            pairs = cache.get_pairs(cluster_name, source_file_id)
            snaps_sorted = sorted(group.snapshots, key=lambda s: s.id)
            points, _ = build_points(snaps_sorted, pairs, now)

            # reclaim_rows' own unmeasured count (points from the first gap
            # onward, conservative for bucketing) is what the frontend's
            # "still measuring" progress messaging keys off -- keep using it
            # here rather than build_points' plain total-pending count, which
            # the goal orchestrator uses instead (it only cares whether that
            # count is exactly zero, where the two formulas always agree).
            rows, unmeasured = reclaim_rows(points)
            return {
                "cluster_name": cluster_name,
                "source_file_id": source_file_id,
                "points": points,
                "rows": [
                    {
                        "keep_days": r[0],
                        "delete_before": r[1],
                        "delete_count": r[2],
                        "reclaim_bytes": r[3],
                    }
                    for r in rows
                ],
                "unmeasured_pairs": unmeasured,
            }
        finally:
            cache.close()

    last_job_result = await db.execute(
        select(InspectJob)
        .where(
            InspectJob.cluster_id == cluster_id,
            InspectJob.source_file_id == source_file_id,
            InspectJob.job_type == "inspect",
        )
        .order_by(InspectJob.started_at.desc())
        .limit(1)
    )
    last_job = last_job_result.scalar_one_or_none()
    last_run = (
        {
            "status": last_job.status,
            "error_message": last_job.error_message,
            "finished_at": to_utc_iso(last_job.finished_at) if last_job.finished_at else None,
        }
        if last_job is not None
        else None
    )

    result = await _run_qumulo_worker(_worker)
    if result is None:
        raise HTTPException(404, "Source not found in snapshot listing")
    result["last_run"] = last_run
    return result


# ---------------------------------------------------------------------------
# GET /{cluster_id}/groups/{source_file_id}/snapshots  (cached, no new work)
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/groups/{source_file_id}/snapshots")
async def get_snapshot_sizes(
    cluster_id: str,
    source_file_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _worker():
        from app.qumulo import api
        from app.qumulo.compute.groups import age_days, group_snapshots

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            now = datetime.now(timezone.utc)
            groups = group_snapshots(snaps, now)
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                return None

            snaps_sorted = sorted(group.snapshots, key=lambda s: s.id)
            n = len(snaps_sorted)
            pairs = cache.get_pairs(cluster_name, source_file_id)
            triples = cache.get_triples(cluster_name, source_file_id)
            pair_partials = cache.get_partials(cluster_name, source_file_id)
            triple_partials = cache.get_triple_partials(cluster_name, source_file_id)

            rows = []
            for i, snap in enumerate(snaps_sorted):
                exclusive_bytes = total_files = None
                if n == 1 or i == n - 1:
                    status = "not_sizable"
                elif i == 0:
                    key = (snaps_sorted[0].id, snaps_sorted[1].id)
                    pair = pairs.get(key)
                    if pair:
                        status = "computed"
                        exclusive_bytes, total_files = pair
                    elif key in pair_partials:
                        status = "partial"
                    elif snap.held:
                        status = "skipped_held"
                    else:
                        status = "unmeasured"
                else:
                    key3 = (snaps_sorted[i - 1].id, snap.id, snaps_sorted[i + 1].id)
                    triple = triples.get(key3)
                    if triple:
                        status = "computed"
                        exclusive_bytes, total_files = triple
                    elif key3 in triple_partials:
                        status = "partial"
                    elif snap.held:
                        status = "skipped_held"
                    else:
                        status = "unmeasured"
                rows.append(
                    {
                        "id": snap.id,
                        "name": snap.name,
                        "date": snap.timestamp[:10],
                        "age_days": age_days(snap.timestamp, now),
                        "exclusive_bytes": exclusive_bytes,
                        "total_files": total_files,
                        "status": status,
                        "held": snap.held,
                        "held_reason": snap.held_reason if snap.held else None,
                    }
                )
            return {"cluster_name": cluster_name, "source_file_id": source_file_id, "snapshots": rows}
        finally:
            cache.close()

    last_job_result = await db.execute(
        select(InspectJob)
        .where(
            InspectJob.cluster_id == cluster_id,
            InspectJob.source_file_id == source_file_id,
            InspectJob.job_type == "snapshot_exclusive",
        )
        .order_by(InspectJob.started_at.desc())
        .limit(1)
    )
    last_job = last_job_result.scalar_one_or_none()
    last_run = (
        {
            "status": last_job.status,
            "error_message": last_job.error_message,
            "finished_at": to_utc_iso(last_job.finished_at) if last_job.finished_at else None,
        }
        if last_job is not None
        else None
    )

    result = await _run_qumulo_worker(_worker)
    if result is None:
        raise HTTPException(404, "Source not found in snapshot listing")
    result["last_run"] = last_run
    return result


# ---------------------------------------------------------------------------
# GET /{cluster_id}/older-than
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/older-than")
async def older_than(
    cluster_id: str,
    source_file_id: str,
    before: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _worker():
        from app.qumulo import api
        from app.qumulo.compute.groups import group_snapshots

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            snaps_raw = cached or [asdict(s) for s in api.list_snapshots(qclient)]
            if cached is None:
                cache.put_listing(cluster_name, snaps_raw)
            snaps = [api.Snapshot.from_json(d) for d in snaps_raw]

            now = datetime.now(timezone.utc)
            groups = group_snapshots(snaps, now)
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                return []

            ids = []
            for snap in sorted(group.snapshots, key=lambda s: s.id):
                snap_date = snap.timestamp[:10]
                if snap_date < before:
                    ids.append(snap.id)
            return ids
        finally:
            cache.close()

    ids = await _run_qumulo_worker(_worker)
    return {"snapshot_ids": ids, "count": len(ids)}


# ---------------------------------------------------------------------------
# POST /{cluster_id}/inspect  — start an inspect job
# ---------------------------------------------------------------------------


class InspectRequest(BaseModel):
    source_file_id: str
    path: str
    include_held: bool = False


@router.post("/{cluster_id}/inspect", status_code=202)
async def start_inspect(
    cluster_id: str,
    req: InspectRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    existing = job_registry.find_running(cluster_id, req.source_file_id)
    if existing is not None:
        return {"job_id": existing.id, "reused": True}

    def _get_cluster_name():
        from app.qumulo import api

        qclient = make_qclient(cluster)
        return checked_cluster_name(qclient)

    cluster_name = await _run_qumulo_worker(_get_cluster_name)

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }
    job = await launch_inspect_job(
        db,
        cluster_id,
        cluster_name,
        cluster_snapshot,
        req.source_file_id,
        req.path,
        user.id,
        req.include_held,
    )

    return {"job_id": job.id, "reused": False}


async def launch_inspect_job(
    db: AsyncSession,
    cluster_id: str,
    cluster_name: str,
    cluster_snapshot: dict,
    source_file_id: str,
    path: str,
    started_by: str,
    include_held: bool = False,
) -> job_registry.InspectJob:
    """Create the durable inspect_jobs row + in-memory registry entry and
    launch _run_inspect_task -- the same sequence start_inspect uses, shared
    with the goal orchestrator so a tree it auto-inspects is a first-class,
    independently-visible Inspect job like any other (found by find_running,
    watchable from that tree's own detail page, etc.)."""
    db_job = InspectJob(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=path,
        started_by=started_by,
        status="running",
    )
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)

    job = job_registry.create(
        job_id=db_job.id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=path,
        started_by=started_by,
    )
    job.task = asyncio.create_task(
        _run_inspect_task(job, cluster_snapshot, source_file_id, path, include_held)
    )
    return job


def load_tree_status(cluster_snapshot: dict, cluster_name: str, source_file_id: str) -> dict | None:
    """Cache-only aside from a possible listing refresh / one-time path
    lookup (same cost profile as GET /groups): this tree's path, its current
    points + unmeasured-pair count, and a cheap prunable count used only to
    decide run order. Shared by the goal orchestrator and the warm-tree
    background sweep -- both need the exact same "is this tree already fully
    measured, and if not why" check."""
    from app.qumulo import api, paths
    from app.qumulo.client import QumuloClient
    from app.qumulo.compute.curve import build_points
    from app.qumulo.compute.groups import group_snapshots, prune_prefix

    token = decrypt_token(cluster_snapshot["token_encrypted"])
    qclient = QumuloClient(
        cluster_snapshot["host"],
        cluster_snapshot["port"],
        token,
        insecure=cluster_snapshot["insecure"],
    )
    cache = _open_cache()
    try:
        cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
        if cached is None:
            snaps = api.list_snapshots(qclient)
            cache.put_listing(cluster_name, [asdict(s) for s in snaps])
        else:
            snaps = [api.Snapshot.from_json(d) for d in cached]
        now = datetime.now(timezone.utc)
        group = next(
            (g for g in group_snapshots(snaps, now) if g.source_file_id == source_file_id), None
        )
        if group is None:
            return None
        path = paths.resolve_source_path(
            qclient, cache, cluster_name, source_file_id, group.snapshots[-1].id
        )
        pairs = cache.get_pairs(cluster_name, source_file_id)
        snaps_sorted = sorted(group.snapshots, key=lambda s: s.id)
        points, unmeasured = build_points(snaps_sorted, pairs, now)
        prunable = prune_prefix(group, now, 0).prunable

        # If an unmeasured pair's older snapshot is locked/replication-held,
        # that's *why* -- Inspect skips held snapshots by default (their
        # size isn't actionable since they can't be deleted anyway), and
        # that skip is never cached, so this tree can never reach 0
        # unmeasured through an ordinary auto-inspect no matter how many
        # times it's retried. Surface that instead of a generic "still
        # incomplete" message so it's clear this isn't transient.
        held_reason = None
        if unmeasured > 0:
            snaps_by_id = {s.id: s for s in group.snapshots}
            for p in points:
                if p["status"] in ("pending", "timed_out"):
                    snap = snaps_by_id.get(p["older_id"])
                    if snap is not None and snap.held:
                        held_reason = snap.held_reason
                        break

        return {
            "path": path,
            "points": points,
            "unmeasured": unmeasured,
            "prunable": prunable,
            "held_reason": held_reason,
        }
    finally:
        cache.close()


def is_cluster_wide_fatal(e: Exception) -> bool:
    # These mean every remaining tree would fail the exact same way, so
    # there's no point treating them as one tree's problem -- an expired
    # token or an unsupported Core version aborts the whole run, same as
    # a single-tree Inspect would already report it.
    if isinstance(e, UnsupportedVersionError):
        return True
    if isinstance(e, ApiError) and e.status_code in (401, 403):
        return True
    return False


async def _run_inspect_task(
    job: job_registry.InspectJob,
    cluster_snapshot: dict,
    source_file_id: str,
    path: str,
    include_held: bool = False,
) -> None:
    loop = asyncio.get_event_loop()
    error_message: str | None = None

    def push(event_type: str, data: dict) -> None:
        loop.call_soon_threadsafe(job.event_queue.put_nowait, {"type": event_type, **data})

    def _worker():
        nonlocal error_message
        from app.qumulo import api
        from app.qumulo.client import QumuloClient
        from app.qumulo.compute.groups import group_snapshots
        from app.qumulo.compute.inspect import WebObserver, run_inspect

        token = decrypt_token(cluster_snapshot["token_encrypted"])
        qclient = QumuloClient(
            cluster_snapshot["host"],
            cluster_snapshot["port"],
            token,
            insecure=cluster_snapshot["insecure"],
        )
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            groups = group_snapshots(snaps, datetime.now(timezone.utc))
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                error_message = f"Source {source_file_id} not found"
                push("error", {"message": error_message})
                return

            observer = WebObserver(push)
            run_inspect(
                qclient,
                cache,
                cluster_name,
                group.snapshots,
                limit=settings.pair_batch_size,
                max_workers=settings.file_workers,
                observer=observer,
                should_stop=lambda: job.done,
                pair_workers=settings.pair_workers,
                include_held=include_held,
            )
        except UnsupportedVersionError as e:
            error_message = str(e)
            push("error", {"message": error_message})
        except ApiError as e:
            error_message = CLUSTER_AUTH_ERROR_MESSAGE if e.status_code == 401 else str(e)
            push("error", {"message": error_message})
        except Exception as e:
            error_message = str(e)
            push("error", {"message": error_message})
        finally:
            cache.close()

    try:
        await loop.run_in_executor(None, _worker)
    finally:
        cancelled = job.cancel_requested
        job.done = True
        async with SessionLocal() as db:
            result = await db.execute(
                select(InspectJob).where(InspectJob.id == job.id)
            )
            db_job = result.scalar_one_or_none()
            if db_job is not None:
                if cancelled:
                    db_job.status = "cancelled"
                elif error_message is not None:
                    db_job.status = "error"
                    db_job.error_message = error_message
                else:
                    db_job.status = "completed"
                db_job.finished_at = datetime.utcnow()
                await db.commit()


# ---------------------------------------------------------------------------
# POST /{cluster_id}/goal — cluster-wide space-recovery goal solver
# ---------------------------------------------------------------------------


class GoalRequest(BaseModel):
    source_file_ids: list[str]
    target_bytes: int


@router.post("/{cluster_id}/goal", status_code=202)
async def start_goal(
    cluster_id: str,
    req: GoalRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    if not req.source_file_ids:
        raise HTTPException(400, "source_file_ids must not be empty")

    def _get_cluster_name():
        qclient = make_qclient(cluster)
        return checked_cluster_name(qclient)

    cluster_name = await _run_qumulo_worker(_get_cluster_name)

    # Ephemeral, in-memory-only job -- unlike a regular Inspect job this one
    # spans many trees, which doesn't fit the inspect_jobs table's one-job-
    # one-tree schema (source_file_id is NOT NULL there). Nothing expensive is
    # lost if the process restarts mid-run: any real Inspect work this job
    # triggers along the way gets its own durable row via launch_inspect_job,
    # so the user just re-solves and picks up the cached results for free.
    job = job_registry.create(
        job_id=str(uuid.uuid4()),
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id="*",
        path="*",
        started_by=user.id,
        job_type="goal",
    )

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }
    job.task = asyncio.create_task(
        _run_goal_task(
            job,
            cluster_snapshot,
            cluster_id,
            cluster_name,
            req.source_file_ids,
            req.target_bytes,
            user.id,
        )
    )
    return {"job_id": job.id}


async def _run_goal_task(
    job: job_registry.InspectJob,
    cluster_snapshot: dict,
    cluster_id: str,
    cluster_name: str,
    source_file_ids: list[str],
    target_bytes: int,
    started_by: str,
) -> None:
    from app.qumulo.compute.goal import TreeInput, allocate

    loop = asyncio.get_event_loop()
    error_message: str | None = None

    def push(event_type: str, data: dict) -> None:
        loop.call_soon_threadsafe(job.event_queue.put_nowait, {"type": event_type, **data})

    skipped: list[dict] = []

    try:
        # A network blip while loading one tree shouldn't cost every other
        # tree's already-computed result -- isolate per tree and skip just
        # the one that failed, same as an unmeasurable tree is skipped below.
        loaded: dict[str, dict] = {}
        for sfid in source_file_ids:
            try:
                info = await loop.run_in_executor(None, load_tree_status, cluster_snapshot, cluster_name, sfid)
            except Exception as e:
                if is_cluster_wide_fatal(e):
                    raise
                reason = str(e)
                push("tree_skipped", {"source_file_id": sfid, "reason": reason})
                skipped.append({"source_file_id": sfid, "reason": reason})
                continue
            if info is not None:
                loaded[sfid] = info

        # Order is UX-only (biggest wins surface first, the heaviest scans
        # happen first so a cancel loses the least) -- it never affects the
        # final allocation, which only runs once every tree is measured.
        ordered = sorted(loaded, key=lambda sfid: loaded[sfid]["prunable"], reverse=True)
        total = len(ordered)
        tree_inputs: list[TreeInput] = []

        for index, sfid in enumerate(ordered):
            if job.done:
                break
            info = loaded[sfid]
            push(
                "tree_start",
                {"source_file_id": sfid, "path": info["path"], "index": index, "total": total},
            )

            try:
                if info["unmeasured"] > 0:
                    # Trees run strictly one at a time -- this is the one part of
                    # this feature that does new, potentially heavy live Qumulo
                    # work, and nothing else in this app runs more than one such
                    # scan concurrently.
                    existing = job_registry.find_running(cluster_id, sfid)
                    if existing is not None:
                        # Don't drain another consumer's queue -- another tab may
                        # already be watching this Inspect run.
                        while not existing.done:
                            push("inspect_progress", {"source_file_id": sfid, "waiting": True})
                            await asyncio.sleep(2)
                            if job.done:
                                break
                    else:
                        async with SessionLocal() as sub_db:
                            sub_job = await launch_inspect_job(
                                sub_db,
                                cluster_id,
                                cluster_name,
                                cluster_snapshot,
                                sfid,
                                info["path"],
                                started_by,
                            )
                        while True:
                            if job.done:
                                # Cancelling the goal run aborts whatever sub-
                                # inspect is in flight too. Both flags matter:
                                # .done actually stops run_inspect (should_stop),
                                # .cancel_requested is what its own finally block
                                # reads to record "cancelled" instead of
                                # "completed" in the durable inspect_jobs row.
                                sub_job.cancel_requested = True
                                sub_job.done = True
                            try:
                                event = await asyncio.wait_for(
                                    sub_job.event_queue.get(), timeout=1.5
                                )
                                # Nested, not spread -- the sub-event has its own
                                # "type" key (pair_start/progress/finish/...) that
                                # would otherwise collide with and overwrite this
                                # wrapper event's own "type": "inspect_progress".
                                push("inspect_progress", {"source_file_id": sfid, "event": event})
                                if event.get("type") in ("finish", "error"):
                                    break
                            except asyncio.TimeoutError:
                                pass
                            if sub_job.done and sub_job.event_queue.empty():
                                break

                    info = await loop.run_in_executor(None, load_tree_status, cluster_snapshot, cluster_name, sfid)
            except Exception as e:
                if is_cluster_wide_fatal(e):
                    raise
                reason = str(e)
                push("tree_skipped", {"source_file_id": sfid, "reason": reason})
                skipped.append({"source_file_id": sfid, "reason": reason})
                continue

            if info is None or info["unmeasured"] > 0:
                held_reason = info.get("held_reason") if info else None
                if held_reason:
                    reason = (
                        f'Contains a {held_reason} snapshot, which Inspect skips by default '
                        f'since it can\'t actually be deleted. Re-inspect this tree directly '
                        f'with "Include locked/replication-held snapshots" checked to have it '
                        f'counted here.'
                    )
                else:
                    reason = "Inspect did not finish measuring every pair -- see the backend log for details."
                push("tree_skipped", {"source_file_id": sfid, "reason": reason})
                skipped.append({"source_file_id": sfid, "reason": reason})
                continue

            push("tree_measured", {"source_file_id": sfid})
            tree_inputs.append(TreeInput(sfid, info["points"]))

        result = allocate(target_bytes, tree_inputs)
        push("finish", {"result": asdict(result), "skipped": skipped})
    except UnsupportedVersionError as e:
        error_message = str(e)
        push("error", {"message": error_message})
    except ApiError as e:
        error_message = CLUSTER_AUTH_ERROR_MESSAGE if e.status_code == 401 else str(e)
        push("error", {"message": error_message})
    except Exception as e:
        error_message = str(e)
        push("error", {"message": error_message})
    finally:
        job.done = True


# ---------------------------------------------------------------------------
# POST /{cluster_id}/groups/{source_file_id}/size-snapshots — start a
# per-snapshot exclusive-size job
# ---------------------------------------------------------------------------


class SizeSnapshotsRequest(BaseModel):
    path: str
    include_held: bool = False


@router.post("/{cluster_id}/groups/{source_file_id}/size-snapshots", status_code=202)
async def start_size_snapshots(
    cluster_id: str,
    source_file_id: str,
    req: SizeSnapshotsRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    existing = job_registry.find_running(cluster_id, source_file_id, job_type="snapshot_exclusive")
    if existing is not None:
        return {"job_id": existing.id, "reused": True}

    def _get_cluster_name():
        from app.qumulo import api

        qclient = make_qclient(cluster)
        return checked_cluster_name(qclient)

    cluster_name = await _run_qumulo_worker(_get_cluster_name)

    db_job = InspectJob(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=req.path,
        started_by=user.id,
        job_type="snapshot_exclusive",
        status="running",
    )
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)

    job = job_registry.create(
        job_id=db_job.id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=req.path,
        started_by=user.id,
        job_type="snapshot_exclusive",
    )

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }
    task = asyncio.create_task(
        _run_size_snapshots_task(job, cluster_snapshot, source_file_id, req.include_held)
    )
    job.task = task

    return {"job_id": job.id, "reused": False}


async def _run_size_snapshots_task(
    job: job_registry.InspectJob,
    cluster_snapshot: dict,
    source_file_id: str,
    include_held: bool = False,
) -> None:
    loop = asyncio.get_event_loop()
    error_message: str | None = None

    def push(event_type: str, data: dict) -> None:
        loop.call_soon_threadsafe(job.event_queue.put_nowait, {"type": event_type, **data})

    def _worker():
        nonlocal error_message
        from app.qumulo import api
        from app.qumulo.client import QumuloClient
        from app.qumulo.compute.groups import group_snapshots
        from app.qumulo.compute.snapshot_exclusive_job import WebTripleObserver, run_snapshot_exclusive

        token = decrypt_token(cluster_snapshot["token_encrypted"])
        qclient = QumuloClient(
            cluster_snapshot["host"],
            cluster_snapshot["port"],
            token,
            insecure=cluster_snapshot["insecure"],
        )
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            groups = group_snapshots(snaps, datetime.now(timezone.utc))
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                error_message = f"Source {source_file_id} not found"
                push("error", {"message": error_message})
                return

            observer = WebTripleObserver(push)
            run_snapshot_exclusive(
                qclient,
                cache,
                cluster_name,
                group.snapshots,
                limit=settings.pair_batch_size,
                max_workers=settings.file_workers,
                observer=observer,
                should_stop=lambda: job.done,
                triple_workers=settings.pair_workers,
                include_held=include_held,
            )
        except UnsupportedVersionError as e:
            error_message = str(e)
            push("error", {"message": error_message})
        except ApiError as e:
            error_message = CLUSTER_AUTH_ERROR_MESSAGE if e.status_code == 401 else str(e)
            push("error", {"message": error_message})
        except Exception as e:
            error_message = str(e)
            push("error", {"message": error_message})
        finally:
            cache.close()

    try:
        await loop.run_in_executor(None, _worker)
    finally:
        cancelled = job.cancel_requested
        job.done = True
        async with SessionLocal() as db:
            result = await db.execute(
                select(InspectJob).where(InspectJob.id == job.id)
            )
            db_job = result.scalar_one_or_none()
            if db_job is not None:
                if cancelled:
                    db_job.status = "cancelled"
                elif error_message is not None:
                    db_job.status = "error"
                    db_job.error_message = error_message
                else:
                    db_job.status = "completed"
                db_job.finished_at = datetime.utcnow()
                await db.commit()


# ---------------------------------------------------------------------------
# POST /{cluster_id}/groups/{source_file_id}/estimate-deletion — combined
# space-freed estimate for an arbitrary selected set of snapshots
# ---------------------------------------------------------------------------


class EstimateDeletionRequest(BaseModel):
    snapshot_ids: list[int]


@router.post("/{cluster_id}/groups/{source_file_id}/estimate-deletion", status_code=202)
async def start_estimate_deletion(
    cluster_id: str,
    source_file_id: str,
    req: EstimateDeletionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)

    def _prepare():
        from app.qumulo import api
        from app.qumulo.compute.deletion_estimate import (
            SelectionError,
            partition_into_runs,
            validate_selection,
        )
        from app.qumulo.compute.groups import group_snapshots

        qclient = make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = checked_cluster_name(qclient)
            cached = cache.get_listing(cluster_name, ttl_seconds=settings.snapshot_listing_ttl)
            if cached is None:
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            else:
                snaps = [api.Snapshot.from_json(d) for d in cached]

            groups = group_snapshots(snaps, datetime.now(timezone.utc))
            group = next((g for g in groups if g.source_file_id == source_file_id), None)
            if group is None:
                return None, None, f"Source {source_file_id} not found"

            snaps_sorted = sorted(group.snapshots, key=lambda s: s.id)
            selected = set(req.snapshot_ids)
            try:
                validate_selection(snaps_sorted, selected)
                runs = partition_into_runs(snaps_sorted, selected)
            except SelectionError as e:
                return None, None, str(e)
            return cluster_name, runs, None
        finally:
            cache.close()

    cluster_name, runs, error = await _run_qumulo_worker(_prepare)
    if error is not None:
        raise HTTPException(400, error)

    db_job = InspectJob(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=f"{len(req.snapshot_ids)} selected snapshots",
        started_by=user.id,
        job_type="deletion_estimate",
        status="running",
    )
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)

    job = job_registry.create(
        job_id=db_job.id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=db_job.path,
        started_by=user.id,
        job_type="deletion_estimate",
    )

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }
    task = asyncio.create_task(_run_estimate_deletion_task(job, cluster_snapshot, cluster_name, source_file_id, runs))
    job.task = task

    return {"job_id": job.id, "reused": False}


async def _run_estimate_deletion_task(
    job: job_registry.InspectJob,
    cluster_snapshot: dict,
    cluster_name: str,
    source_file_id: str,
    runs: list,
) -> None:
    loop = asyncio.get_event_loop()
    error_message: str | None = None

    def push(event_type: str, data: dict) -> None:
        loop.call_soon_threadsafe(job.event_queue.put_nowait, {"type": event_type, **data})

    def _worker():
        nonlocal error_message
        from app.qumulo.client import QumuloClient
        from app.qumulo.compute.deletion_estimate import WebDeletionEstimateObserver, run_deletion_estimate

        token = decrypt_token(cluster_snapshot["token_encrypted"])
        qclient = QumuloClient(
            cluster_snapshot["host"],
            cluster_snapshot["port"],
            token,
            insecure=cluster_snapshot["insecure"],
        )
        cache = _open_cache()
        try:
            observer = WebDeletionEstimateObserver(push)
            run_deletion_estimate(
                qclient,
                cache,
                cluster_name,
                source_file_id,
                runs,
                max_workers=settings.file_workers,
                observer=observer,
                should_stop=lambda: job.done,
            )
        except UnsupportedVersionError as e:
            error_message = str(e)
            push("error", {"message": error_message})
        except ApiError as e:
            error_message = CLUSTER_AUTH_ERROR_MESSAGE if e.status_code == 401 else str(e)
            push("error", {"message": error_message})
        except Exception as e:
            error_message = str(e)
            push("error", {"message": error_message})
        finally:
            cache.close()

    try:
        await loop.run_in_executor(None, _worker)
    finally:
        cancelled = job.cancel_requested
        job.done = True
        async with SessionLocal() as db:
            result = await db.execute(
                select(InspectJob).where(InspectJob.id == job.id)
            )
            db_job = result.scalar_one_or_none()
            if db_job is not None:
                if cancelled:
                    db_job.status = "cancelled"
                elif error_message is not None:
                    db_job.status = "error"
                    db_job.error_message = error_message
                else:
                    db_job.status = "completed"
                db_job.finished_at = datetime.utcnow()
                await db.commit()


# ---------------------------------------------------------------------------
# GET /{cluster_id}/jobs/{job_id}/stream  — SSE
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/jobs/{job_id}/stream")
async def stream_job(
    cluster_id: str,
    job_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    job = job_registry.get(job_id)
    if job is None or job.cluster_id != cluster_id:
        raise HTTPException(404, "Job not found")

    async def generate():
        while True:
            try:
                event = await asyncio.wait_for(job.event_queue.get(), timeout=25)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("finish", "error"):
                    break
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            if job.done and job.event_queue.empty():
                break

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# POST /{cluster_id}/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/{cluster_id}/jobs/{job_id}/cancel")
async def cancel_job(
    cluster_id: str,
    job_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    job = job_registry.get(job_id)
    if job is None or job.cluster_id != cluster_id:
        raise HTTPException(404, "Job not found")
    job.cancel_requested = True
    job.done = True
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /{cluster_id}/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{cluster_id}/jobs/{job_id}")
async def get_job(
    cluster_id: str,
    job_id: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    await get_authorized_cluster(cluster_id, user, db)
    result = await db.execute(
        select(InspectJob).where(InspectJob.id == job_id, InspectJob.cluster_id == cluster_id)
    )
    db_job = result.scalar_one_or_none()
    if db_job is None:
        raise HTTPException(404, "Job not found")
    return {
        "id": db_job.id,
        "path": db_job.path,
        "status": db_job.status,
        "started_at": to_utc_iso(db_job.started_at),
        "finished_at": to_utc_iso(db_job.finished_at) if db_job.finished_at else None,
        "error_message": db_job.error_message,
    }


# ---------------------------------------------------------------------------
# POST /{cluster_id}/snapshots/delete  — operator/admin only
# ---------------------------------------------------------------------------


class DeleteRequest(BaseModel):
    snapshot_ids: list[int]


@router.post("/{cluster_id}/snapshots/delete")
async def delete_snapshots(
    cluster_id: str,
    req: DeleteRequest,
    user: RequireOperator,
    db: AsyncSession = Depends(get_db),
):
    cluster = await get_authorized_cluster(cluster_id, user, db)
    if not req.snapshot_ids:
        raise HTTPException(400, "snapshot_ids must not be empty")

    def _worker():
        from app.qumulo import api

        qclient = make_qclient(cluster)
        deleted = []
        errors = []
        for snap_id in req.snapshot_ids:
            try:
                api.delete_snapshot(qclient, snap_id)
                deleted.append(snap_id)
            except Exception as e:
                errors.append({"id": snap_id, "error": str(e)})

        if deleted:
            # The cached snapshot listing is now stale (up to 5 minutes, per
            # snapshot_listing_ttl) -- we know for a fact it changed, so
            # refresh it immediately rather than leaving ghost ids around
            # for every subsequent Inspect/Size-snapshots/Estimate call.
            cache = _open_cache()
            try:
                cluster_name = checked_cluster_name(qclient)
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            finally:
                cache.close()

        return deleted, errors

    deleted, errors = await asyncio.get_event_loop().run_in_executor(None, _worker)
    return {"deleted": deleted, "errors": errors}
