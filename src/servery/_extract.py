"""Safe archive extraction for uploads (opt-in via ``--upload-extract``).

Archive extraction is a classic CVE source, so every known footgun is guarded:

* **Zip-slip / path traversal** — each entry's real path must stay inside the
  destination (``security.is_contained``); ``..`` and absolute names can't escape.
* **Symlinks / hardlinks / devices** — never created. We extract *only* regular
  files and directories (via ``open()``), so a malicious link entry is skipped,
  not materialized.
* **Zip bombs** — total uncompressed bytes and entry count are capped, enforced on
  the bytes actually written (not the archive's self-reported sizes).

Supports zip and tar (gz/bz2/xz). Extracts into the destination directory.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from typing import IO

from servery import security

_CHUNK = 64 * 1024
_MAX_TOTAL: int = 1024**3  # 1 GiB uncompressed, total
_MAX_ENTRIES: int = 10_000

_ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


class ExtractError(Exception):
    """The archive was unsafe (traversal / bomb) or unsupported."""


def is_archive(name: str) -> bool:
    """True if ``name`` looks like a supported archive."""
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


def _resolve(dest_real: str, dest_dir: str, name: str) -> str:
    """Resolve an entry name under ``dest_dir`` and verify containment (zip-slip)."""
    target = os.path.realpath(os.path.join(dest_dir, name))
    if not security.is_contained(dest_real, target):
        raise ExtractError(f"unsafe path in archive: {name!r}")
    return target


def _write(src: IO[bytes], target: str, total: int) -> int:
    """Stream ``src`` to ``target``; return the running uncompressed total."""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as dst:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_TOTAL:
                raise ExtractError("archive expands beyond the size limit (possible zip bomb)")
            dst.write(chunk)
    return total


def extract(archive_path: str, dest_dir: str, *, allow_overwrite: bool = False) -> list[str]:
    """Securely extract ``archive_path`` into ``dest_dir``; return extracted names."""
    dest_real = os.path.realpath(dest_dir)
    if zipfile.is_zipfile(archive_path):
        return _extract_zip(archive_path, dest_dir, dest_real, overwrite=allow_overwrite)
    if tarfile.is_tarfile(archive_path):
        return _extract_tar(archive_path, dest_dir, dest_real, overwrite=allow_overwrite)
    raise ExtractError("not a supported archive (zip or tar)")


def _extract_zip(path: str, dest_dir: str, dest_real: str, *, overwrite: bool) -> list[str]:
    extracted: list[str] = []
    total = 0
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ExtractError("archive has too many entries")
        for info in infos:
            target = _resolve(dest_real, dest_dir, info.filename)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            if not overwrite and os.path.exists(target):
                raise ExtractError(f"refusing to overwrite {info.filename!r}")
            with zf.open(info) as src:  # open() below never creates a symlink
                total = _write(src, target, total)
            extracted.append(info.filename)
    return extracted


def _extract_tar(path: str, dest_dir: str, dest_real: str, *, overwrite: bool) -> list[str]:
    extracted: list[str] = []
    total = 0
    with tarfile.open(path) as tf:  # members validated below; no extractall
        members = tf.getmembers()
        if len(members) > _MAX_ENTRIES:
            raise ExtractError("archive has too many entries")
        for member in members:
            # Only regular files and directories; symlinks/hardlinks/devices/fifos
            # are silently skipped (never materialized).
            if not (member.isfile() or member.isdir()):
                continue
            target = _resolve(dest_real, dest_dir, member.name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not overwrite and os.path.exists(target):
                raise ExtractError(f"refusing to overwrite {member.name!r}")
            src = tf.extractfile(member)
            if src is None:
                continue
            total = _write(src, target, total)
            extracted.append(member.name)
    return extracted
