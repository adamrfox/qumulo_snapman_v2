"""Inspect router: groups overview, start/stream inspect jobs, delete snapshots."""

import asyncio
import json
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
from app.models import Cluster, InspectJob
from app.routers.clusters import decrypt_token, get_authorized_cluster

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers — run sync Qumulo/cache work off the event loop
# ---------------------------------------------------------------------------


def _make_qclient(cluster: Cluster):
    from app.qumulo.client import QumuloClient

    token = decrypt_token(cluster.token_encrypted)
    return QumuloClient(cluster.host, cluster.port, token, insecure=cluster.insecure)


def _open_cache():
    from app.qumulo.cache import Cache

    return Cache(Path(settings.cache_path))


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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = api.get_cluster_name(qclient)
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

    cluster_name, groups = await asyncio.get_event_loop().run_in_executor(None, _worker)
    return {"cluster_name": cluster_name, "groups": groups}


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
        from app.qumulo.compute.curve import CurveModel, reclaim_rows
        from app.qumulo.compute.groups import group_snapshots

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = api.get_cluster_name(qclient)
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
            curve = CurveModel(now)
            cumulative = 0
            known = True
            for older, newer in zip(snaps_sorted[:-1], snaps_sorted[1:]):
                pair_data = pairs.get((older.id, newer.id))
                if pair_data is None:
                    known = False
                    curve.add(older, newer, None, None, None, cached=False, pending=True)
                else:
                    freed, files = pair_data
                    if known:
                        cumulative += freed
                    curve.add(
                        older,
                        newer,
                        freed,
                        cumulative if known else None,
                        files,
                        cached=True,
                        pending=False,
                    )

            rows, unmeasured = reclaim_rows(curve.points)
            return {
                "cluster_name": cluster_name,
                "source_file_id": source_file_id,
                "points": curve.points,
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

    result = await asyncio.get_event_loop().run_in_executor(None, _worker)
    if result is None:
        raise HTTPException(404, "Source not found in snapshot listing")
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = api.get_cluster_name(qclient)
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

    ids = await asyncio.get_event_loop().run_in_executor(None, _worker)
    return {"snapshot_ids": ids, "count": len(ids)}


# ---------------------------------------------------------------------------
# POST /{cluster_id}/inspect  — start an inspect job
# ---------------------------------------------------------------------------


class InspectRequest(BaseModel):
    source_file_id: str
    path: str


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

        qclient = _make_qclient(cluster)
        return api.get_cluster_name(qclient)

    cluster_name = await asyncio.get_event_loop().run_in_executor(None, _get_cluster_name)

    db_job = InspectJob(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=req.source_file_id,
        path=req.path,
        started_by=user.id,
        status="running",
    )
    db.add(db_job)
    await db.commit()
    await db.refresh(db_job)

    job = job_registry.create(
        job_id=db_job.id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=req.source_file_id,
        path=req.path,
        started_by=user.id,
    )

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }
    task = asyncio.create_task(
        _run_inspect_task(job, cluster_snapshot, req.source_file_id, req.path)
    )
    job.task = task

    return {"job_id": job.id, "reused": False}


async def _run_inspect_task(
    job: job_registry.InspectJob,
    cluster_snapshot: dict,
    source_file_id: str,
    path: str,
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
            cluster_name = api.get_cluster_name(qclient)
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
            )
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
        "started_at": db_job.started_at.isoformat(),
        "finished_at": db_job.finished_at.isoformat() if db_job.finished_at else None,
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

        qclient = _make_qclient(cluster)
        deleted = []
        errors = []
        for snap_id in req.snapshot_ids:
            try:
                api.delete_snapshot(qclient, snap_id)
                deleted.append(snap_id)
            except Exception as e:
                errors.append({"id": snap_id, "error": str(e)})
        return deleted, errors

    deleted, errors = await asyncio.get_event_loop().run_in_executor(None, _worker)
    return {"deleted": deleted, "errors": errors}
