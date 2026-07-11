"""WebDAV (RFC 4918) read/write — opt-in via ``--dav`` / ``--dav-write``.

Enough of WebDAV to MOUNT the share as a network drive (macOS Finder, Windows
Explorer, Linux gio/davfs2): OPTIONS, PROPFIND, PUT, DELETE, MKCOL, MOVE, COPY,
PROPPATCH, and configurable LOCK/UNLOCK behavior. Read-only mode is honest DAV
class 1; writable mode can stay class 1, return compatibility tokens, or enforce
in-process exclusive locks. Pure stdlib; reuses servery's path-safety, ETag, and
atomic-write primitives. The handler's thin ``do_*`` methods dispatch here.

XML safety: ``xml.etree.ElementTree`` is used **only to serialize** responses
(``Element``/``SubElement``/``tostring``). servery never *parses* a request body as
XML — request bodies are consumed as opaque bytes — so the XML-attack surface bandit
warns about (B405: entity expansion / external entities) does not exist here.
``defusedxml`` would be the alternative, but it is a third-party dependency and the
core stays zero-dependency, so B405 is suppressed on the import below.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - serialize-only; see module docstring
from dataclasses import dataclass
from typing import TYPE_CHECKING

from servery import _body, _http1, _response, security
from servery._conditional import make_etag

if TYPE_CHECKING:
    from servery.config import Config
    from servery.handler import ServeryHandler

_DAV = "DAV:"
ET.register_namespace("D", _DAV)  # process-global; emit "D:" prefixes, not "ns0:"

_ALLOW_RO = "OPTIONS, GET, HEAD, PROPFIND, LOCK, UNLOCK"
_ALLOW_RW = (
    "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK"
)


@dataclass(frozen=True, slots=True)
class DavLock:
    path: str
    token: str
    expires: float
    owner: str = ""


class DavLockManager:
    """In-memory exclusive WebDAV locks for servery's single-process model."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, DavLock] = {}

    def _purge(self, now: float) -> None:
        for token, record in tuple(self._locks.items()):
            if record.expires <= now:
                self._locks.pop(token, None)

    @staticmethod
    def _covers(root: str, path: str) -> bool:
        return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

    def acquire(self, path: str, timeout: int, owner: str = "") -> DavLock | None:
        now = time.time()
        canonical = os.path.realpath(path)
        with self._guard:
            self._purge(now)
            if any(
                self._covers(record.path, canonical) or self._covers(canonical, record.path)
                for record in self._locks.values()
            ):
                return None
            token = f"opaquelocktoken:{uuid.uuid4()}"
            record = DavLock(canonical, token, now + timeout, owner)
            self._locks[token] = record
            return record

    def release(self, token: str, path: str) -> bool:
        with self._guard:
            self._purge(time.time())
            record = self._locks.get(token)
            if record is None or record.path != os.path.realpath(path):
                return False
            self._locks.pop(token, None)
            return True

    def refresh(self, submitted: str, path: str, timeout: int) -> DavLock | None:
        now = time.time()
        canonical = os.path.realpath(path)
        with self._guard:
            self._purge(now)
            for token, record in self._locks.items():
                if token in submitted and record.path == canonical:
                    refreshed = DavLock(record.path, token, now + timeout, record.owner)
                    self._locks[token] = refreshed
                    return refreshed
        return None

    def discover(self, path: str) -> list[DavLock]:
        canonical = os.path.realpath(path)
        with self._guard:
            self._purge(time.time())
            return [
                record for record in self._locks.values() if self._covers(record.path, canonical)
            ]

    def authorized(self, paths: list[str], submitted: str) -> bool:
        now = time.time()
        canonical = [os.path.realpath(path) for path in paths]
        with self._guard:
            self._purge(now)
            required = {
                record.token
                for record in self._locks.values()
                if any(
                    self._covers(record.path, path) or self._covers(path, record.path)
                    for path in canonical
                )
            }
        return all(token in submitted for token in required)


def allow_header(config: Config) -> str:
    writable = config.dav_write
    mode = _effective_lock_mode(config)
    allow = _ALLOW_RW if writable else _ALLOW_RO
    if mode == "class1":
        allow = ", ".join(
            method for method in allow.split(", ") if method not in {"LOCK", "UNLOCK"}
        )
    return allow


def dav_class(config: Config) -> str:
    return "1" if _effective_lock_mode(config) == "class1" else "1, 2"


def _effective_lock_mode(config: Config) -> str:
    # A read-only collection cannot perform lock-protected writes, so claiming
    # class 2 would add no useful guarantee and can mislead clients.
    return config.dav_lock_mode if config.dav_write else "class1"


def _q(tag: str) -> str:
    return f"{{{_DAV}}}{tag}"


def _http_date(mtime: float) -> str:
    return _http1.format_http_date(mtime)


def _iso_date(ctime: float) -> str:
    return datetime.datetime.fromtimestamp(ctime, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _send(
    handler: ServeryHandler,
    status: int,
    *,
    body: bytes = b"",
    ctype: str | None = None,
    extra: list[tuple[str, str]] | None = None,
) -> None:
    handler.send_response(status)
    if ctype is not None:
        handler.send_header("Content-Type", ctype)
    for name, value in extra or []:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body and handler.command != "HEAD":
        handler.wfile.write(body)


def _dav_error(handler: ServeryHandler, status: int, condition: str) -> None:
    err = ET.Element(_q("error"))
    ET.SubElement(err, _q(condition))
    _send(handler, status, body=_serialize(err), ctype='application/xml; charset="utf-8"')


def _serialize(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _submitted_tokens(handler: ServeryHandler) -> str:
    return " ".join(
        filter(None, (handler.headers.get("If", ""), handler.headers.get("Lock-Token", "")))
    )


def _locks_allow(handler: ServeryHandler, paths: list[str]) -> bool:
    if _effective_lock_mode(handler._server.config) != "enforced":
        return True
    if handler._server.dav_locks.authorized(paths, _submitted_tokens(handler)):
        return True
    handler.send_error(423, "Resource is locked")
    return False


def _append_active_lock(parent: ET.Element, record: DavLock, href: str) -> None:
    active = ET.SubElement(parent, _q("activelock"))
    ET.SubElement(ET.SubElement(active, _q("locktype")), _q("write"))
    ET.SubElement(ET.SubElement(active, _q("lockscope")), _q("exclusive"))
    ET.SubElement(active, _q("depth")).text = "infinity"
    remaining = max(1, int(record.expires - time.time()))
    ET.SubElement(active, _q("timeout")).text = f"Second-{remaining}"
    ET.SubElement(ET.SubElement(active, _q("locktoken")), _q("href")).text = record.token
    ET.SubElement(ET.SubElement(active, _q("lockroot")), _q("href")).text = href
    if record.owner:
        ET.SubElement(active, _q("owner")).text = record.owner


def _prop_response(handler: ServeryHandler, href: str, fs_path: str) -> ET.Element:
    """One <response> with the live properties for ``fs_path`` at URL ``href``."""
    stat = os.stat(fs_path)
    is_dir = os.path.isdir(fs_path)
    response = ET.Element(_q("response"))
    href_text = href + "/" if (is_dir and not href.endswith("/")) else href
    ET.SubElement(response, _q("href")).text = href_text
    propstat = ET.SubElement(response, _q("propstat"))
    prop = ET.SubElement(propstat, _q("prop"))
    resourcetype = ET.SubElement(prop, _q("resourcetype"))
    if is_dir:
        ET.SubElement(resourcetype, _q("collection"))
    else:
        ET.SubElement(prop, _q("getcontentlength")).text = str(stat.st_size)
        ET.SubElement(prop, _q("getcontenttype")).text = _response.guess_type(fs_path)
        ET.SubElement(prop, _q("getetag")).text = make_etag(stat)
    ET.SubElement(prop, _q("displayname")).text = os.path.basename(href.rstrip("/")) or "/"
    ET.SubElement(prop, _q("getlastmodified")).text = _http_date(stat.st_mtime)
    ET.SubElement(prop, _q("creationdate")).text = _iso_date(stat.st_ctime)
    supportedlock = ET.SubElement(prop, _q("supportedlock"))
    mode = _effective_lock_mode(handler._server.config)
    for scope in () if mode == "class1" else ("exclusive",):
        entry = ET.SubElement(supportedlock, _q("lockentry"))
        ET.SubElement(ET.SubElement(entry, _q("lockscope")), _q(scope))
        ET.SubElement(ET.SubElement(entry, _q("locktype")), _q("write"))
    discovery = ET.SubElement(prop, _q("lockdiscovery"))
    if mode == "enforced":
        for record in handler._server.dav_locks.discover(fs_path):
            _append_active_lock(discovery, record, href)
    ET.SubElement(propstat, _q("status")).text = "HTTP/1.1 200 OK"
    return response


def propfind(handler: ServeryHandler) -> None:
    fs_path = handler.translate_path(handler.path)
    if not fs_path or not os.path.exists(fs_path):
        handler.send_error(404)
        return
    depth = handler.headers.get("Depth", "1")
    if depth == "infinity":  # bound the DoS (RFC 4918 §9.1.1); clients only need 0/1
        _dav_error(handler, 403, "propfind-finite-depth")
        return
    base = handler.path.split("?", 1)[0]
    entries = [(base, fs_path)]
    if depth == "1" and os.path.isdir(fs_path):
        names: list[str] = []
        with os.scandir(fs_path) as iterator:
            for entry in iterator:
                names.append(entry.name)
                if len(names) > handler._server.config.max_propfind_entries:
                    handler.send_error(507, "Collection exceeds the PROPFIND entry limit")
                    return
        for name in sorted(names):
            child = os.path.join(fs_path, name)
            href = base.rstrip("/") + "/" + urllib.parse.quote(name)
            entries.append((href, child))
    multistatus = ET.Element(_q("multistatus"))
    for href, path in entries:
        try:
            multistatus.append(_prop_response(handler, href, path))
        except OSError:
            continue  # vanished between listdir and stat — skip
    _send(handler, 207, body=_serialize(multistatus), ctype='application/xml; charset="utf-8"')


def proppatch(handler: ServeryHandler) -> None:
    # Accept-and-discard: Windows/Office SET Win32 dead props and roll back the whole
    # copy on any failure, so report 200 for each without persisting (RFC 4918 §9.2).
    fs_path = handler.translate_path(handler.path)
    if not fs_path or not os.path.exists(fs_path):
        handler.send_error(404)
        return
    if not _locks_allow(handler, [fs_path]):
        return
    if _read_body(handler) is None:  # consume the propertyupdate body
        return
    resp = ET.Element(_q("response"))
    ET.SubElement(resp, _q("href")).text = handler.path.split("?", 1)[0]
    propstat = ET.SubElement(resp, _q("propstat"))
    ET.SubElement(propstat, _q("prop"))
    ET.SubElement(propstat, _q("status")).text = "HTTP/1.1 200 OK"
    multistatus = ET.Element(_q("multistatus"))
    multistatus.append(resp)
    _send(handler, 207, body=_serialize(multistatus), ctype='application/xml; charset="utf-8"')


def mkcol(handler: ServeryHandler) -> None:
    if handler.headers.get("Content-Length", "0") not in ("0", ""):
        handler._reject_unread_body(415, "MKCOL does not accept a request body")
        return
    fs_path = handler.translate_path(handler.path)
    if not fs_path:
        handler.send_error(409)
        return
    if os.path.exists(fs_path):
        handler.send_error(405)
        return
    if not os.path.isdir(os.path.dirname(fs_path)):
        handler.send_error(409)  # MUST NOT create intermediate collections (§9.3)
        return
    if not _locks_allow(handler, [fs_path]):
        return
    with handler._server.target_locks.hold(
        fs_path, handler._server.config.write_lock_timeout
    ) as acquired:
        if not acquired:
            handler.send_error(409, "Another write to this target is active")
            return
        try:
            os.mkdir(fs_path)
        except OSError:
            handler.send_error(409)
            return
    _send(handler, 201)


def put(handler: ServeryHandler) -> None:
    fs_path = handler.translate_path(handler.path)
    if not fs_path:
        handler.send_error(409)
        return
    if os.path.isdir(fs_path):
        handler.send_error(405)  # can't PUT over a collection (§9.7.2)
        return
    parent = os.path.dirname(fs_path)
    if not os.path.isdir(parent):
        handler.send_error(409)  # missing parent collection (§9.7.1)
        return
    if not _locks_allow(handler, [fs_path]):
        return
    with handler._server.target_locks.hold(
        fs_path, handler._server.config.write_lock_timeout
    ) as acquired:
        if not acquired:
            handler._reject_unread_body(409, "Another write to this target is active")
            return
        existed = os.path.exists(fs_path)
        if existed and not handler._server.config.allow_overwrite:
            handler._reject_unread_body(412, "Overwrite is disabled")
            return
        if not _write_file(handler, fs_path, parent):
            return
    _send(handler, 204 if existed else 201)


def delete(handler: ServeryHandler) -> None:
    fs_path = handler.translate_path(handler.path)
    if not fs_path or not os.path.exists(fs_path):
        handler.send_error(404)
        return
    if not _locks_allow(handler, [fs_path]):
        return
    with handler._server.target_locks.hold(
        fs_path, handler._server.config.write_lock_timeout
    ) as acquired:
        if not acquired:
            handler.send_error(409, "Another write to this target is active")
            return
        try:
            if os.path.isdir(fs_path):
                shutil.rmtree(fs_path)  # collections delete Depth-infinity (§9.6.1)
            else:
                os.remove(fs_path)
        except OSError:  # pragma: no cover - permission failure on an existing path
            handler.send_error(403)
            return
    _send(handler, 204)


def _destination(handler: ServeryHandler) -> str | None:
    """The contained filesystem path of the COPY/MOVE Destination header, or None."""
    dest = handler.headers.get("Destination")
    if not dest:
        return None
    parts = urllib.parse.urlsplit(dest)
    path = urllib.parse.unquote(parts.path)
    return security.safe_join(handler._server.root_real, path)  # same containment as GET


def _transfer(handler: ServeryHandler, *, move: bool) -> None:
    src = handler.translate_path(handler.path)
    if not src or not os.path.exists(src):
        handler.send_error(404)
        return
    dst = _destination(handler)
    if dst is None:
        handler.send_error(400)  # missing/cross-host/escaping Destination (§10.3)
        return
    if os.path.realpath(src) == os.path.realpath(dst):
        handler.send_error(403)
        return
    dest_exists = os.path.exists(dst)
    if dest_exists and handler.headers.get("Overwrite", "T").upper() == "F":
        handler.send_error(412)
        return
    if not os.path.isdir(os.path.dirname(dst)):
        handler.send_error(409)
        return
    lock_paths = [src, dst] if move else [dst]
    if not _locks_allow(handler, lock_paths):
        return
    with handler._server.target_locks.hold_many(
        [src, dst], handler._server.config.write_lock_timeout
    ) as acquired:
        if not acquired:
            handler.send_error(409, "Another write to this target is active")
            return
        dest_exists = os.path.exists(dst)
        if dest_exists and handler.headers.get("Overwrite", "T").upper() == "F":
            handler.send_error(412)
            return
        try:
            if dest_exists:
                shutil.rmtree(dst) if os.path.isdir(dst) else os.remove(dst)
            if move:
                shutil.move(src, dst)
            elif os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except OSError:  # pragma: no cover - filesystem failure mid-copy/move
            handler.send_error(409)
            return
    _send(handler, 204 if dest_exists else 201)


def copy(handler: ServeryHandler) -> None:
    _transfer(handler, move=False)


def move(handler: ServeryHandler) -> None:
    _transfer(handler, move=True)


def lock(handler: ServeryHandler) -> None:
    mode = _effective_lock_mode(handler._server.config)
    if mode == "class1":
        handler.send_error(405, "WebDAV locking is disabled")
        return
    fs_path = handler.translate_path(handler.path)
    if not fs_path or not os.path.exists(fs_path):
        handler.send_error(404)
        return
    body = _read_body(handler)
    if body is None:
        return
    timeout = _lock_timeout(handler.headers.get("Timeout", "Second-3600"))
    owner = body.decode("utf-8", "replace")[:1024]
    if mode == "enforced":
        record = (
            handler._server.dav_locks.refresh(_submitted_tokens(handler), fs_path, timeout)
            if not body
            else handler._server.dav_locks.acquire(fs_path, timeout, owner)
        )
        if record is None:
            handler.send_error(423, "Resource is already locked or the refresh token is invalid")
            return
    else:
        record = DavLock(
            os.path.realpath(fs_path),
            f"opaquelocktoken:{uuid.uuid4()}",
            time.time() + timeout,
            owner,
        )
    prop = ET.Element(_q("prop"))
    _append_active_lock(ET.SubElement(prop, _q("lockdiscovery")), record, handler.path)
    _send(
        handler,
        200,
        body=_serialize(prop),
        ctype='application/xml; charset="utf-8"',
        extra=[("Lock-Token", f"<{record.token}>")],
    )


def unlock(handler: ServeryHandler) -> None:
    mode = _effective_lock_mode(handler._server.config)
    if mode == "class1":
        handler.send_error(405, "WebDAV locking is disabled")
        return
    if mode == "enforced":
        fs_path = handler.translate_path(handler.path)
        token = handler.headers.get("Lock-Token", "").strip("<>")
        if not fs_path or not token or not handler._server.dav_locks.release(token, fs_path):
            handler.send_error(409, "Unknown lock token")
            return
    _send(handler, 204)


def _lock_timeout(value: str) -> int:
    if value.lower() == "infinite":
        return 3600
    try:
        seconds = int(value.split("-", 1)[1])
    except (IndexError, ValueError):
        return 3600
    return max(1, min(seconds, 24 * 60 * 60))


def _read_body(handler: ServeryHandler) -> bytes | None:
    length = handler._body_plan.length or 0
    if length > handler._server.config.max_request_body:
        handler._reject_unread_body(413, "Request body exceeds the size limit")
        return None
    reader = _body.LimitedReader(handler._request_body_stream(), length)
    body = reader.read()
    if reader.remaining:
        handler.close_connection = True
        return None
    handler._body_consumed()
    return body


def _write_file(handler: ServeryHandler, fs_path: str, parent: str) -> bool:
    """Stream the request body to ``fs_path`` atomically; return False (+ error sent) on failure."""

    length = handler._body_plan.length or 0
    if length > handler._server.config.max_upload_size:
        handler._reject_unread_body(413, "Upload exceeds the size limit")
        return False
    reader = _body.LimitedReader(handler._request_body_stream(), length)
    tmp = tempfile.NamedTemporaryFile(dir=parent, delete=False)  # noqa: SIM115 (closed before replace)
    try:
        while chunk := reader.read(65536):
            tmp.write(chunk)
        tmp.close()
        os.replace(tmp.name, fs_path)
    except _body.BodyTimeoutError:
        tmp.close()
        with contextlib.suppress(OSError):
            os.remove(tmp.name)
        handler.close_connection = True
        raise
    except OSError:  # pragma: no cover - disk/permission failure mid-write
        tmp.close()
        with contextlib.suppress(OSError):
            os.remove(tmp.name)
        handler.send_error(500, "Write failed")
        handler.close_connection = True
        return False
    if reader.remaining:
        handler.close_connection = True
        return False
    handler._body_consumed()
    return True


def dispatch(handler: ServeryHandler, op: str) -> None:
    {
        "propfind": propfind,
        "proppatch": proppatch,
        "mkcol": mkcol,
        "put": put,
        "delete": delete,
        "copy": copy,
        "move": move,
        "lock": lock,
        "unlock": unlock,
    }[op](handler)
