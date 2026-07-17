"""In-memory mock implementing the Client protocol for app.qumulo.api tests.

Ported from qsnap's test/client.py (same fixture/registration shape) with the
import path swapped to this repo's client module.
"""

import re
import urllib.parse

from collections.abc import Callable
from dataclasses import dataclass

from app.qumulo.client import ApiError


@dataclass
class _Page:
    entries: list[dict]
    page_size: int = 1000


def _split(path: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(path)
    return parsed.path, dict(urllib.parse.parse_qsl(parsed.query))


def _with_datablocks_default(attrs: dict) -> dict:
    if "logical_datablocks" in attrs:
        return attrs
    return {**attrs, "logical_datablocks": "0"}


class TestClient:
    def __init__(self) -> None:
        self._snapshots: list[dict] = []
        self._tree_diffs: dict[tuple[int, int], _Page] = {}
        self._file_diffs: dict[tuple[int, int, str], _Page] = {}
        self._attrs: dict[tuple[int, str], dict] = {}
        self._dirs: dict[tuple[int, str], _Page] = {}
        self._cluster_name: str | None = None
        self._revision_id: str = "Qumulo Core 7.9.0"
        self._errors: dict[str, ApiError] = {}
        self._tree_diff_errors: dict[tuple[int, int], ApiError] = {}
        self._file_diff_errors: dict[tuple[int, int, str], ApiError] = {}
        self._delete_errors: dict[int, ApiError] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.api_snapshot: Callable[[], tuple[int, int, tuple[tuple[float, float], ...]]] = lambda: (
            0,
            0,
            (),
        )

    # -- registration -------------------------------------------------------

    def add_snapshots(self, snapshots: list[dict]) -> None:
        self._snapshots = list(snapshots)

    def set_tree_diff(
        self, newer: int, older: int, entries: list[dict], *, page_size: int = 1000
    ) -> None:
        self._tree_diffs[(newer, older)] = _Page(entries, page_size)

    def set_file_diff(
        self,
        newer: int,
        older: int,
        ref: str,
        entries: list[dict],
        *,
        page_size: int = 1000,
    ) -> None:
        self._file_diffs[(newer, older, ref)] = _Page(entries, page_size)

    def set_attrs(self, snapshot: int, ref: str, attrs: dict) -> None:
        self._attrs[(snapshot, ref)] = _with_datablocks_default(attrs)

    def set_dir(self, snapshot: int, ref: str, files: list[dict], *, page_size: int = 1000) -> None:
        self._dirs[(snapshot, ref)] = _Page([_with_datablocks_default(f) for f in files], page_size)

    def set_cluster_name(self, name: str) -> None:
        self._cluster_name = name

    def set_revision_id(self, revision_id: str) -> None:
        self._revision_id = revision_id

    def set_error(self, ref: str, error: ApiError) -> None:
        self._errors[ref] = error

    def set_tree_diff_error(self, newer: int, older: int, error: ApiError) -> None:
        self._tree_diff_errors[(newer, older)] = error

    def set_file_diff_error(self, newer: int, older: int, ref: str, error: ApiError) -> None:
        self._file_diff_errors[(newer, older, ref)] = error

    def set_delete_error(self, snapshot_id: int, error: ApiError) -> None:
        self._delete_errors[snapshot_id] = error

    # -- request dispatch ---------------------------------------------------

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.requests.append((method, path, body))
        base, query = _split(path)

        if base == "/v4/snapshots/status/":
            filter_ = query.get("filter")
            if filter_ == "api_snapshots_exclude_in_delete":
                return {"entries": [s for s in self._snapshots if not s.get("in_delete")]}
            if filter_ == "api_snapshots_exclude_not_in_delete":
                return {"entries": [s for s in self._snapshots if s.get("in_delete")]}
            return {"entries": self._snapshots}

        if base == "/v1/cluster/settings":
            assert self._cluster_name is not None, "TestClient: cluster name not registered"
            return {"cluster_name": self._cluster_name}

        if base == "/v1/version":
            return {"revision_id": self._revision_id}

        m = re.fullmatch(r"/v2/snapshots/(\d+)/changes-since/(\d+)", base)
        if m:
            key = (int(m[1]), int(m[2]))
            if key in self._tree_diff_errors:
                raise self._tree_diff_errors[key]
            assert key in self._tree_diffs, f"TestClient: no tree diff {key}"
            return self._serve(base, query, self._tree_diffs[key], "entries")

        m = re.fullmatch(r"/v3/snapshots/(\d+)/changes-since/(\d+)/files/(.+)", base)
        if m:
            ref = urllib.parse.unquote(m[3])
            fd_key = (int(m[1]), int(m[2]), ref)
            if fd_key in self._file_diff_errors:
                raise self._file_diff_errors[fd_key]
            self._maybe_raise(ref)
            key = fd_key
            assert key in self._file_diffs, f"TestClient: no file diff {key}"
            return self._serve(base, query, self._file_diffs[key], "entries")

        m = re.fullmatch(r"/v3/snapshots/(\d+)", base)
        if m:
            sid = int(m[1])
            if method == "DELETE":
                return self._delete_snapshot(sid)
            for s in self._snapshots:
                if int(s["id"]) == sid:
                    return s
            raise ApiError(404, "snapshot_not_found_error", f"no snapshot {sid}")

        m = re.fullmatch(r"/v1/files/(.+)/info/attributes", base)
        if m:
            ref = urllib.parse.unquote(m[1])
            self._maybe_raise(ref)
            key = (int(query["snapshot"]), ref)
            assert key in self._attrs, f"TestClient: no attrs {key}"
            return self._attrs[key]

        m = re.fullmatch(r"/v1/files/(.+)/entries/", base)
        if m:
            ref = urllib.parse.unquote(m[1])
            self._maybe_raise(ref)
            key = (int(query["snapshot"]), ref)
            assert key in self._dirs, f"TestClient: no dir {key}"
            return self._serve(base, query, self._dirs[key], "files")

        raise AssertionError(f"TestClient: unrecognized request {method} {path}")

    def _maybe_raise(self, ref: str) -> None:
        if ref in self._errors:
            raise self._errors[ref]

    def _delete_snapshot(self, sid: int) -> dict:
        if sid in self._delete_errors:
            raise self._delete_errors[sid]
        for i, s in enumerate(self._snapshots):
            if int(s["id"]) == sid and not s.get("in_delete"):
                self._snapshots[i] = {**s, "in_delete": True}
                return self._snapshots[i]
        raise ApiError(
            404,
            "snapshot_not_found_error",
            "The snapshot you are trying to delete no longer exists.",
        )

    @staticmethod
    def _serve(base: str, query: dict[str, str], page: _Page, key: str) -> dict:
        after = int(query.get("after", "0"))
        items = page.entries[after : after + page.page_size]
        end = after + len(items)
        if end < len(page.entries):
            params = {k: v for k, v in query.items() if k != "after"}
            params["after"] = str(end)
            next_uri: str | None = base + "?" + urllib.parse.urlencode(params)
        else:
            next_uri = None
        return {key: items, "paging": {"next": next_uri}}
