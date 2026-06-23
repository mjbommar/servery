"""Access logging to a file — Common Log Format, Combined, or JSON lines.

Separate from the diagnostic stderr logger (``servery._log``): this writes one
structured line per response to ``--access-log`` for ops/analytics. Uses a
``logging.FileHandler`` (thread-safe, line-buffered) so it's safe under the
threading server. Covers the HTTP/1.1 file-serving surface (file/listing/error/
redirect/OPTIONS/upload/WebDAV responses) — everything that flows through the
handler's ``end_headers``.
"""

from __future__ import annotations

import json
import logging
import time

_FORMATS = ("clf", "combined", "json")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _clf_time(when: float) -> str:
    tm = time.localtime(when)
    offset = -(time.altzone if tm.tm_isdst else time.timezone)
    sign = "+" if offset >= 0 else "-"
    hh, mm = divmod(abs(offset) // 60, 60)
    return (
        f"{tm.tm_mday:02d}/{_MONTHS[tm.tm_mon - 1]}/{tm.tm_year:04d}:"
        f"{tm.tm_hour:02d}:{tm.tm_min:02d}:{tm.tm_sec:02d} {sign}{hh:02d}{mm:02d}"
    )


class AccessLog:
    """Append-only access log in ``clf`` / ``combined`` / ``json`` format."""

    def __init__(self, path: str, fmt: str = "clf") -> None:
        if fmt not in _FORMATS:
            raise ValueError(f"access-log format must be one of {_FORMATS}, got {fmt!r}")
        self._fmt = fmt
        self._logger = logging.getLogger("servery.access")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False  # never leak access lines into the stderr logger
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(file_handler)

    def record(
        self,
        client: str,
        requestline: str,
        status: int | str,
        size: int | str,
        *,
        referer: str = "-",
        user_agent: str = "-",
        when: float | None = None,
    ) -> None:
        when = time.time() if when is None else when
        if self._fmt == "json":
            method, _, rest = requestline.partition(" ")
            path, _, proto = rest.rpartition(" ")
            line = json.dumps(
                {
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + "Z",
                    "client": client,
                    "method": method,
                    "path": path,
                    "protocol": proto,
                    "status": status,
                    "size": size,
                    "referer": referer,
                    "user_agent": user_agent,
                }
            )
        else:
            line = f'{client} - - [{_clf_time(when)}] "{requestline}" {status} {size}'
            if self._fmt == "combined":
                line += f' "{referer}" "{user_agent}"'
        self._logger.info(line)
