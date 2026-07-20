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


def _make_qclient(cluster: Cluster):
    from app.qumulo.client import QumuloClient

    token = decrypt_token(cluster.token_encrypted)
    return QumuloClient(cluster.host, cluster.port, token, insecure=cluster.insecure)


def _open_cache():
    from app.qumulo.cache import Cache

    return Cache(Path(settings.cache_path))


def _checked_cluster_name(qclient) -> str:
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = _checked_cluster_name(qclient)
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = _checked_cluster_name(qclient)
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
            cluster_name = _checked_cluster_name(qclient)
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

    result = await _run_qumulo_worker(_worker)
    if result is None:
        raise HTTPException(404, "Source not found in snapshot listing")
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = _checked_cluster_name(qclient)
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
            "finished_at": last_job.finished_at.isoformat() if last_job.finished_at else None,
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = _checked_cluster_name(qclient)
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

        qclient = _make_qclient(cluster)
        return _checked_cluster_name(qclient)

    cluster_name = await _run_qumulo_worker(_get_cluster_name)

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
        _run_inspect_task(job, cluster_snapshot, req.source_file_id, req.path, req.include_held)
    )
    job.task = task

    return {"job_id": job.id, "reused": False}


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
            cluster_name = _checked_cluster_name(qclient)
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

        qclient = _make_qclient(cluster)
        return _checked_cluster_name(qclient)

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
            cluster_name = _checked_cluster_name(qclient)
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

        qclient = _make_qclient(cluster)
        cache = _open_cache()
        try:
            cluster_name = _checked_cluster_name(qclient)
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

        if deleted:
            # The cached snapshot listing is now stale (up to 5 minutes, per
            # snapshot_listing_ttl) -- we know for a fact it changed, so
            # refresh it immediately rather than leaving ghost ids around
            # for every subsequent Inspect/Size-snapshots/Estimate call.
            cache = _open_cache()
            try:
                cluster_name = _checked_cluster_name(qclient)
                snaps = api.list_snapshots(qclient)
                cache.put_listing(cluster_name, [asdict(s) for s in snaps])
            finally:
                cache.close()

        return deleted, errors

    deleted, errors = await asyncio.get_event_loop().run_in_executor(None, _worker)
    return {"deleted": deleted, "errors": errors}
