"""The single path-resolution choke-point.

Every filesystem access goes through :func:`is_contained`, which closes the
symlink-escape gap that ``http.server`` leaves open. The model is Starlette's
``StaticFiles.lookup_path``: resolve real paths on *both* sides and require the
target to live under the root via :func:`os.path.commonpath` (never a
``startswith`` prefix check, which is fooled by ``/srv/rootEVIL`` vs ``/srv/root``).

The resolver fails *closed*: anything it cannot prove is contained is treated as
absent, so callers return 404 (never 403) and never leak whether a path exists.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

_POSIX = os.name == "posix"


def safe_segments(url_path: str) -> list[str]:
    """Decode a URL path into safe filesystem segments.

    Drops the query/fragment, percent-decodes, and removes empty, ``.`` and ``..``
    segments — so a join under any root can never traverse above it.
    """
    path = url_path.split("?", 1)[0].split("#", 1)[0]
    return [seg for seg in urllib.parse.unquote(path).split("/") if seg and seg not in (".", "..")]


def safe_join(root_real: str, url_path: str) -> str | None:
    """Resolve ``url_path`` to a contained filesystem path under ``root_real``, else ``None``."""
    candidate = os.path.join(root_real, *safe_segments(url_path))  # noqa: PTH118 (os-level by design)
    return candidate if is_contained(root_real, candidate) else None


def is_contained(root_real: str, candidate: str) -> bool:
    """Return True iff ``candidate`` resolves to a path inside ``root_real``.

    ``root_real`` must already be a realpath. ``candidate`` is a filesystem path
    (possibly a symlink); its real path must be ``root_real`` itself or a
    descendant of it.
    """
    real = os.path.realpath(candidate)
    if real == root_real:
        return True
    if _POSIX:
        # Both sides are already realpath'd (absolute, normalized), so a
        # separator-anchored prefix test is exactly equivalent to commonpath here
        # — and ~15x faster on this per-request hot path. The trailing separator
        # is what makes it safe: it rejects a sibling like ``/srv/rootEVIL`` for
        # root ``/srv/root`` (the classic startswith bug). Stripping a trailing
        # separator from the root keeps serving the filesystem root (``/``) working.
        return real.startswith(root_real.rstrip(os.sep) + os.sep)
    try:
        # Windows: commonpath handles drive letters / UNC / case folding robustly.
        return os.path.commonpath((root_real, real)) == root_real
    except ValueError:
        # Mixed absolute/relative or cross-drive paths cannot be contained.
        return False


def contained_path(root: Path, candidate: str) -> str | None:
    """Return ``candidate`` if contained in ``root``, else ``None``.

    Convenience wrapper used by callers that resolve the root once per request.
    """
    root_real = os.path.realpath(root)
    return candidate if is_contained(root_real, candidate) else None
