"""Tests for the log-tail helpers behind GET /api/admin/logs.

Pure-function tests against the filesystem -- no FastAPI app/auth wiring
needed, since these are just text-file readers the endpoint calls into.
"""

import tempfile
import unittest

from pathlib import Path

from app.routers.admin_logs import _tail_backend_log, _tail_plain


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class TailPlainTest(unittest.TestCase):
    def test_missing_file_returns_empty_string(self) -> None:
        self.assertEqual(_tail_plain(_tmpdir() / "nope.log", 100), "")

    def test_more_lines_requested_than_exist_returns_whole_file(self) -> None:
        d = _tmpdir()
        path = d / "access.log"
        path.write_text("line1\nline2\nline3\n")
        self.assertEqual(_tail_plain(path, 100), "line1\nline2\nline3")

    def test_tail_limits_to_requested_line_count(self) -> None:
        d = _tmpdir()
        path = d / "access.log"
        path.write_text("\n".join(f"line{i}" for i in range(10)))
        self.assertEqual(_tail_plain(path, 3), "line7\nline8\nline9")


class TailBackendLogTest(unittest.TestCase):
    def test_missing_files_return_empty_string(self) -> None:
        self.assertEqual(_tail_backend_log(_tmpdir(), 100), "")

    def test_current_file_alone_is_enough(self) -> None:
        d = _tmpdir()
        (d / "backend.log").write_text("\n".join(f"cur{i}" for i in range(10)))
        self.assertEqual(_tail_backend_log(d, 3), "cur7\ncur8\ncur9")

    def test_falls_back_into_backup_when_current_is_short(self) -> None:
        d = _tmpdir()
        (d / "backend.log.1").write_text("\n".join(f"old{i}" for i in range(10)))
        (d / "backend.log").write_text("new0\nnew1")
        # Requesting 5 lines: current only has 2, so the 3 oldest come from
        # the backup, in order, followed by the current file's lines.
        self.assertEqual(_tail_backend_log(d, 5), "old7\nold8\nold9\nnew0\nnew1")

    def test_no_backup_needed_when_current_alone_satisfies_request(self) -> None:
        d = _tmpdir()
        (d / "backend.log.1").write_text("old0\nold1\nold2")
        (d / "backend.log").write_text("\n".join(f"cur{i}" for i in range(10)))
        self.assertEqual(_tail_backend_log(d, 3), "cur7\ncur8\ncur9")


if __name__ == "__main__":
    unittest.main()
