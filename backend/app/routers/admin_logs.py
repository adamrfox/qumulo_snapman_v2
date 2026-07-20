"""Admin-only diagnostic log download.

Bundles the backend's own tee'd log (see app/logsetup.py) with nginx's
access/error logs (written to a volume shared with the frontend container,
see docker-compose.yml) so an admin can grab everything useful in one request
and hand it off for troubleshooting -- without needing shell/Docker access to
the host, which customers generally won't have.
"""

from pathlib import Path

from fastapi import APIRouter

from app.auth import RequireAdmin
from app.config import settings

router = APIRouter()

MAX_LINES = 50_000
DEFAULT_LINES = 5_000


def _tail_lines(text: str, lines: int) -> list[str]:
    return text.splitlines()[-lines:] if lines > 0 else []


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")


def _tail_backend_log(log_dir: Path, lines: int) -> str:
    """Tail backend.log, falling back into backend.log.1 (the previous
    rotation) if the current file alone doesn't have enough lines yet."""
    current_lines = _tail_lines(_read(log_dir / "backend.log"), lines)
    if len(current_lines) >= lines:
        return "\n".join(current_lines)
    remaining = lines - len(current_lines)
    backup_lines = _tail_lines(_read(log_dir / "backend.log.1"), remaining)
    return "\n".join([*backup_lines, *current_lines])


def _tail_plain(path: Path, lines: int) -> str:
    return "\n".join(_tail_lines(_read(path), lines))


@router.get("/logs")
async def get_logs(admin: RequireAdmin, lines: int = DEFAULT_LINES) -> dict:
    lines = max(1, min(lines, MAX_LINES))
    log_dir = Path(settings.log_dir)
    nginx_dir = Path(settings.nginx_log_dir)
    return {
        "backend_log": _tail_backend_log(log_dir, lines),
        "nginx_access_log": _tail_plain(nginx_dir / "access.log", lines),
        "nginx_error_log": _tail_plain(nginx_dir / "error.log", lines),
    }
