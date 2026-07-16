"""Path helpers — direct port of qsnap's io/paths.py."""

from app.qumulo.api import snapshot_file_attrs
from app.qumulo.cache import Cache
from app.qumulo.client import ApiError, Client


def normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def is_ancestor(anc: str, desc: str) -> bool:
    return desc == anc or desc.startswith(anc.rstrip("/") + "/")


def paths_nest(a: str, b: str) -> bool:
    return is_ancestor(a, b) or is_ancestor(b, a)


def resolve_source_path(
    client: Client,
    cache: Cache,
    cluster_name: str,
    source_file_id: str,
    snapshot_id: int,
) -> str:
    cached = cache.get_path(cluster_name, source_file_id)
    if cached is not None:
        return cached

    try:
        path = snapshot_file_attrs(client, snapshot_id, file_id=source_file_id).path
    except ApiError:
        path = None
    if path is None:
        path = f"<id:{source_file_id}>"

    cache.put_path(cluster_name, source_file_id, path)
    return path
