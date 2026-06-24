"""WebDAV (RFC 4918) read/write — opt-in via ``--dav`` / ``--dav-write``.

Enough of WebDAV to MOUNT the share as a network drive (macOS Finder, Windows
Explorer, Linux gio/davfs2): OPTIONS, PROPFIND, PUT, DELETE, MKCOL, MOVE, COPY,
PROPPATCH, and a *stub* LOCK/UNLOCK (advertise class 2 so clients mount read-write;
the lock stores no state — the industry norm). Pure stdlib; reuses servery's
path-safety, ETag, and atomic-write primitives. The handler's thin ``do_*`` methods
dispatch here.

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
import urllib.parse
import uuid
import xml.etree.ElementTree as ET  # nosec B405 - serialize-only; see module docstring
from typing import TYPE_CHECKING

from servery import _http1, _response, security
from servery._conditional import make_etag

if TYPE_CHECKING:
    from servery.handler import ServeryHandler

_DAV = "DAV:"
ET.register_namespace("D", _DAV)  # process-global; emit "D:" prefixes, not "ns0:"

_ALLOW_RO = "OPTIONS, GET, HEAD, PROPFIND, LOCK, UNLOCK"
_ALLOW_RW = (
    "OPTIONS, GET, HEAD, POST, PUT, DELETE, PROPFIND, PROPPATCH, MKCOL, COPY, MOVE, LOCK, UNLOCK"
)


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


def _prop_response(href: str, fs_path: str) -> ET.Element:
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
    for scope in ("exclusive", "shared"):
        entry = ET.SubElement(supportedlock, _q("lockentry"))
        ET.SubElement(ET.SubElement(entry, _q("lockscope")), _q(scope))
        ET.SubElement(ET.SubElement(entry, _q("locktype")), _q("write"))
    ET.SubElement(prop, _q("lockdiscovery"))  # empty: we don't track locks
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
        for name in sorted(os.listdir(fs_path)):
            child = os.path.join(fs_path, name)
            href = base.rstrip("/") + "/" + urllib.parse.quote(name)
            entries.append((href, child))
    multistatus = ET.Element(_q("multistatus"))
    for href, path in entries:
        try:
            multistatus.append(_prop_response(href, path))
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
    _read_body(handler)  # consume the propertyupdate body
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
        handler.send_error(415)
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
    existed = os.path.exists(fs_path)
    if existed and not handler._server.config.allow_overwrite:
        handler.send_error(412)
        return
    if not _write_file(handler, fs_path, parent):
        return
    _send(handler, 204 if existed else 201)


def delete(handler: ServeryHandler) -> None:
    fs_path = handler.translate_path(handler.path)
    if not fs_path or not os.path.exists(fs_path):
        handler.send_error(404)
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
    # Stub lock: hand out a token, enforce nothing. Class 2 is advertised so clients
    # mount read-write; real lock state is intentionally not kept (RFC 4918 §6.6 lets
    # the server decline to honor the timeout; minimal servers everywhere do this).
    _read_body(handler)
    token = f"opaquelocktoken:{uuid.uuid4()}"
    prop = ET.Element(_q("prop"))
    activelock = ET.SubElement(ET.SubElement(prop, _q("lockdiscovery")), _q("activelock"))
    ET.SubElement(ET.SubElement(activelock, _q("locktype")), _q("write"))
    ET.SubElement(ET.SubElement(activelock, _q("lockscope")), _q("exclusive"))
    ET.SubElement(activelock, _q("depth")).text = "infinity"
    ET.SubElement(activelock, _q("timeout")).text = "Second-3600"
    ET.SubElement(ET.SubElement(activelock, _q("locktoken")), _q("href")).text = token
    ET.SubElement(ET.SubElement(activelock, _q("lockroot")), _q("href")).text = handler.path
    _send(
        handler,
        200,
        body=_serialize(prop),
        ctype='application/xml; charset="utf-8"',
        extra=[("Lock-Token", f"<{token}>")],
    )


def unlock(handler: ServeryHandler) -> None:
    _send(handler, 204)


def _read_body(handler: ServeryHandler) -> bytes:
    try:
        length = max(0, int(handler.headers.get("Content-Length", "0") or 0))
    except ValueError:  # pragma: no cover - defensive against a malformed header
        return b""
    return (
        handler.rfile.read(min(length, handler._server.config.max_upload_size)) if length else b""
    )


def _write_file(handler: ServeryHandler, fs_path: str, parent: str) -> bool:
    """Stream the request body to ``fs_path`` atomically; return False (+ error sent) on failure."""
    from servery import upload

    try:
        length = max(0, int(handler.headers.get("Content-Length", "0") or 0))
    except ValueError:  # pragma: no cover - defensive against a malformed header
        handler.send_error(400, "Invalid Content-Length")
        return False
    if length > handler._server.config.max_upload_size:
        handler.send_error(413)
        return False
    reader = upload.BoundedReader(handler.rfile, length)
    tmp = tempfile.NamedTemporaryFile(dir=parent, delete=False)  # noqa: SIM115 (closed before replace)
    try:
        while chunk := reader.read(65536):
            tmp.write(chunk)
        tmp.close()
        os.replace(tmp.name, fs_path)
    except OSError:  # pragma: no cover - disk/permission failure mid-write
        tmp.close()
        with contextlib.suppress(OSError):
            os.remove(tmp.name)
        handler.send_error(500, "Write failed")
        return False
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
