"""CGI/1.1 hosting (RFC 3875) — opt-in via ``--cgi DIR``.

``DIR`` is a cgi-bin: a request maps to a script inside it (longest path prefix
that is an executable file), the remainder becomes ``PATH_INFO``, and the script
runs as a child process per RFC 3875. This is the highest-risk dynamic handler —
it executes code — so it is off by default and the security mitigations below are
not optional.

Security model (RFC 3875 §9 + the well-known CGI CVEs):

* **httpoxy** (CVE-2016-5385): the ``Proxy`` request header must never reach the
  child as ``HTTP_PROXY`` — it is dropped, and the child gets a clean, minimal
  environment (never the server's), so an inherited proxy can't leak either.
* **Authorization** (§9.2): ``Authorization`` / ``Proxy-Authorization`` are never
  forwarded — servery validated Basic auth itself.
* **Shellshock** (CVE-2014-6271): the script is exec'd directly (``shell=False``),
  so crafted env values are never parsed as shell function definitions.
* **Path traversal** (§9.8): the resolved script + ``PATH_INFO`` must stay inside
  ``DIR`` (servery's realpath/commonpath containment).
* **Resource limits** (§9.6): the request body is bounded and the child has a
  hard timeout.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 (executing CGI scripts is the whole point of --cgi)
import sys
from pathlib import Path

from servery import _http1, _log, security
from servery.handler import ServeryHandler

# RFC 3875 §9.2 + httpoxy: request headers that must never become CGI meta-vars.
_BLOCKED_HEADERS = frozenset({"authorization", "proxy-authorization", "proxy"})
_TIMEOUT: float = 30.0
_MAX_BODY: int = 100 * 1024 * 1024


def resolve_script(cgi_root: str, url_path: str) -> tuple[str, str] | None:
    """Split ``url_path`` into (script_path, PATH_INFO) within ``cgi_root``.

    Returns the longest leading path that is a contained, regular file, with the
    remainder as the (un-encoded) PATH_INFO. ``None`` if no script matches.
    """
    parts = security.safe_segments(url_path)
    for i in range(len(parts), 0, -1):
        candidate = os.path.join(cgi_root, *parts[:i])
        if os.path.isfile(candidate) and security.is_contained(cgi_root, candidate):
            rest = parts[i:]
            return candidate, ("/" + "/".join(rest) if rest else "")
    return None


def build_env(handler: ServeryHandler, script: str, path_info: str) -> dict[str, str]:
    """Build the RFC 3875 §4.1 meta-variable environment (clean + filtered)."""
    _, _, query = handler.path.partition("?")
    headers = handler.headers
    server_host, server_port = handler.server.server_address[:2]  # ty: ignore[not-subscriptable]
    env = {
        "GATEWAY_INTERFACE": "CGI/1.1",
        "SERVER_SOFTWARE": handler.version_string(),
        "SERVER_PROTOCOL": handler.request_version or "HTTP/1.1",
        "SERVER_NAME": str(server_host),
        "SERVER_PORT": str(server_port),
        "REQUEST_METHOD": handler.command or "GET",
        "SCRIPT_NAME": "/" + os.path.relpath(script, handler._server.cgi_root).replace(os.sep, "/"),
        "PATH_INFO": path_info,
        "QUERY_STRING": query,
        "REMOTE_ADDR": handler.client_address[0],
        "REMOTE_PORT": str(handler.client_address[1]),
        "PATH": "/usr/bin:/bin",  # minimal, fixed
    }
    content_type = headers.get("content-type")
    if content_type:
        env["CONTENT_TYPE"] = content_type
    length = headers.get("content-length")
    if length:
        env["CONTENT_LENGTH"] = length
    for name, value in headers.items():
        if name.lower() in _BLOCKED_HEADERS:
            continue
        env["HTTP_" + name.upper().replace("-", "_")] = value
    return env


def _argv(script: str) -> list[str]:
    # .py always runs with the current interpreter (portable; no shebang needed,
    # works on Windows). Anything else must be a real executable (shebang + bit).
    if script.endswith(".py"):
        return [sys.executable, script]
    return [script]


def run(handler: ServeryHandler) -> None:
    """Resolve + execute the CGI script for this request and relay the response."""
    cgi_root = handler._server.cgi_root
    resolved = resolve_script(cgi_root, handler.path)
    if resolved is None:
        handler.send_error(404, "No CGI script found")
        return
    script, path_info = resolved
    length = max(0, min(int(handler.headers.get("content-length") or 0), _MAX_BODY))
    body = handler.rfile.read(length) if length else b""
    env = build_env(handler, script, path_info)
    try:
        proc = subprocess.run(  # nosec B603 (argv list, shell=False, clean minimal env)
            _argv(script),
            input=body,
            env=env,
            capture_output=True,
            timeout=_TIMEOUT,
            cwd=str(Path(script).parent),
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.logger.warning("CGI script %s timed out after %ss", script, _TIMEOUT)
        handler.send_error(504, "CGI script timed out")
        return
    except OSError as exc:
        _log.logger.error("CGI script %s could not be executed: %s", script, exc)
        handler.send_error(502, "CGI script could not be executed")
        return
    if proc.returncode != 0:
        # Surface the script's own diagnostics — the usual reason a CGI 500s.
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        _log.logger.warning(
            "CGI script %s exited %s%s", script, proc.returncode, f": {stderr}" if stderr else ""
        )
        if not proc.stdout:
            handler.send_error(502, "CGI script error")
            return
    _relay(handler, proc.stdout)


def _relay(handler: ServeryHandler, output: bytes) -> None:
    """Parse the CGI response (RFC 3875 §6) and write it to the client."""
    head, sep, body = output.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = output.partition(b"\n\n")
    status = "200 OK"
    out_headers: list[tuple[str, str]] = []
    for raw in head.split(b"\n"):
        line = raw.rstrip(b"\r").decode("latin-1")
        name, colon, value = line.partition(":")
        if not colon:
            continue
        name, value = name.strip(), value.strip()
        low = name.lower()
        if low == "status":
            status = value
        elif low == "location" and not value.startswith(("http://", "https://", "/")):
            out_headers.append((name, value))
        elif low == "location":
            status = "302 Found"
            out_headers.append((name, value))
        else:
            out_headers.append((name, value))
    blob, _ = _http1.build_head(
        version=handler.protocol_version,
        status=status,
        headers=out_headers,
        is_head=handler.command == "HEAD",
        keep_alive=not handler.close_connection,
        server=handler.version_string(),
        date=handler.date_time_string(),
        default_content_type="text/plain",
        body_len=len(body),
    )
    handler.wfile.write(blob if handler.command == "HEAD" else blob + body)
    handler.log_request(status.split(" ", 1)[0])


class CGIHandler(ServeryHandler):
    """Routes every request to a CGI script under the configured cgi directory."""

    def handle(self) -> None:
        super(ServeryHandler, self).handle()  # HTTP/1.1 only; skip h2 dispatch

    def _run_cgi(self) -> None:
        if not self._authorized():  # --auth gates the script, like file serving
            return
        run(self)

    def do_GET(self) -> None:
        self._run_cgi()

    def do_HEAD(self) -> None:
        self._run_cgi()

    def do_POST(self) -> None:
        self._run_cgi()

    def do_PUT(self) -> None:
        self._run_cgi()

    def do_DELETE(self) -> None:
        self._run_cgi()

    def do_PATCH(self) -> None:
        self._run_cgi()
