"""On-the-fly directory archives, streamed (``tar.gz`` / ``zip``).

Both ``tarfile`` (streaming ``w|gz`` mode) and ``zipfile`` (non-seekable streaming
with data descriptors) write to an arbitrary ``.write(bytes)`` sink, so servery
can compress a directory straight onto the wire without buffering it. Only
regular files are included — symlinks are skipped (``os.walk`` does not follow
them), so an archive can never leak content from outside the served tree.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

    from _typeshed import SupportsWrite


def _iter_regular_files(root_dir: str) -> Iterator[str]:
    for dirpath, _dirnames, filenames in os.walk(root_dir, followlinks=False):
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if not os.path.islink(full):
                yield full


def _arcname(full: str, root_dir: str, base_name: str) -> str:
    relative = os.path.relpath(full, root_dir).replace(os.sep, "/")
    return f"{base_name}/{relative}"


def stream_targz(root_dir: str, base_name: str, writer: SupportsWrite[bytes]) -> None:
    """Write a streaming gzip-compressed tar of ``root_dir`` to ``writer``."""
    with tarfile.open(fileobj=cast("BinaryIO", writer), mode="w|gz") as tar:
        for full in _iter_regular_files(root_dir):
            tar.add(full, arcname=_arcname(full, root_dir, base_name), recursive=False)


def stream_zip(root_dir: str, base_name: str, writer: SupportsWrite[bytes]) -> None:
    """Write a streaming zip of ``root_dir`` to ``writer``."""
    with zipfile.ZipFile(
        cast("BinaryIO", writer),
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        for full in _iter_regular_files(root_dir):
            archive.write(full, arcname=_arcname(full, root_dir, base_name))
