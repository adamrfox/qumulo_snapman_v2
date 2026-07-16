"""Human-readable size/duration formatting. Direct port of qsnap."""

import re
from datetime import timedelta

_SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in _BYTE_UNITS[1:]:
        size /= 1024
        if size < 1024:
            return f"{size:.2f} {unit}"
    return f"{size:.2f} {_BYTE_UNITS[-1]}"
