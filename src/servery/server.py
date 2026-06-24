"""The servery HTTP server.

``ServeryHTTPServer`` is a threading server (one thread per connection); its
configuration is an immutable :class:`~servery.config.Config`, and the resolved
root real-path is computed once so the per-request containment check is cheap.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from servery import _log, _tls, auth
from servery.config import Config
from servery.handler import ServeryHandler


class ServeryHTTPServer(ThreadingHTTPServer):
    """Threading HTTP/1.1 server bound to a :class:`Config`."""

    daemon_threads = True
    allow_reuse_address = True
    # Listen backlog (socketserver default is 5): too shallow for connection
    # bursts (e.g. many short non-keep-alive clients), which then get refused
    # before a worker can accept. 128 absorbs bursts without unbounded queueing.
    request_queue_size = 128

    wsgi_app: Any = None
    cgi_root: str = ""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root_real = os.path.realpath(config.directory)
        self.credential = auth.parse(config.auth)
        self.access_log = None
        if config.access_log:
            from servery import _accesslog

            self.access_log = _accesslog.AccessLog(config.access_log, config.access_log_format)
        self._executor = (
            ThreadPoolExecutor(max_workers=config.max_workers) if config.max_workers else None
        )
        # Bound accepted-but-queued connections too, not just running workers, so
        # a flood can't grow the executor queue (and held sockets) without limit.
        self._slots = threading.Semaphore(config.max_workers * 4) if config.max_workers else None
        if ":" in config.host:
            self.address_family = socket.AF_INET6
        # Opt-in dynamic handlers replace file serving entirely (loaded up front
        # so an import error surfaces at startup, not mid-request).
        self._handler_cls: type[ServeryHandler] = ServeryHandler
        if config.wsgi_app:
            from servery import wsgi

            self.wsgi_app = wsgi.load_app(config.wsgi_app)
            self._handler_cls = wsgi.WSGIHandler
        elif config.cgi_dir:
            from servery import cgi

            self.cgi_root = os.path.realpath(config.cgi_dir)
            if not Path(self.cgi_root).is_dir():
                raise ValueError(f"--cgi: {config.cgi_dir!r} is not a directory")
            self._handler_cls = cgi.CGIHandler
        super().__init__((config.host, config.port), self._handler_cls)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Default: a thread per connection (ThreadingMixIn). With --max-workers,
        # bound concurrency through a shared pool instead.
        if self._executor is not None and self._slots is not None:
            self._slots.acquire()
            self._executor.submit(self._process_request_pooled, request, client_address)
        else:
            super().process_request(request, client_address)

    def _process_request_pooled(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # mirror ThreadingMixIn: never let a worker thread die
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            if self._slots is not None:
                self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A failed TLS handshake or a dropped connection is a client-side problem,
        # not a server fault — don't spew a traceback for every old/scanning peer
        # (still visible at DEBUG). Anything else is a real bug: route it through
        # our logger (with traceback) rather than socketserver's raw stderr print.
        exc = sys.exc_info()[1]
        if isinstance(exc, _tls.CLIENT_TRANSPORT_ERRORS):
            _log.logger.debug("client transport error from %s: %r", client_address, exc)
            return
        _log.logger.error("unhandled error serving %s", client_address, exc_info=True)

    def server_close(self) -> None:
        super().server_close()
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        if self.access_log is not None:  # release the access-log file handle
            self.access_log.close()

    def server_bind(self) -> None:
        # Accept both IPv4 and IPv6 when bound to an IPv6 wildcard.
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def server_activate(self) -> None:
        super().server_activate()
        config = self.config
        if not config.uses_tls:
            return
        alpn = ["h2", "http/1.1"] if config.http2 else ["http/1.1"]
        context = _tls.build_context(config, alpn)
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def finish_request(self, request: Any, client_address: Any) -> None:
        # Use the selected handler class (ServeryHandler, or WSGIHandler in
        # --wsgi mode), not a hardcoded one.
        self._handler_cls(
            request,
            client_address,
            self,
            directory=os.fspath(self.config.directory),
        )


def make_server(config: Config, *, port_scan: int = 64) -> ServeryHTTPServer:
    """Create (bind + activate) a server for ``config``.

    If ``config.port`` is already in use, scan forward for the next free port (up to
    ``port_scan`` ports) instead of failing — the port actually bound is reported on
    ``server.server_address``. An ephemeral port (``0``) binds directly, and bind
    errors other than "address in use" (e.g. permission denied) are never retried.
    """
    import dataclasses
    import errno

    if config.port == 0:  # the OS already picks a free port — nothing to scan
        return ServeryHTTPServer(config)
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
    last: OSError | None = None
    for port in range(config.port, min(config.port + port_scan + 1, 65536)):
        candidate = config if port == config.port else dataclasses.replace(config, port=port)
        try:
            server = ServeryHTTPServer(candidate)
        except OSError as exc:
            if exc.errno not in in_use:
                raise
            last = exc
            continue
        if port != config.port:
            _log.logger.warning("port %d is in use — bound %d instead", config.port, port)
        return server
    raise last if last is not None else OSError("no free port found")  # pragma: no cover


def server_url(server: ServeryHTTPServer) -> str:
    """Return the URL the server is actually listening on."""
    host, port = server.server_address[:2]
    host_display = f"[{host}]" if ":" in str(host) else host
    scheme = "https" if server.config.uses_tls else "http"
    return f"{scheme}://{host_display}:{port}/"


def _lan_url(config: Config, port: int) -> tuple[str, str]:
    """The URL to advertise (LAN IP substituted for a wildcard bind) + a status."""
    from servery import _netinfo

    host, status = _netinfo.display_host(config.host)
    host_display = f"[{host}]" if ":" in host else host
    scheme = "https" if config.uses_tls else "http"
    return f"{scheme}://{host_display}:{port}/", status


def _print_qr(config: Config, port: int) -> None:  # pragma: no cover - terminal output
    """Print a scannable QR of the LAN URL (or a hint if there's no reachable IP)."""
    from servery import _qr

    url, status = _lan_url(config, port)
    if status != "ok":
        print(
            f"servery: --qr needs a reachable LAN address (bound to {config.host}: {status})",
            file=sys.stderr,
        )
        return
    with contextlib.suppress(_qr.QrError):
        print(f"\nservery: scan to open on another device — {url}", file=sys.stderr)
        print(_qr.render(_qr.generate(url)) + "\n", file=sys.stderr)


def _start_mdns(config: Config, port: int):  # pragma: no cover - needs multicast
    """Begin advertising over mDNS; return a responder handle (or None)."""
    import socket as _socket

    from servery import _mdns, _netinfo

    ip, status = _netinfo.display_host(config.host)
    if status != "ok":
        if not config.quiet:
            print(
                f"servery: --discoverable needs a reachable LAN address ({status})", file=sys.stderr
            )
        return None
    host = _socket.gethostname().split(".")[0] or "servery"
    instance = f"servery on {host} ({port})"
    responder = _mdns.start(instance, host, ip, port)
    if responder is not None and not config.quiet:
        print(f"servery: discoverable as '{instance}' on _http._tcp.local", file=sys.stderr)
    return responder


def _ensure_acme(config: Config) -> tuple[str, str]:  # pragma: no cover - needs a CA + port 80
    """Obtain (or reuse a cached) ACME certificate; return (cert_path, key_path)."""
    import json
    import time

    from servery import _acme, _certgen

    staging = config.acme_staging
    directory = _acme.LE_STAGING if staging else _acme.LE_PRODUCTION
    cache = Path.home() / ".config" / "servery" / "acme" / ("staging" if staging else "production")
    cache.mkdir(parents=True, exist_ok=True)
    primary = config.acme[0]
    cert_path, key_path = cache / f"{primary}.crt", cache / f"{primary}.key"
    # Reuse a cert younger than 60 days (Let's Encrypt certs last 90) — respects rate limits.
    if (
        cert_path.exists()
        and key_path.exists()
        and time.time() - cert_path.stat().st_mtime < 60 * 86400
    ):
        return str(cert_path), str(key_path)
    # Persist the account key so restarts don't re-register (RFC 8555 §7.3.1).
    account_path = cache / "account.json"
    if account_path.exists():
        account_key = {k: int(v) for k, v in json.loads(account_path.read_text()).items()}
    else:
        account_key = _certgen._generate_rsa(2048)
        account_path.write_text(json.dumps({k: str(v) for k, v in account_key.items()}))
        account_path.chmod(0o600)
    chain, key_pem = _acme.obtain(
        list(config.acme),
        email=config.acme_email,
        directory_url=directory,
        account_key=account_key,
    )
    cert_path.write_text(chain)
    key_path.write_text(key_pem)
    key_path.chmod(0o600)
    return str(cert_path), str(key_path)


def serve(config: Config) -> None:  # pragma: no cover - blocking server loop (CLI entry)
    """Run the server until interrupted. Blocks the calling thread."""
    if not config.quiet:
        _log.configure_stderr()
    if config.acme:  # obtain (or reuse) a Let's Encrypt cert, then serve HTTPS with it
        import dataclasses

        ca = "staging" if config.acme_staging else "PRODUCTION"
        if not config.quiet:
            print(
                f"servery: obtaining ACME cert ({ca}) for {', '.join(config.acme)} …",
                file=sys.stderr,
            )
        cert_path, key_path = _ensure_acme(config)
        config = dataclasses.replace(config, tls_cert=cert_path, tls_key=key_path)
    if config.asgi_app:  # ASGI runs its own asyncio event loop, not the threading server
        from servery import asgi

        if not config.quiet:
            scheme = "https" if config.uses_tls else "http"
            print(
                f"servery: serving ASGI app {config.asgi_app} at "
                f"{scheme}://{config.host}:{config.port}/ (experimental)",
                file=sys.stderr,
            )
            for warning in config.startup_warnings():
                print(f"servery: WARNING {warning}", file=sys.stderr)
        asgi.run(config)
        return
    with make_server(config) as httpd:
        port = httpd.server_address[1]
        if not config.quiet:
            print(f"servery: serving {config.directory} at {server_url(httpd)}", file=sys.stderr)
            for warning in config.startup_warnings():
                print(f"servery: WARNING {warning}", file=sys.stderr)
            if config.qr:
                _print_qr(config, port)
        responder = _start_mdns(config, port) if config.discoverable else None
        try:
            with contextlib.suppress(KeyboardInterrupt):
                httpd.serve_forever()
        finally:
            if responder is not None:
                responder.stop()
