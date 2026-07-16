"""SQLite cache for snapshot listings and pair contributions.

Direct port of qsnap's cache.py. Lives on a Docker volume so it persists
across container restarts. Keyed by cluster_name (the cluster's own stable
identifier) so two users who register the same physical cluster share results.
"""

import json
import sqlite3
import threading
import time

from pathlib import Path

SCHEMA_VERSION = 1
_TABLES = ("snapshot_listing", "source_path", "pair_contribution", "pair_partial")


class Cache:
    def __init__(self, path: Path, *, now=time.time) -> None:
        self.path = path
        self._now = now
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate_locked()
            self._init_schema_locked()

    def _migrate_locked(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            for table in _TABLES:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _init_schema_locked(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS snapshot_listing (
                cluster_name   TEXT PRIMARY KEY,
                snapshots_json TEXT NOT NULL,
                fetched_at     REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS source_path (
                cluster_name   TEXT NOT NULL,
                source_file_id TEXT NOT NULL,
                path           TEXT NOT NULL,
                PRIMARY KEY (cluster_name, source_file_id)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pair_contribution (
                cluster_name   TEXT NOT NULL,
                source_file_id TEXT NOT NULL,
                older_id       INTEGER NOT NULL,
                newer_id       INTEGER NOT NULL,
                freed_bytes    INTEGER NOT NULL,
                total_files    INTEGER NOT NULL,
                computed_at    REAL NOT NULL,
                PRIMARY KEY (cluster_name, source_file_id, older_id, newer_id)
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pair_partial (
                cluster_name   TEXT NOT NULL,
                source_file_id TEXT NOT NULL,
                older_id       INTEGER NOT NULL,
                newer_id       INTEGER NOT NULL,
                cursor_token   TEXT NOT NULL,
                partial_freed  INTEGER NOT NULL,
                partial_files  INTEGER NOT NULL,
                updated_at     REAL NOT NULL,
                PRIMARY KEY (cluster_name, source_file_id, older_id, newer_id)
            )"""
        )
        self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            for table in _TABLES:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- snapshot listing ---------------------------------------------------

    def get_listing(self, cluster_name: str, ttl_seconds: float) -> list[dict] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshots_json, fetched_at FROM snapshot_listing WHERE cluster_name = ?",
                (cluster_name,),
            ).fetchone()
        if row is None:
            return None
        snapshots_json, fetched_at = row
        if self._now() - fetched_at > ttl_seconds:
            return None
        return json.loads(snapshots_json)

    def put_listing(self, cluster_name: str, snapshots: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshot_listing "
                "(cluster_name, snapshots_json, fetched_at) VALUES (?, ?, ?)",
                (cluster_name, json.dumps(snapshots), self._now()),
            )
            self._conn.commit()

    # -- source path labels -------------------------------------------------

    def get_path(self, cluster_name: str, source_file_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM source_path WHERE cluster_name = ? AND source_file_id = ?",
                (cluster_name, source_file_id),
            ).fetchone()
        return row[0] if row is not None else None

    def put_path(self, cluster_name: str, source_file_id: str, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO source_path "
                "(cluster_name, source_file_id, path) VALUES (?, ?, ?)",
                (cluster_name, source_file_id, path),
            )
            self._conn.commit()

    # -- per-pair contribution ----------------------------------------------

    def get_pairs(
        self, cluster_name: str, source_file_id: str
    ) -> dict[tuple[int, int], tuple[int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT older_id, newer_id, freed_bytes, total_files FROM pair_contribution "
                "WHERE cluster_name = ? AND source_file_id = ?",
                (cluster_name, source_file_id),
            ).fetchall()
        return {(r[0], r[1]): (r[2], r[3]) for r in rows}

    def put_pair(
        self,
        cluster_name: str,
        source_file_id: str,
        older_id: int,
        newer_id: int,
        freed_bytes: int,
        total_files: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pair_contribution (cluster_name, source_file_id, "
                "older_id, newer_id, freed_bytes, total_files, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cluster_name, source_file_id, older_id, newer_id, freed_bytes, total_files, self._now()),
            )
            self._conn.commit()

    # -- partial checkpoints ------------------------------------------------

    def get_partials(
        self, cluster_name: str, source_file_id: str
    ) -> dict[tuple[int, int], tuple[str, int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT older_id, newer_id, cursor_token, partial_freed, partial_files "
                "FROM pair_partial WHERE cluster_name = ? AND source_file_id = ?",
                (cluster_name, source_file_id),
            ).fetchall()
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}

    def put_partial(
        self,
        cluster_name: str,
        source_file_id: str,
        older_id: int,
        newer_id: int,
        cursor_token: str,
        partial_freed: int,
        partial_files: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO pair_partial (cluster_name, source_file_id, "
                "older_id, newer_id, cursor_token, partial_freed, partial_files, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cluster_name, source_file_id, older_id, newer_id,
                 cursor_token, partial_freed, partial_files, self._now()),
            )
            self._conn.commit()

    def delete_partial(
        self, cluster_name: str, source_file_id: str, older_id: int, newer_id: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM pair_partial WHERE cluster_name = ? AND source_file_id = ? "
                "AND older_id = ? AND newer_id = ?",
                (cluster_name, source_file_id, older_id, newer_id),
            )
            self._conn.commit()

    def get_reclaim_prefix(
        self, cluster_name: str, source_file_id: str, pair_ids: list[tuple[int, int]]
    ) -> tuple[int, int]:
        if not pair_ids:
            return (0, 0)
        pairs = self.get_pairs(cluster_name, source_file_id)
        total = count = 0
        for p in pair_ids:
            if p not in pairs:
                break
            total += pairs[p][0]
            count += 1
        return (total, count)
