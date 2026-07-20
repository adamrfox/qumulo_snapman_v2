"""Duplicates the backend's own stdout/stderr into a file so an admin can
download it later (see routers/admin_logs.py) -- otherwise everything the
process prints only ever reaches Docker's log driver.

A stdout/stderr Tee (rather than a logging.FileHandler on the root logger)
is used deliberately: uvicorn's own access/error loggers set
propagate=False in their default config, so a root-logger handler alone
would silently miss request logs and unhandled-exception tracebacks. Raw
print() calls scattered through compute/*.py would also bypass a logging
handler entirely. Wrapping the streams themselves catches all of it
uniformly, with no per-callsite changes anywhere else.

Must run at import time, before uvicorn starts logging, so call
install_log_tee() as the first thing app/main.py does.
"""

import sys

from pathlib import Path

_MAX_LOG_BYTES = 20 * 1024 * 1024  # rotate on startup, not mid-run, if this big


class _Tee:
    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for s in self._streams:
            s.write(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()

    def isatty(self) -> bool:
        return self._streams[0].isatty()


def install_log_tee(log_dir: str) -> None:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "backend.log"

    if log_path.exists() and log_path.stat().st_size > _MAX_LOG_BYTES:
        log_path.replace(directory / "backend.log.1")

    log_file = open(log_path, "a", buffering=1)  # noqa: SIM115 -- kept open for process lifetime

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
