"""Streaming ``multipart/form-data`` upload handling (RFC 7578), no ``cgi``.

The ``cgi`` module (and its ``FieldStorage``) was removed in Python 3.13, and the
stdlib has no streaming replacement, so servery parses multipart bodies itself:
each file part is streamed straight to a temporary file in the destination
directory and then atomically committed with :func:`os.replace`. Memory stays
bounded by the read-chunk size, never the upload size.

Safety: the caller bounds the body with :class:`BoundedReader` (so a lying or
oversized ``Content-Length`` cannot exhaust memory), filenames are reduced to a
single path component, and overwrites are refused unless explicitly allowed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import tempfile
from typing import Protocol

_CHUNK = 64 * 1024


class UploadError(Exception):
    """The multipart body was malformed or unsafe."""


class UploadConflictError(UploadError):
    """A target file already exists and overwriting is not allowed."""


@dataclasses.dataclass(frozen=True, slots=True)
class SavedFile:
    """A file that was written to disk."""

    filename: str
    size: int


class _Sink(Protocol):
    def write(self, data: bytes, /) -> int: ...


class _Discard:
    """A sink that drops everything (used for non-file form fields)."""

    def write(self, data: bytes, /) -> int:
        return len(data)


class BoundedReader:
    """Reads at most ``limit`` bytes from an underlying binary stream."""

    def __init__(self, stream: _ReadableStream, limit: int) -> None:
        self._stream = stream
        self._remaining = limit

    def read(self, size: int) -> bytes:
        if self._remaining <= 0:
            return b""
        chunk = self._stream.read(min(size, self._remaining))
        self._remaining -= len(chunk)
        return chunk

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


class _ReadableStream(Protocol):
    def read(self, size: int, /) -> bytes: ...


class _Stream:
    """Buffered line/delimiter reader over a chunked byte stream."""

    def __init__(self, reader: _ReadableStream) -> None:
        self._reader = reader
        self._buf = b""

    def _fill(self) -> bool:
        data = self._reader.read(_CHUNK)
        if not data:
            return False
        self._buf += data
        return True

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            if not self._fill():
                line, self._buf = self._buf, b""
                return line
        line, _, self._buf = self._buf.partition(b"\n")
        return line + b"\n"

    def read_until(self, marker: bytes, dest: _Sink) -> int:
        """Write bytes to ``dest`` until ``marker``; consume it. Returns bytes written."""
        written = 0
        keep = len(marker) - 1
        while True:
            index = self._buf.find(marker)
            if index != -1:
                written += dest.write(self._buf[:index])
                self._buf = self._buf[index + len(marker) :]
                return written
            if len(self._buf) > keep:
                cut = len(self._buf) - keep
                written += dest.write(self._buf[:cut])
                self._buf = self._buf[cut:]
            if not self._fill():
                raise UploadError("unterminated multipart part")


def extract_boundary(content_type: str) -> bytes | None:
    """Pull the boundary token out of a ``multipart/form-data`` Content-Type."""
    for parameter in content_type.split(";"):
        parameter = parameter.strip()
        if parameter.startswith("boundary="):
            value = parameter[len("boundary=") :].strip().strip('"')
            if value:
                return value.encode("latin-1")
    return None


def _read_headers(stream: _Stream) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    while True:
        line = stream.readline().rstrip(b"\r\n")
        if not line:
            return headers
        name, _, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()


def _disposition_filename(headers: dict[bytes, bytes]) -> str | None:
    disposition = headers.get(b"content-disposition", b"").decode("latin-1", "replace")
    for parameter in disposition.split(";"):
        parameter = parameter.strip()
        if parameter.startswith("filename="):
            return parameter[len("filename=") :].strip().strip('"')
    return None


def _safe_name(filename: str) -> str | None:
    # Reduce to a single component: a client may send "../x" or "C:\\x".
    name = os.path.basename(filename.replace("\\", "/"))
    if name in {"", ".", ".."} or "\x00" in name:
        return None
    return name


def _save_part(stream: _Stream, marker: bytes, dest_dir: str, name: str, *, overwrite: bool) -> int:
    final = os.path.join(dest_dir, name)
    if os.path.exists(final) and not overwrite:
        # Drain this part so the stream stays aligned, then signal the conflict.
        stream.read_until(marker, _Discard())
        raise UploadConflictError(name)
    tmp = tempfile.NamedTemporaryFile(dir=dest_dir, delete=False)  # noqa: SIM115 (closed before os.replace)
    try:
        written = stream.read_until(marker, tmp)
        tmp.close()
        os.replace(tmp.name, final)
    except BaseException:
        tmp.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise
    return written


def save(
    reader: _ReadableStream,
    boundary: bytes,
    dest_dir: str,
    *,
    allow_overwrite: bool = False,
) -> list[SavedFile]:
    """Parse a multipart body and write its file parts into ``dest_dir``."""
    stream = _Stream(reader)
    delimiter = b"--" + boundary
    if stream.readline().rstrip(b"\r\n") != delimiter:
        raise UploadError("missing initial multipart boundary")

    saved: list[SavedFile] = []
    marker = b"\r\n" + delimiter
    while True:
        headers = _read_headers(stream)
        filename = _disposition_filename(headers)
        if filename is not None:
            name = _safe_name(filename)
            if name is None:
                raise UploadError("unsafe upload filename")
            written = _save_part(stream, marker, dest_dir, name, overwrite=allow_overwrite)
            saved.append(SavedFile(name, written))
        else:
            stream.read_until(marker, _Discard())
        trailer = stream.readline()
        if trailer.startswith(b"--") or trailer == b"":
            return saved
