"""Typed wrappers over the Qumulo snapshot-diff REST APIs.

Ported directly from qsnap — same dataclasses, same from_json, same pagination
pattern. The only change is the import path for Client / ApiError.
"""

import re
import urllib.parse

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from app.qumulo.client import Client

BLOCK_SIZE = 4096

MIN_CORE_VERSION = (7, 9, 0)


class DiffOp(Enum):
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


@dataclass(frozen=True)
class Snapshot:
    id: int
    name: str
    timestamp: str
    source_file_id: str
    policy_id: int | None
    expiration: str
    in_delete: bool
    locked: bool = False
    has_owners: bool = False

    @property
    def held(self) -> bool:
        return self.locked or self.has_owners

    @property
    def held_reason(self) -> str:
        return "locked" if self.locked else "owned (replication)"

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        return cls(
            id=int(d["id"]),
            name=d["name"],
            timestamp=d["timestamp"],
            source_file_id=d["source_file_id"],
            policy_id=d.get("policy_id"),
            expiration=d.get("expiration", ""),
            in_delete=bool(d.get("in_delete", False)),
            locked=bool(d.get("lock_key") or d.get("locked")),
            has_owners=bool(d.get("owners") or d.get("has_owners")),
        )


@dataclass(frozen=True)
class TreeDiffEntry:
    op: DiffOp
    path: str

    @classmethod
    def from_json(cls, d: dict) -> "TreeDiffEntry":
        return cls(op=DiffOp(d["op"]), path=d["path"])

    @property
    def is_directory(self) -> bool:
        return self.path.endswith("/")


@dataclass(frozen=True)
class FileDiffEntry:
    op: DiffOp
    offset: int
    size: int

    @classmethod
    def from_json(cls, d: dict) -> "FileDiffEntry":
        return cls(op=DiffOp(d["op"]), offset=int(d["offset"]), size=int(d["size"]))


@dataclass(frozen=True)
class FileAttrs:
    file_id: str
    size: int
    logical_datablocks: int
    type: str
    path: str | None = None

    @classmethod
    def from_json(cls, d: dict) -> "FileAttrs":
        return cls(
            file_id=d["id"],
            size=int(d["size"]),
            logical_datablocks=int(d["logical_datablocks"]),
            type=d["type"],
            path=d.get("path"),
        )

    @property
    def data_bytes(self) -> int:
        return min(self.size, self.logical_datablocks * BLOCK_SIZE)

    @property
    def is_directory(self) -> bool:
        return self.type == "FS_FILE_TYPE_DIRECTORY"

    @property
    def is_file(self) -> bool:
        return self.type == "FS_FILE_TYPE_FILE"


def _quote(ref: str) -> str:
    return urllib.parse.quote(ref, safe="")


def _file_ref(file_id: str | None, path: str | None) -> str:
    if (file_id is None) == (path is None):
        raise ValueError("specify exactly one of file_id or path")
    return _quote(file_id if file_id is not None else path)  # type: ignore[arg-type]


def _paged_entries(client: Client, first_uri: str, key: str = "entries") -> Iterator[dict]:
    uri: str | None = first_uri
    while uri is not None:
        data = client.request("GET", uri)
        yield from data.get(key, [])
        next_uri = data.get("paging", {}).get("next")
        uri = next_uri or None


_STATUS_FILTER = {
    "all": None,
    "exclude_in_delete": "api_snapshots_exclude_in_delete",
    "only_in_delete": "api_snapshots_exclude_not_in_delete",
}


def list_snapshots(client: Client, filter_: str = "all") -> list[Snapshot]:
    value = _STATUS_FILTER[filter_]
    uri = "/v4/snapshots/status/"
    if value is not None:
        uri += f"?filter={value}"
    data = client.request("GET", uri)
    return [Snapshot.from_json(e) for e in data.get("entries", [])]


def get_cluster_name(client: Client) -> str:
    return client.request("GET", "/v1/cluster/settings")["cluster_name"]


def get_version(client: Client) -> str:
    return client.request("GET", "/v1/version")["revision_id"]


def parse_core_version(revision_id: str) -> tuple[int, int, int] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", revision_id.rsplit(" ", 1)[-1])
    if m is None:
        return None
    return int(m[1]), int(m[2]), int(m[3])


def delete_snapshot(client: Client, snapshot_id: int) -> None:
    client.request("DELETE", f"/v3/snapshots/{snapshot_id}")


def tree_diff_pages(
    client: Client,
    newer_id: int,
    older_id: int,
    *,
    limit: int | None = None,
    start_cursor: str | None = None,
) -> Iterator[tuple[list[TreeDiffEntry], str | None]]:
    if start_cursor is not None:
        uri: str | None = start_cursor
    else:
        uri = f"/v2/snapshots/{newer_id}/changes-since/{older_id}"
        if limit is not None:
            uri += f"?limit={limit}"
    while uri is not None:
        data = client.request("GET", uri)
        entries = [TreeDiffEntry.from_json(e) for e in data.get("entries", [])]
        next_uri = data.get("paging", {}).get("next") or None
        yield entries, next_uri
        uri = next_uri


def tree_diff(
    client: Client, newer_id: int, older_id: int, *, limit: int | None = None
) -> Iterator[TreeDiffEntry]:
    for entries, _ in tree_diff_pages(client, newer_id, older_id, limit=limit):
        yield from entries


def file_diff(
    client: Client,
    newer_id: int,
    older_id: int,
    *,
    file_id: str | None = None,
    path: str | None = None,
    limit: int | None = None,
) -> Iterator[FileDiffEntry]:
    ref = _file_ref(file_id, path)
    uri = f"/v3/snapshots/{newer_id}/changes-since/{older_id}/files/{ref}"
    if limit is not None:
        uri += f"?limit={limit}"
    for e in _paged_entries(client, uri):
        yield FileDiffEntry.from_json(e)


def snapshot_file_attrs(
    client: Client,
    snapshot_id: int,
    *,
    file_id: str | None = None,
    path: str | None = None,
) -> FileAttrs:
    ref = _file_ref(file_id, path)
    data = client.request("GET", f"/v1/files/{ref}/info/attributes?snapshot={snapshot_id}")
    return FileAttrs.from_json(data)


def read_dir_in_snapshot(
    client: Client,
    snapshot_id: int,
    *,
    file_id: str | None = None,
    path: str | None = None,
    page_size: int = 1000,
) -> Iterator[FileAttrs]:
    ref = _file_ref(file_id, path)
    uri = f"/v1/files/{ref}/entries/?snapshot={snapshot_id}&limit={page_size}"
    for f in _paged_entries(client, uri, key="files"):
        yield FileAttrs.from_json(f)
