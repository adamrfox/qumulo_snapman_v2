"""In-process inspect job registry.

Jobs live for the lifetime of the server process. The DB record (inspect_jobs)
is the durable state; this registry holds the live asyncio Queue used for SSE
streaming and the asyncio Task handle for cancellation.
"""

import asyncio
from dataclasses import dataclass, field


@dataclass
class InspectJob:
    id: str
    cluster_id: str
    cluster_name: str
    source_file_id: str
    path: str
    started_by: str
    job_type: str = "inspect"
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    done: bool = False
    cancel_requested: bool = False
    # Latest event pushed, kept outside the queue -- a second, later consumer
    # (e.g. the goal solver/measure-trees loop finding this job already
    # running via find_running) can read the current progress here without
    # draining events out from under whichever consumer actually owns this
    # job's queue (its own SSE stream, or another tab watching it directly).
    last_event: dict | None = None
    # Coherent pair/file progress snapshot, updated alongside last_event but
    # surviving past whichever single event happened to be pushed last --
    # e.g. a "pair_finished" event alone doesn't carry the pair's total, so a
    # consumer that only ever reads the *latest* raw event can lose track of
    # it. {"index": int, "total": int} / {"found": int, "sized": int}.
    last_pair_progress: dict | None = None
    last_sub_progress: dict | None = None


_registry: dict[str, InspectJob] = {}


def create(
    job_id: str,
    cluster_id: str,
    cluster_name: str,
    source_file_id: str,
    path: str,
    started_by: str,
    job_type: str = "inspect",
) -> InspectJob:
    job = InspectJob(
        id=job_id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=path,
        started_by=started_by,
        job_type=job_type,
    )
    _registry[job.id] = job
    return job


def get(job_id: str) -> InspectJob | None:
    return _registry.get(job_id)


def find_running(cluster_id: str, source_file_id: str, job_type: str = "inspect") -> InspectJob | None:
    for job in _registry.values():
        if (
            job.cluster_id == cluster_id
            and job.source_file_id == source_file_id
            and job.job_type == job_type
            and not job.done
        ):
            return job
    return None


def purge_done(keep: int = 100) -> None:
    done = [jid for jid, j in _registry.items() if j.done]
    for jid in done[:-keep]:
        _registry.pop(jid, None)
