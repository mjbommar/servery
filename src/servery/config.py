"""Runtime configuration for servery.

``Config`` is a frozen dataclass — the single, immutable source of truth shared
across request-handler threads. Immutability is deliberate: it makes the server
safe to run under free-threaded (no-GIL) CPython without locks.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    """Immutable server configuration.

    New fields are added as features land; everything here is safe to read
    concurrently from many threads.
    """

    directory: Path
    host: str = "127.0.0.1"
    port: int = 8000
    show_hidden: bool = False
    quiet: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_password: str | None = None
    tls_self_signed: bool = False
    auth: str | None = None
    upload: bool = False
    max_upload_size: int = 100 * 1024 * 1024
    max_request_body: int = 100 * 1024 * 1024
    keepalive_drain_limit: int = 64 * 1024
    allow_overwrite: bool = False
    write_lock_timeout: float = 0.0
    partial_upload_ttl: float = 24 * 60 * 60
    max_partial_uploads: int = 128
    upload_extract: bool = False  # expand uploaded archives (requires upload)
    dav: bool = False  # WebDAV (read-only mount); dav_write adds the write methods
    dav_write: bool = False
    dav_lock_mode: str = "enforced"  # class1 | compat | enforced
    max_propfind_entries: int = 10_000
    cors: bool = False
    spa: bool = False
    cache_max_age: int | None = None
    security_headers: bool = True
    compress: bool = True  # gzip text-like responses when the client accepts it
    max_compress_size: int = 10 * 1024 * 1024
    compression_cache_size: int = 0
    max_buffered_response: int = 1024 * 1024
    small_file_buffer_size: int = 16 * 1024
    max_listing_entries: int = 100_000
    listing_page_size: int = 1000
    listing_details_threshold: int = 10_000
    qr: bool = False  # print a QR of the LAN URL on startup
    discoverable: bool = False  # advertise over mDNS/DNS-SD (_http._tcp.local)
    acme: tuple[str, ...] = ()  # domains to obtain a Let's Encrypt cert for (empty = off)
    acme_email: str | None = None
    acme_staging: bool = True  # use the staging CA (safe default); --acme-production to opt in
    access_log: str | None = None  # path to write an access log (off = stderr only)
    access_log_format: str = "clf"  # clf | combined | json
    access_log_queue: int = 0
    access_log_queue_bytes: int = 8 * 1024 * 1024
    access_log_overflow: str = "block"  # block | drop
    access_log_batch_size: int = 8
    access_log_batch_wait: float = 0.001
    access_log_drain_timeout: float = 5.0
    timeout: float = 30.0
    keepalive_timeout: float | None = None
    request_head_timeout: float | None = None
    request_body_timeout: float | None = None
    write_timeout: float | None = None
    drain_timeout: float = 30.0
    workers: int = 1
    worker_start_timeout: float = 30.0
    force_timeout: float = 1.0
    max_workers: int | None = None
    max_archive_streams: int | None = None
    max_connections: int | None = 256
    max_requests_per_connection: int = 0
    http2: bool = False
    max_h2_streams: int = 100
    http3: bool = False
    http3_only: bool = False
    http3_port: int | None = None
    wsgi_app: str | None = None  # "module:callable" — opt-in dynamic handler
    cgi_dir: str | None = None  # cgi-bin directory — opt-in dynamic handler
    asgi_app: str | None = None  # "module:callable" — opt-in async dynamic handler
    lifespan: str = "auto"  # auto | on | off (ASGI only)
    lifespan_timeout: float = 5.0
    proxy_routes: tuple[tuple[str, str], ...] = ()  # (path-prefix, upstream-url) pairs
    tftp: bool = False  # serve the same dir over TFTP (separate UDP listener; LAN only)
    tftp_port: int = 69
    tftp_write: bool = False  # allow anonymous TFTP writes (WRQ); requires tftp
    max_tftp_transfers: int = 32
    preview: bool = False  # serve ?preview= render pages (markdown/code/table/media)
    preview_max_bytes: int = 2 * 1024 * 1024  # largest file the preview will read
    metadata: bool = False  # extract document metadata; enables ?metadata= and meta columns
    metadata_max_bytes: int = 64 * 1024  # per-file read budget for extraction

    @property
    def cache_control(self) -> str:
        """The Cache-Control value for file responses."""
        if self.cache_max_age is None:
            return "no-cache"
        return f"max-age={self.cache_max_age}"

    @property
    def is_loopback_bind(self) -> bool:
        """True when bound to a loopback address (the safe default)."""
        return self.host in {"127.0.0.1", "::1", "localhost"}

    @property
    def uses_tls(self) -> bool:
        """True when HTTPS is configured (a provided or self-signed certificate)."""
        return self.tls_cert is not None or self.tls_self_signed or bool(self.acme)

    @property
    def aggregate_connection_limit(self) -> int | None:
        """Maximum connections across the fixed worker generation.

        ``max_connections`` is deliberately a per-worker admission budget.  The
        supervisor does not place a second semaphore in the parent accept path,
        so operators can calculate the process-tree bound without mistaking the
        configured value for a global limit.
        """
        if self.max_connections is None:
            return None
        return self.workers * self.max_connections

    @property
    def aggregate_compression_cache_size(self) -> int:
        """Maximum retained compressed bytes across all worker-local caches."""
        return self.workers * self.compression_cache_size

    @property
    def aggregate_archive_stream_limit(self) -> int | None:
        """Maximum concurrent archive producers across worker-local leases."""
        if self.max_archive_streams is None:
            return None
        return self.workers * self.max_archive_streams

    def startup_warnings(self) -> list[str]:
        """Return human-readable warnings about an unsafe configuration."""
        warnings: list[str] = []
        if not self.is_loopback_bind:
            warnings.append(f"bound to {self.host} — reachable from the network")
        if self.auth is not None and not self.uses_tls:
            warnings.append("Basic auth is enabled without TLS — credentials travel in cleartext")
        if self.dav_write and self.auth is None:
            warnings.append("--dav-write allows anyone to upload/delete/move files — add --auth")
        if self.dav_write and self.dav_lock_mode == "compat":
            warnings.append(
                "--dav-lock-mode compat returns lock tokens without enforcing exclusion"
            )
        if self.tls_self_signed:
            warnings.append(
                "using a self-signed certificate — clients will see an "
                "'untrusted certificate' warning (fine for a dev box or LAN)"
            )
        if self.tftp:
            warnings.append(
                "TFTP has no authentication or encryption — use it on trusted LANs only"
            )
        if self.tftp_write:
            warnings.append("--tftp-write accepts anonymous file writes over UDP")
        return warnings

    @classmethod
    def create(
        cls,
        directory: str | os.PathLike[str] = ".",
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        show_hidden: bool = False,
        quiet: bool = False,
        tls_cert: str | None = None,
        tls_key: str | None = None,
        tls_password: str | None = None,
        tls_self_signed: bool = False,
        auth: str | None = None,
        upload: bool = False,
        max_upload_size: int = 100 * 1024 * 1024,
        max_request_body: int = 100 * 1024 * 1024,
        keepalive_drain_limit: int = 64 * 1024,
        allow_overwrite: bool = False,
        write_lock_timeout: float = 0.0,
        partial_upload_ttl: float = 24 * 60 * 60,
        max_partial_uploads: int = 128,
        upload_extract: bool = False,
        dav: bool = False,
        dav_write: bool = False,
        dav_lock_mode: str = "enforced",
        max_propfind_entries: int = 10_000,
        cors: bool = False,
        spa: bool = False,
        cache_max_age: int | None = None,
        security_headers: bool = True,
        compress: bool = True,
        max_compress_size: int = 10 * 1024 * 1024,
        compression_cache_size: int = 0,
        max_buffered_response: int = 1024 * 1024,
        small_file_buffer_size: int = 16 * 1024,
        max_listing_entries: int = 100_000,
        listing_page_size: int = 1000,
        listing_details_threshold: int = 10_000,
        qr: bool = False,
        discoverable: bool = False,
        acme: tuple[str, ...] = (),
        acme_email: str | None = None,
        acme_staging: bool = True,
        access_log: str | None = None,
        access_log_format: str = "clf",
        access_log_queue: int = 0,
        access_log_queue_bytes: int = 8 * 1024 * 1024,
        access_log_overflow: str = "block",
        access_log_batch_size: int = 8,
        access_log_batch_wait: float = 0.001,
        access_log_drain_timeout: float = 5.0,
        timeout: float = 30.0,
        keepalive_timeout: float | None = None,
        request_head_timeout: float | None = None,
        request_body_timeout: float | None = None,
        write_timeout: float | None = None,
        drain_timeout: float = 30.0,
        workers: int | str = 1,
        worker_start_timeout: float = 30.0,
        force_timeout: float = 1.0,
        max_workers: int | None = None,
        max_archive_streams: int | None = None,
        max_connections: int | None = 256,
        max_requests_per_connection: int = 0,
        http2: bool = False,
        max_h2_streams: int = 100,
        http3: bool = False,
        http3_only: bool = False,
        http3_port: int | None = None,
        wsgi_app: str | None = None,
        cgi_dir: str | None = None,
        asgi_app: str | None = None,
        lifespan: str = "auto",
        lifespan_timeout: float = 5.0,
        proxy: list[str] | None = None,
        tftp: bool = False,
        tftp_port: int = 69,
        tftp_write: bool = False,
        max_tftp_transfers: int = 32,
        preview: bool = False,
        preview_max_bytes: int = 2 * 1024 * 1024,
        metadata: bool = False,
        metadata_max_bytes: int = 64 * 1024,
    ) -> Config:
        """Build a Config, resolving ``directory`` to an absolute path."""
        proxy_routes = _parse_proxy_routes(proxy or [])
        # Numeric sanity — fail at config time with a clear message, not later with
        # an opaque OSError/UploadError. (port 0 is valid: an ephemeral port.)
        if not 0 <= port <= 65535:
            raise ValueError(f"--port must be 0-65535, got {port}")
        if not 0 <= tftp_port <= 65535:
            raise ValueError(f"--tftp-port must be 0-65535, got {tftp_port}")
        if http3_port is not None and not 0 <= http3_port <= 65535:
            raise ValueError(f"--http3-port must be 0-65535, got {http3_port}")
        if tftp_write and not tftp:
            raise ValueError("--tftp-write requires --tftp")
        if max_upload_size <= 0:
            raise ValueError("--max-upload-size must be a positive number of bytes")
        if max_request_body <= 0:
            raise ValueError("--max-request-body must be a positive number of bytes")
        if keepalive_drain_limit < 0:
            raise ValueError("--keepalive-drain-limit must be >= 0 bytes")
        if write_lock_timeout < 0:
            raise ValueError("--write-lock-timeout must be >= 0 seconds")
        if partial_upload_ttl < 0:
            raise ValueError("--partial-upload-ttl must be >= 0 seconds")
        if max_partial_uploads < 0:
            raise ValueError("--max-partial-uploads must be >= 0")
        if preview_max_bytes <= 0:
            raise ValueError("--preview-max-bytes must be a positive number of bytes")
        if metadata_max_bytes <= 0:
            raise ValueError("--metadata-max-bytes must be a positive number of bytes")
        if timeout <= 0:
            raise ValueError("--timeout must be a positive number of seconds")
        if keepalive_timeout is not None and keepalive_timeout <= 0:
            raise ValueError("--keepalive-timeout must be a positive number of seconds")
        if request_head_timeout is not None and request_head_timeout <= 0:
            raise ValueError("--request-head-timeout must be a positive number of seconds")
        if request_body_timeout is not None and request_body_timeout <= 0:
            raise ValueError("--request-body-timeout must be a positive number of seconds")
        if write_timeout is not None and write_timeout <= 0:
            raise ValueError("--write-timeout must be a positive number of seconds")
        if drain_timeout < 0:
            raise ValueError("--drain-timeout must be >= 0 seconds")
        if isinstance(workers, str):
            if workers == "auto":
                process_cpu_count = getattr(os, "process_cpu_count", os.cpu_count)
                workers = process_cpu_count() or 1
            elif workers.isdecimal():
                workers = int(workers)
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("--workers must be a positive integer or auto")
        if worker_start_timeout <= 0:
            raise ValueError("--worker-start-timeout must be a positive number of seconds")
        if force_timeout < 0:
            raise ValueError("--force-timeout must be >= 0 seconds")
        if max_workers is not None and max_workers <= 0:
            raise ValueError("--max-workers must be a positive integer")
        if max_archive_streams is not None and max_archive_streams <= 0:
            raise ValueError("--max-archive-streams must be a positive integer")
        if (
            max_workers is not None
            and max_archive_streams is not None
            and max_archive_streams >= max_workers
        ):
            raise ValueError("--max-archive-streams must be smaller than --max-workers")
        if max_connections is not None and max_connections <= 0:
            raise ValueError("--max-connections must be a positive integer")
        if max_requests_per_connection < 0:
            raise ValueError("--max-requests-per-connection must be >= 0")
        if lifespan not in {"auto", "on", "off"}:
            raise ValueError("--lifespan must be auto, on, or off")
        if lifespan_timeout <= 0:
            raise ValueError("--lifespan-timeout must be a positive number of seconds")
        if asgi_app is None and (lifespan != "auto" or lifespan_timeout != 5.0):
            raise ValueError("--lifespan and --lifespan-timeout require --asgi")
        if max_h2_streams <= 0:
            raise ValueError("--max-h2-streams must be a positive integer")
        if max_tftp_transfers <= 0:
            raise ValueError("--max-tftp-transfers must be a positive integer")
        if max_propfind_entries <= 0:
            raise ValueError("--max-propfind-entries must be a positive integer")
        if max_compress_size < 0:
            raise ValueError("--max-compress-size must be >= 0 bytes")
        if compression_cache_size < 0:
            raise ValueError("--compression-cache-size must be >= 0 bytes")
        if max_buffered_response < 0:
            raise ValueError("--max-buffered-response must be >= 0 bytes")
        if small_file_buffer_size < 0:
            raise ValueError("--small-file-buffer-size must be >= 0 bytes")
        if max_listing_entries <= 0:
            raise ValueError("--max-listing-entries must be a positive integer")
        if listing_page_size <= 0:
            raise ValueError("--listing-page-size must be a positive integer")
        if listing_details_threshold <= 0:
            raise ValueError("--listing-details-threshold must be a positive integer")
        if dav_lock_mode not in ("class1", "compat", "enforced"):
            raise ValueError("--dav-lock-mode must be class1, compat, or enforced")
        if cache_max_age is not None and cache_max_age < 0:
            raise ValueError("--cache must be >= 0 seconds")
        if tls_self_signed and tls_cert is not None:
            raise ValueError("--tls-self-signed cannot be combined with --tls-cert")
        if acme and (tls_cert is not None or tls_self_signed):
            raise ValueError(
                "--acme obtains its own certificate; drop --tls-cert/--tls-self-signed"
            )
        dynamic = [
            name
            for name, value in (("--wsgi", wsgi_app), ("--cgi", cgi_dir), ("--asgi", asgi_app))
            if value
        ]
        if len(dynamic) > 1:
            raise ValueError(f"choose only one dynamic handler: {' / '.join(dynamic)}")
        if dynamic and http2:
            raise ValueError(f"{dynamic[0]} is HTTP/1.1 only and cannot be combined with --http2")
        if proxy_routes and (dynamic or http2):
            # The proxy only dispatches on the HTTP/1.1 file handler; reject combos
            # where it would be silently ignored rather than pretend it works.
            other = dynamic[0] if dynamic else "--http2"
            raise ValueError(f"--proxy cannot be combined with {other}")
        if http3_only and not http3:
            raise ValueError("--http3-only requires --http3")
        if http3 and not (tls_cert is not None or tls_self_signed or acme):
            raise ValueError("--http3 requires --tls-cert, --tls-self-signed, or --acme")
        if http3 and (dynamic or proxy_routes):
            other = dynamic[0] if dynamic else "--proxy"
            raise ValueError(f"--http3 cannot be combined with {other}")
        if upload_extract and not upload:
            raise ValueError("--upload-extract requires --upload")
        if dav_write and not dav:
            raise ValueError("--dav-write requires --dav")
        if access_log_format not in ("clf", "combined", "json"):
            raise ValueError("--access-log-format must be clf, combined, or json")
        if access_log_queue < 0:
            raise ValueError("--access-log-queue must be >= 0")
        if access_log_queue_bytes <= 0:
            raise ValueError("--access-log-queue-bytes must be positive")
        if access_log_overflow not in ("block", "drop"):
            raise ValueError("--access-log-overflow must be block or drop")
        if access_log_batch_size <= 0:
            raise ValueError("--access-log-batch-size must be positive")
        if access_log_batch_wait < 0:
            raise ValueError("--access-log-batch-wait must be >= 0 seconds")
        if access_log_drain_timeout < 0:
            raise ValueError("--access-log-drain-timeout must be >= 0 seconds")
        if dav and (dynamic or http2 or proxy_routes):
            raise ValueError("--dav is HTTP/1.1 file serving only")
        if workers > 1:
            # These modes either mutate shared filesystem state or own a
            # singleton listener/file/identity.  Keep rejecting them until the
            # parent-broker protocols in EDGE-013 have bounded queues, owner-death
            # cleanup, and cross-platform tests.  Quietly running one instance in
            # every worker would be observably incorrect.
            unsupported_workers = [
                name
                for name, enabled in (
                    ("--upload", upload),
                    ("--dav", dav),
                    ("--cgi", cgi_dir is not None),
                    ("--proxy", bool(proxy_routes)),
                    ("--http3", http3),
                    ("--tftp", tftp),
                    ("--discoverable", discoverable),
                    ("--qr", qr),
                    ("--acme", bool(acme)),
                    ("--access-log", access_log is not None),
                )
                if enabled
            ]
            if unsupported_workers:
                raise ValueError(
                    "--workers > 1 currently supports static, WSGI, or ASGI serving; "
                    f"incompatible with {', '.join(unsupported_workers)}"
                )
        return cls(
            directory=Path(directory).resolve(),
            host=host,
            port=port,
            show_hidden=show_hidden,
            quiet=quiet,
            tls_cert=tls_cert,
            tls_key=tls_key,
            tls_password=tls_password,
            tls_self_signed=tls_self_signed,
            auth=auth,
            upload=upload,
            max_upload_size=max_upload_size,
            max_request_body=max_request_body,
            keepalive_drain_limit=keepalive_drain_limit,
            allow_overwrite=allow_overwrite,
            write_lock_timeout=write_lock_timeout,
            partial_upload_ttl=partial_upload_ttl,
            max_partial_uploads=max_partial_uploads,
            upload_extract=upload_extract,
            dav=dav,
            dav_write=dav_write,
            dav_lock_mode=dav_lock_mode,
            max_propfind_entries=max_propfind_entries,
            cors=cors,
            spa=spa,
            cache_max_age=cache_max_age,
            security_headers=security_headers,
            compress=compress,
            max_compress_size=max_compress_size,
            compression_cache_size=compression_cache_size,
            max_buffered_response=max_buffered_response,
            small_file_buffer_size=small_file_buffer_size,
            max_listing_entries=max_listing_entries,
            listing_page_size=listing_page_size,
            listing_details_threshold=listing_details_threshold,
            qr=qr,
            discoverable=discoverable,
            acme=tuple(acme),
            acme_email=acme_email,
            acme_staging=acme_staging,
            access_log=access_log,
            access_log_format=access_log_format,
            access_log_queue=access_log_queue,
            access_log_queue_bytes=access_log_queue_bytes,
            access_log_overflow=access_log_overflow,
            access_log_batch_size=access_log_batch_size,
            access_log_batch_wait=access_log_batch_wait,
            access_log_drain_timeout=access_log_drain_timeout,
            timeout=timeout,
            keepalive_timeout=keepalive_timeout,
            request_head_timeout=request_head_timeout,
            request_body_timeout=request_body_timeout,
            write_timeout=write_timeout,
            drain_timeout=drain_timeout,
            workers=workers,
            worker_start_timeout=worker_start_timeout,
            force_timeout=force_timeout,
            max_workers=max_workers,
            max_archive_streams=max_archive_streams,
            max_connections=max_connections,
            max_requests_per_connection=max_requests_per_connection,
            http2=http2,
            max_h2_streams=max_h2_streams,
            http3=http3,
            http3_only=http3_only,
            http3_port=http3_port,
            wsgi_app=wsgi_app,
            cgi_dir=cgi_dir,
            asgi_app=asgi_app,
            lifespan=lifespan,
            lifespan_timeout=lifespan_timeout,
            proxy_routes=proxy_routes,
            tftp=tftp,
            tftp_port=tftp_port,
            tftp_write=tftp_write,
            max_tftp_transfers=max_tftp_transfers,
            preview=preview,
            preview_max_bytes=preview_max_bytes,
            metadata=metadata,
            metadata_max_bytes=metadata_max_bytes,
        )


def _parse_proxy_routes(specs: list[str]) -> tuple[tuple[str, str], ...]:
    """Parse ``["/api=http://host:port", ...]`` into validated (prefix, url) pairs."""
    routes: list[tuple[str, str]] = []
    for spec in specs:
        prefix, sep, upstream = spec.partition("=")
        if not sep or not prefix.startswith("/"):
            raise ValueError(f"--proxy {spec!r}: expected '/prefix=http://upstream'")
        if not upstream.startswith(("http://", "https://")):
            raise ValueError(f"--proxy {spec!r}: upstream must be an http(s) URL")
        routes.append((prefix, upstream))
    # Longest prefix first, so /api/v2 wins over /api.
    routes.sort(key=lambda route: len(route[0]), reverse=True)
    return tuple(routes)
