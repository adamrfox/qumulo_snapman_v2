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
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    done: bool = False
    cancel_requested: bool = False


_registry: dict[str, InspectJob] = {}


def create(
    job_id: str,
    cluster_id: str,
    cluster_name: str,
    source_file_id: str,
    path: str,
    started_by: str,
) -> InspectJob:
    job = InspectJob(
        id=job_id,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        source_file_id=source_file_id,
        path=path,
        started_by=started_by,
    )
    _registry[job.id] = job
    return job


def get(job_id: str) -> InspectJob | None:
    return _registry.get(job_id)


def find_running(cluster_id: str, source_file_id: str) -> InspectJob | None:
    for job in _registry.values():
        if job.cluster_id == cluster_id and job.source_file_id == source_file_id and not job.done:
            return job
    return None


def purge_done(keep: int = 100) -> None:
    done = [jid for jid, j in _registry.items() if j.done]
    for jid in done[:-keep]:
        _registry.pop(jid, None)
