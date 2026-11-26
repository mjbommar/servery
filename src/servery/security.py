"""The single path-resolution choke-point.

Request paths use the configured symlink and filesystem traversal policy.
Internal callers without a config use realpath containment, closing the
symlink-escape gap that ``http.server`` leaves open. Separator-aware checks
reject sibling prefixes such as ``/srv/rootEVIL`` for ``/srv/root``.

The resolver fails *closed*: anything it cannot prove is contained is treated as
absent, so callers return 404 (never 403) and never leak whether a path exists.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

_POSIX = os.name == "posix"


def safe_segments(url_path: str) -> list[str]:
    """Decode a URL path into safe filesystem segments.

    Drops the query/fragment, percent-decodes, and removes empty, ``.`` and ``..``
    segments — so a join under any root can never traverse above it.
    """
    path = url_path.split("?", 1)[0].split("#", 1)[0]
    return [seg for seg in urllib.parse.unquote(path).split("/") if seg and seg not in (".", "..")]


def safe_join(root_real: str, url_path: str, config: Config | None = None) -> str | None:
    """Resolve ``url_path`` to a contained filesystem path under ``root_real``, else ``None``."""
    candidate = os.path.join(root_real, *safe_segments(url_path))  # noqa: PTH118 (os-level by design)
    return candidate if is_contained(root_real, candidate, config) else None


def is_contained(root_real: str, candidate: str, config: Config | None = None) -> bool:
    """Check containment, or apply the configured traversal policy.

    With a config, paths must start lexically under the root; symlinks and
    device crossings require explicit opt-ins. Without one, realpath containment
    is used for internal callers such as archive extraction.

    ``root_real`` must already be a realpath. ``candidate`` is a filesystem path
    (possibly a symlink); its real path must be ``root_real`` itself or a
    descendant of it.
    """
    if config is not None:
        # Validate the lexical path before following any explicitly allowed links.
        lexical = os.path.abspath(candidate)  # noqa: PTH100 - must not resolve links
        try:
            if os.path.commonpath((root_real, lexical)) != root_real:
                return False
            current = root_real
            root_device = Path(root_real).stat().st_dev
            for segment in Path(os.path.relpath(lexical, root_real)).parts:
                current = os.path.join(current, segment)  # noqa: PTH118
                if not config.follow_symlinks and Path(current).is_symlink():
                    return False
                try:
                    device = Path(current).stat().st_dev
                except FileNotFoundError:
                    # New upload destinations inherit their existing parent's policy.
                    continue
                if not config.cross_filesystems and device != root_device:
                    return False
        except (OSError, ValueError):
            return False
        return True
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
