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

import contextlib
import os
import tarfile
import tempfile
import zipfile
from typing import IO, Protocol

from servery import security

_CHUNK = 64 * 1024
MAX_TOTAL: int = 1024**3  # 1 GiB uncompressed, total (absolute ceiling)
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


class _TargetLocks(Protocol):
    def hold(self, path: str, timeout: float = 0.0): ...


def _enforce_total(total: int, max_total: int) -> None:
    if total > max_total:
        raise ExtractError("archive expands beyond the size limit (possible zip bomb)")


def is_archive(name: str) -> bool:
    """True if ``name`` looks like a supported archive."""
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


def _resolve(dest_real: str, dest_dir: str, name: str) -> str:
    """Resolve an entry name under ``dest_dir`` and verify containment (zip-slip)."""
    target = os.path.realpath(os.path.join(dest_dir, name))
    if not security.is_contained(dest_real, target):
        raise ExtractError(f"unsafe path in archive: {name!r}")
    return target


def _write(
    src: IO[bytes],
    target: str,
    total: int,
    max_total: int,
    *,
    display_name: str,
    overwrite: bool,
    target_locks: _TargetLocks | None,
    lock_timeout: float,
) -> int:
    """Stream one entry to a temporary file and atomically commit it."""
    os.makedirs(os.path.dirname(target), exist_ok=True)

    @contextlib.contextmanager
    def unlocked():
        yield True

    guard = target_locks.hold(target, lock_timeout) if target_locks is not None else unlocked()
    with guard as acquired:
        if not acquired:
            raise ExtractError(f"target is busy: {display_name!r}")
        if not overwrite and os.path.exists(target):
            raise ExtractError(f"refusing to overwrite {display_name!r}")
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 (closed before replace)
            dir=os.path.dirname(target), delete=False
        )
        try:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                _enforce_total(total, max_total)
                tmp.write(chunk)
            tmp.close()
            os.replace(tmp.name, target)
        except BaseException:
            tmp.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise
    return total


def extract(
    archive_path: str,
    dest_dir: str,
    *,
    allow_overwrite: bool = False,
    max_total: int = MAX_TOTAL,
    target_locks: _TargetLocks | None = None,
    lock_timeout: float = 0.0,
) -> list[str]:
    """Securely extract ``archive_path`` into ``dest_dir``; return extracted names."""
    dest_real = os.path.realpath(dest_dir)
    if zipfile.is_zipfile(archive_path):
        return _extract_zip(
            archive_path,
            dest_dir,
            dest_real,
            overwrite=allow_overwrite,
            max_total=max_total,
            target_locks=target_locks,
            lock_timeout=lock_timeout,
        )
    if tarfile.is_tarfile(archive_path):
        return _extract_tar(
            archive_path,
            dest_dir,
            dest_real,
            overwrite=allow_overwrite,
            max_total=max_total,
            target_locks=target_locks,
            lock_timeout=lock_timeout,
        )
    raise ExtractError("not a supported archive (zip or tar)")


def _extract_zip(
    path: str,
    dest_dir: str,
    dest_real: str,
    *,
    overwrite: bool,
    max_total: int,
    target_locks: _TargetLocks | None,
    lock_timeout: float,
) -> list[str]:
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
            with zf.open(info) as src:  # open() below never creates a symlink
                total = _write(
                    src,
                    target,
                    total,
                    max_total,
                    display_name=info.filename,
                    overwrite=overwrite,
                    target_locks=target_locks,
                    lock_timeout=lock_timeout,
                )
            extracted.append(info.filename)
    return extracted


def _extract_tar(
    path: str,
    dest_dir: str,
    dest_real: str,
    *,
    overwrite: bool,
    max_total: int,
    target_locks: _TargetLocks | None,
    lock_timeout: float,
) -> list[str]:
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
            src = tf.extractfile(member)
            if src is None:
                continue
            total = _write(
                src,
                target,
                total,
                max_total,
                display_name=member.name,
                overwrite=overwrite,
                target_locks=target_locks,
                lock_timeout=lock_timeout,
            )
            extracted.append(member.name)
    return extracted
