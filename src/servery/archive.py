"""On-the-fly directory archives, streamed (``tar.gz`` / ``zip``).

Both ``tarfile`` (streaming ``w|gz`` mode) and ``zipfile`` (non-seekable streaming
with data descriptors) write to an arbitrary ``.write(bytes)`` sink, so servery
can compress a directory straight onto the wire without buffering it. Only
regular files allowed by the configured traversal policy are included. Symlinks
and device crossings are disabled by default; followed directory cycles are pruned.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from typing import TYPE_CHECKING, cast

from servery import security
from servery.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

    from _typeshed import SupportsWrite


def _iter_regular_files(root_dir: str, config: Config | None = None) -> Iterator[str]:
    root_real = os.fspath(config.directory) if config else os.path.abspath(root_dir)
    config = config or Config.create(root_dir)
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=config.follow_symlinks):
        if not security.is_contained(root_real, dirpath, config):
            dirnames[:] = []
            continue
        stat = os.stat(dirpath)
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen:
            dirnames[:] = []
            continue
        seen.add(identity)
        dirnames[:] = [
            name
            for name in dirnames
            if security.is_contained(root_real, os.path.join(dirpath, name), config)
        ]
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if security.is_contained(root_real, full, config) and os.path.isfile(full):
                yield full


def _arcname(full: str, root_dir: str, base_name: str) -> str:
    relative = os.path.relpath(full, root_dir).replace(os.sep, "/")
    return f"{base_name}/{relative}"


def stream_targz(
    root_dir: str, base_name: str, writer: SupportsWrite[bytes], config: Config | None = None
) -> None:
    """Write a streaming gzip-compressed tar of ``root_dir`` to ``writer``."""
    with tarfile.open(fileobj=cast("BinaryIO", writer), mode="w|gz", dereference=True) as tar:
        for full in _iter_regular_files(root_dir, config):
            tar.add(full, arcname=_arcname(full, root_dir, base_name), recursive=False)


def stream_zip(
    root_dir: str, base_name: str, writer: SupportsWrite[bytes], config: Config | None = None
) -> None:
    """Write a streaming zip of ``root_dir`` to ``writer``."""
    with zipfile.ZipFile(
        cast("BinaryIO", writer),
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        for full in _iter_regular_files(root_dir, config):
            archive.write(full, arcname=_arcname(full, root_dir, base_name))


def stream_zip_selection(
    dir_path: str,
    names: list[str],
    base_name: str,
    writer: SupportsWrite[bytes],
    config: Config | None = None,
) -> None:
    """Stream a zip of the named direct children of ``dir_path`` (files and subdirs).

    ``names`` are bare entry names; any that are not a real child (contain a path
    separator or ``..``) are skipped. Symlinks and filesystem crossings follow
    the configured traversal policy.
    """
    with zipfile.ZipFile(
        cast("BinaryIO", writer),
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        for name in names:
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                continue
            full = os.path.join(dir_path, name)
            policy = config or Config.create(dir_path)
            root = os.fspath(config.directory) if config else os.path.abspath(dir_path)
            if not security.is_contained(root, full, policy):
                continue
            if os.path.isdir(full):
                for entry in _iter_regular_files(full, config):
                    archive.write(entry, arcname=_arcname(entry, dir_path, base_name))
            elif os.path.isfile(full):
                archive.write(full, arcname=_arcname(full, dir_path, base_name))
