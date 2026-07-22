"""Background sweep that keeps opted-in trees' reclaim curves fresh.

A tree is only ever auto-Inspected here if a user explicitly opted it in
(see the /{cluster_id}/warm-trees endpoints in routers/inspect.py) -- this
never scans a tree nobody asked to keep warm. One long-lived task runs per
cluster that currently has at least one opted-in tree, so a slow or
unreachable cluster never delays another cluster's sweep; a lightweight
supervisor spawns/retires those per-cluster tasks as opt-ins come and go.
The opt-in list itself (the `warm_trees` table) is the only durable state --
this module holds nothing that needs to survive a restart on its own, so
`start()` just picks back up wherever the table says it should.
"""

import asyncio
import sys

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.jobs import InspectJob as JobHandle
from app.jobs import find_running
from app.models import Cluster, WarmTree
from app.routers.inspect import (
    checked_cluster_name,
    is_cluster_wide_fatal,
    launch_inspect_job,
    load_tree_status,
    make_qclient,
)

_cluster_tasks: dict[str, asyncio.Task] = {}
_shutdown = asyncio.Event()
_supervisor_task: asyncio.Task | None = None
# In-flight jobs this sweep launched, keyed by job id (InspectJob isn't
# hashable) -- tracked only so stop() can propagate cancellation into them
# (see the goal solver's own cancellation-flag fix: both .cancel_requested
# and .done must be set together, or the underlying Inspect records
# "completed" instead of "cancelled" when interrupted).
_active_jobs: dict[str, JobHandle] = {}


async def start() -> None:
    global _supervisor_task
    _shutdown.clear()
    _supervisor_task = asyncio.create_task(_supervisor_loop())


async def stop() -> None:
    _shutdown.set()
    for job in list(_active_jobs.values()):
        job.cancel_requested = True
        job.done = True
    tasks = list(_cluster_tasks.values())
    if _supervisor_task is not None:
        tasks.append(_supervisor_task)
    if tasks:
        await asyncio.wait(tasks, timeout=15)


async def _supervisor_loop() -> None:
    while not _shutdown.is_set():
        try:
            async with SessionLocal() as db:
                result = await db.execute(select(WarmTree.cluster_id).distinct())
                cluster_ids = set(result.scalars().all())
        except Exception as e:
            print(f"[snapman] warm sweep: failed to list opted-in clusters ({e})", file=sys.stderr)
            cluster_ids = set()

        for cluster_id in cluster_ids:
            task = _cluster_tasks.get(cluster_id)
            if task is None or task.done():
                _cluster_tasks[cluster_id] = asyncio.create_task(_cluster_sweep_loop(cluster_id))

        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=settings.warm_sweep_poll_interval_seconds
            )
        except asyncio.TimeoutError:
            pass


async def _cluster_sweep_loop(cluster_id: str) -> None:
    while not _shutdown.is_set():
        async with SessionLocal() as db:
            result = await db.execute(select(WarmTree).where(WarmTree.cluster_id == cluster_id))
            warm_trees = list(result.scalars().all())

        if not warm_trees:
            return  # self-terminate; the supervisor respawns this if trees get opted back in

        await _sweep_cluster_once(cluster_id, warm_trees)

        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=settings.warm_sweep_interval_seconds
            )
        except asyncio.TimeoutError:
            pass


async def _sweep_cluster_once(cluster_id: str, warm_trees: list[WarmTree]) -> None:
    loop = asyncio.get_event_loop()

    async with SessionLocal() as db:
        cluster = await db.get(Cluster, cluster_id)
    if cluster is None:
        return  # cluster was deleted; its warm_trees rows cascade-delete separately

    cluster_snapshot = {
        "host": cluster.host,
        "port": cluster.port,
        "token_encrypted": cluster.token_encrypted,
        "insecure": cluster.insecure,
    }

    try:
        qclient = make_qclient(cluster)
        cluster_name = await loop.run_in_executor(None, checked_cluster_name, qclient)
    except Exception as e:
        print(
            f"[snapman] warm sweep: cluster {cluster_id} unreachable ({e}) -- "
            "will retry next pass",
            file=sys.stderr,
        )
        return

    for warm_tree in warm_trees:
        if _shutdown.is_set():
            return

        sfid = warm_tree.source_file_id

        # Someone else -- a live user, the goal solver, or a slow earlier
        # pass -- already has this tree. No deadline needs this tree's
        # result right now, so just move on instead of waiting.
        if find_running(cluster_id, sfid) is not None:
            continue

        try:
            info = await loop.run_in_executor(
                None, load_tree_status, cluster_snapshot, cluster_name, sfid
            )
        except Exception as e:
            if is_cluster_wide_fatal(e):
                print(
                    f"[snapman] warm sweep: cluster {cluster_id} auth/version error ({e}) -- "
                    "skipping the rest of this pass",
                    file=sys.stderr,
                )
                return
            print(
                f"[snapman] warm sweep: tree {sfid} on cluster {cluster_id} failed ({e}) -- skipping",
                file=sys.stderr,
            )
            continue

        if info is None:
            # Tree no longer exists -- self-heal. Nothing in the UI can ever
            # untoggle a tree that's disappeared from /groups.
            async with SessionLocal() as db:
                stale = await db.get(WarmTree, warm_tree.id)
                if stale is not None:
                    await db.delete(stale)
                    await db.commit()
            continue

        if info["held_reason"] is not None:
            # Permanently blocked -- re-launching every pass forever would
            # waste real Qumulo load for zero benefit (see load_tree_status).
            continue

        if info["unmeasured"] == 0:
            continue

        async with SessionLocal() as db:
            job = await launch_inspect_job(
                db, cluster_id, cluster_name, cluster_snapshot,
                sfid, info["path"], warm_tree.created_by,
            )
        _active_jobs[job.id] = job
        try:
            await job.task
        except Exception as e:
            print(
                f"[snapman] warm sweep: inspect of {sfid} on cluster {cluster_id} raised ({e})",
                file=sys.stderr,
            )
        finally:
            _active_jobs.pop(job.id, None)
