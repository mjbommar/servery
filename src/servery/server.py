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
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from servery import (
    _compress,
    _digest,
    _listener,
    _log,
    _resumable,
    _tls,
    _work,
    _writecoord,
    auth,
)
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

    def __init__(
        self,
        config: Config,
        *,
        target_locks: _writecoord.TargetLocks | None = None,
        listener: socket.socket | None = None,
    ) -> None:
        self.config = config
        self.root_real = os.path.realpath(config.directory)
        self.credential = auth.parse(config.auth)
        self.access_log = None
        self._executor: ThreadPoolExecutor | None = None
        self._drain_condition = threading.Condition()
        # The hot request path reads Event.is_set() without taking the registry
        # condition. begin_draining() still mutates the registry/deadline under
        # that condition, while Event supplies a free-threading-safe published
        # admission bit.
        self._drain_event = threading.Event()
        self._drain_deadline: float | None = None
        self._forced_drain_reported = False
        self._active_sockets: set[Any] = set()
        # A socket enters this set only after its max-connections permit was
        # acquired.  Removing it is the single authority for releasing that
        # permit, which makes cleanup idempotent across submit/start failures,
        # normal handler completion, and forced shutdown.
        self._connection_permits: set[Any] = set()
        self._connection_drainers: dict[Any, Any] = {}
        self._slots = (
            threading.BoundedSemaphore(config.max_workers * 4) if config.max_workers else None
        )
        self._connections = (
            threading.BoundedSemaphore(config.max_connections) if config.max_connections else None
        )
        self.archive_limiter = (
            _work.BoundedWorkLimiter("archive", active=config.max_archive_streams)
            if config.max_archive_streams is not None
            else None
        )
        self.target_locks = target_locks or _writecoord.TargetLocks()
        self.partial_uploads = _resumable.PartialUploadBudget(
            self.root_real, config.max_partial_uploads
        )
        self.compression_cache = _compress.CompressionCache(config.compression_cache_size)
        # Digest retention stays disabled until an explicit operator policy has
        # evidence behind it. The zero-sized cache still coalesces concurrent
        # requests for one opened file identity.
        self.digest_cache = _digest.DigestCache()
        self.dav_locks: Any = None
        self.http3_port: int | None = None
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
        super().__init__(
            (config.host, config.port),
            self._handler_cls,
            bind_and_activate=listener is None,
        )
        if listener is not None:
            initial_socket = self.socket
            try:
                self.socket = _listener.adopt_tcp_listener(listener, host=config.host)
            except BaseException:
                initial_socket.close()
                raise
            initial_socket.close()
            self.server_address = self.socket.getsockname()
            host, port = self.server_address[:2]
            self.server_name = socket.getfqdn(host)
            self.server_port = port
            try:
                self._wrap_listener_tls()
            except BaseException:
                super().server_close()
                raise
        try:
            if config.access_log:
                from servery import _accesslog

                if config.access_log_queue:
                    self.access_log = _accesslog.AsyncAccessLog(
                        config.access_log,
                        config.access_log_format,
                        queue_capacity=config.access_log_queue,
                        queue_byte_capacity=config.access_log_queue_bytes,
                        overflow=config.access_log_overflow,
                        batch_size=config.access_log_batch_size,
                        batch_wait=config.access_log_batch_wait,
                    )
                else:
                    self.access_log = _accesslog.AccessLog(
                        config.access_log,
                        config.access_log_format,
                    )
            if config.max_workers:
                self._executor = ThreadPoolExecutor(max_workers=config.max_workers)
            if config.dav:
                from servery import _webdav

                self.dav_locks = _webdav.DavLockManager()
        except BaseException:
            if self.access_log is not None:
                self.access_log.close()
            super().server_close()
            raise

    def process_request(self, request: Any, client_address: Any) -> None:
        if self._connections is not None and not self._connections.acquire(blocking=False):
            _log.logger.warning("connection limit reached; rejecting %s", client_address)
            self.shutdown_request(request)
            return
        reject_for_drain = False
        with self._drain_condition:
            if self._connections is not None:
                self._connection_permits.add(request)
            if self._drain_event.is_set():
                reject_for_drain = True
            else:
                self._active_sockets.add(request)
        if reject_for_drain:
            self._finish_connection(request)
            self.shutdown_request(request)
            return
        if self._executor is not None and self._slots is not None:
            if not self._slots.acquire(blocking=False):
                _log.logger.warning("worker queue limit reached; rejecting %s", client_address)
                self._finish_connection(request)
                self.shutdown_request(request)
                return
            try:
                self._executor.submit(self._process_request_pooled, request, client_address)
            except RuntimeError:
                self._slots.release()
                self._finish_connection(request)
                self.shutdown_request(request)
                raise
        else:
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._finish_connection(request)
                raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish_connection(request)

    def _process_request_pooled(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # mirror ThreadingMixIn: never let a worker thread die
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            if self._slots is not None:
                self._slots.release()
            self._finish_connection(request)

    @property
    def is_draining(self) -> bool:
        """Whether this process has stopped admitting new connection work."""
        return self._drain_event.is_set()

    def register_connection_drainer(self, request: Any, callback: Any) -> None:
        """Register a protocol hook (currently HTTP/2 GOAWAY) for ``request``."""
        call_now = False
        with self._drain_condition:
            if request in self._active_sockets:
                self._connection_drainers[request] = callback
                call_now = self._drain_event.is_set()
        if call_now:
            self._notify_drainer(callback)

    def unregister_connection_drainer(self, request: Any) -> None:
        with self._drain_condition:
            self._connection_drainers.pop(request, None)

    def begin_draining(self) -> None:
        """Stop admission and notify active protocols without waiting for them."""
        callbacks: tuple[Any, ...] = ()
        with self._drain_condition:
            if not self._drain_event.is_set():
                self._drain_event.set()
                if self.archive_limiter is not None:
                    self.archive_limiter.close()
                self._drain_deadline = time.monotonic() + self.config.drain_timeout
                callbacks = tuple(self._connection_drainers.values())
                self._drain_condition.notify_all()
        for callback in callbacks:
            self._notify_drainer(callback)

    @staticmethod
    def _notify_drainer(callback: Any) -> None:
        """Run a protocol notification without putting socket I/O on shutdown's thread."""

        def notify() -> None:
            with contextlib.suppress(OSError):
                callback()

        threading.Thread(
            target=notify,
            name="servery-drain-notify",
            daemon=True,
        ).start()

    def _finish_connection(self, request: Any) -> None:
        release = False
        with self._drain_condition:
            self._connection_drainers.pop(request, None)
            self._active_sockets.discard(request)
            if request in self._connection_permits:
                self._connection_permits.remove(request)
                release = True
            self._drain_condition.notify_all()
        if release and self._connections is not None:
            self._connections.release()

    def _wait_for_drain(self) -> None:
        """Wait to the fixed drain deadline, then interrupt remaining sockets."""
        report: tuple[int, int] | None = None
        with self._drain_condition:
            while self._active_sockets:
                deadline = self._drain_deadline
                if deadline is None:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._drain_condition.wait(remaining)
            remaining_sockets = tuple(self._active_sockets)
            if remaining_sockets and not self._forced_drain_reported:
                protocol_connections = sum(
                    request in self._connection_drainers for request in remaining_sockets
                )
                report = (len(remaining_sockets) - protocol_connections, protocol_connections)
                self._forced_drain_reported = True
        if report is not None:
            _log.logger.warning(
                "graceful drain deadline reached; force-closing "
                "%d HTTP/1/application and %d protocol connection(s)",
                report[0],
                report[1],
            )
        for request in remaining_sockets:
            with contextlib.suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                request.close()

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

    def shutdown(self) -> None:
        """Stop accepting, drain active work to the configured deadline, then abort it."""
        self.begin_draining()
        super().shutdown()
        self._wait_for_drain()

    def server_close(self) -> None:
        self.begin_draining()
        super().server_close()
        self._wait_for_drain()
        if self._executor is not None:
            # A stuck application callable must not turn graceful cleanup into an
            # unbounded wait. A future supervisor can terminate the worker process.
            self._executor.shutdown(wait=False, cancel_futures=True)
        if self.access_log is not None:  # release the access-log file handle
            from servery import _accesslog

            if isinstance(self.access_log, _accesslog.AsyncAccessLog):
                snapshot = self.access_log.close(timeout=self.config.access_log_drain_timeout)
                if snapshot.writer_alive:
                    _log.logger.warning(
                        "access-log drain deadline expired with %d queued and %d active records",
                        snapshot.queued,
                        snapshot.active,
                    )
                if snapshot.dropped_capacity or snapshot.dropped_bytes:
                    _log.logger.warning(
                        "access-log dropped %d capacity-limited and %d byte-limited records",
                        snapshot.dropped_capacity,
                        snapshot.dropped_bytes,
                    )
            else:
                self.access_log.close()

    def server_bind(self) -> None:
        # Accept both IPv4 and IPv6 when bound to an IPv6 wildcard.
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def server_activate(self) -> None:
        super().server_activate()
        self._wrap_listener_tls()

    def _wrap_listener_tls(self) -> None:
        """Wrap this runtime's listener descriptor, never the caller's socket."""
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


def make_server(
    config: Config,
    *,
    port_scan: int = 64,
    target_locks: _writecoord.TargetLocks | None = None,
    listener: socket.socket | None = None,
) -> ServeryHTTPServer:
    """Create (bind + activate) a server for ``config``.

    If ``config.port`` is already in use, scan forward for the next free port (up to
    ``port_scan`` ports) instead of failing — the port actually bound is reported on
    ``server.server_address``. An ephemeral port (``0``) binds directly, and bind
    errors other than "address in use" (e.g. permission denied) are never retried.

    When ``listener`` is supplied it is already bound and listening; scanning is
    skipped. The caller retains that socket, while the returned server owns a
    validated descriptor duplicate.
    """
    if listener is not None:
        return ServeryHTTPServer(config, target_locks=target_locks, listener=listener)

    import dataclasses

    bound = _listener.bind_tcp_listener(
        config.host,
        config.port,
        port_scan=port_scan,
        backlog=ServeryHTTPServer.request_queue_size,
    )
    try:
        actual_port = int(bound.getsockname()[1])
        candidate = (
            dataclasses.replace(config, port=actual_port)
            if config.port != 0 and actual_port != config.port
            else config
        )
        server = ServeryHTTPServer(candidate, target_locks=target_locks, listener=bound)
    finally:
        bound.close()
    if actual_port != config.port and config.port != 0:
        _log.logger.warning("port %d is in use — bound %d instead", config.port, actual_port)
    return server


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


def _start_tftp(
    config: Config, target_locks: _writecoord.TargetLocks
):  # pragma: no cover - needs a UDP socket
    """Start a TFTP listener serving the same directory; return the server handle."""
    from servery import _tftp

    root_real = os.path.realpath(config.directory)
    server = _tftp.TftpServer(
        root_real,
        config.host,
        config.tftp_port,
        allow_write=config.tftp_write,
        max_write_size=config.max_upload_size,
        max_transfers=config.max_tftp_transfers,
        target_locks=target_locks,
        write_lock_timeout=config.write_lock_timeout,
    )
    server.start()
    if not config.quiet:
        mode = "read/write" if config.tftp_write else "read-only"
        print(
            f"servery: serving TFTP ({mode}) on {config.host}:{server.port}/udp",
            file=sys.stderr,
        )
    return server


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
    cert_context = (
        _tls.self_signed_files(config)
        if (config.http3 or config.workers > 1) and config.tls_self_signed
        else contextlib.nullcontext(None)
    )
    with cert_context as generated:
        if generated is not None:
            import dataclasses

            config = dataclasses.replace(
                config, tls_cert=generated[0], tls_key=generated[1], tls_self_signed=False
            )
        _serve_prepared(config)


def _serve_prepared(config: Config) -> None:  # pragma: no cover - lifecycle integration
    """Run listeners after ACME/self-signed certificate material is prepared."""
    if config.workers > 1:
        from servery import _supervisor

        _supervisor.serve(config)
        return
    target_locks = _writecoord.TargetLocks()
    tftp_server = _start_tftp(config, target_locks) if config.tftp else None
    try:
        if config.http3_only:
            import time

            from servery import http3

            handle = http3.start_http3(config)
            try:
                if not config.quiet:
                    print(
                        f"servery: serving HTTP/3 only on {config.host}:{handle.port}/udp",
                        file=sys.stderr,
                    )
                with contextlib.suppress(KeyboardInterrupt):
                    while handle.is_alive:
                        time.sleep(0.25)
            finally:
                handle.close()
            return
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
        with make_server(config, target_locks=target_locks) as httpd:
            port = int(httpd.server_address[1])
            h3_handle = None
            if config.http3:
                import dataclasses

                from servery import http3

                h3_config = dataclasses.replace(
                    config,
                    port=port,
                    http3_port=config.http3_port if config.http3_port is not None else port,
                )
                h3_handle = http3.start_http3(h3_config, compression_cache=httpd.compression_cache)
                httpd.http3_port = h3_handle.port
            if not config.quiet:
                print(
                    f"servery: serving {config.directory} at {server_url(httpd)}", file=sys.stderr
                )
                if h3_handle is not None:
                    print(
                        f"servery: HTTP/3 available on UDP {h3_handle.port} "
                        "(advertised via Alt-Svc)",
                        file=sys.stderr,
                    )
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
                if h3_handle is not None:
                    h3_handle.close()
    finally:
        if tftp_server is not None:
            tftp_server.stop()
