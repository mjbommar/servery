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
    allow_overwrite: bool = False
    upload_extract: bool = False  # expand uploaded archives (requires upload)
    dav: bool = False  # WebDAV (read-only mount); dav_write adds the write methods
    dav_write: bool = False
    cors: bool = False
    spa: bool = False
    cache_max_age: int | None = None
    security_headers: bool = True
    compress: bool = True  # gzip text-like responses when the client accepts it
    qr: bool = False  # print a QR of the LAN URL on startup
    discoverable: bool = False  # advertise over mDNS/DNS-SD (_http._tcp.local)
    acme: tuple[str, ...] = ()  # domains to obtain a Let's Encrypt cert for (empty = off)
    acme_email: str | None = None
    acme_staging: bool = True  # use the staging CA (safe default); --acme-production to opt in
    access_log: str | None = None  # path to write an access log (off = stderr only)
    access_log_format: str = "clf"  # clf | combined | json
    timeout: float = 30.0
    max_workers: int | None = None
    http2: bool = False
    wsgi_app: str | None = None  # "module:callable" — opt-in dynamic handler
    cgi_dir: str | None = None  # cgi-bin directory — opt-in dynamic handler
    asgi_app: str | None = None  # "module:callable" — opt-in async dynamic handler
    proxy_routes: tuple[tuple[str, str], ...] = ()  # (path-prefix, upstream-url) pairs
    tftp: bool = False  # serve the same dir over TFTP (separate UDP listener; LAN only)
    tftp_port: int = 69
    tftp_write: bool = False  # allow anonymous TFTP writes (WRQ); requires tftp

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
        return self.tls_cert is not None or self.tls_self_signed

    def startup_warnings(self) -> list[str]:
        """Return human-readable warnings about an unsafe configuration."""
        warnings: list[str] = []
        if not self.is_loopback_bind:
            warnings.append(f"bound to {self.host} — reachable from the network")
        if self.auth is not None and not self.uses_tls:
            warnings.append("Basic auth is enabled without TLS — credentials travel in cleartext")
        if self.dav_write and self.auth is None:
            warnings.append("--dav-write allows anyone to upload/delete/move files — add --auth")
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
        allow_overwrite: bool = False,
        upload_extract: bool = False,
        dav: bool = False,
        dav_write: bool = False,
        cors: bool = False,
        spa: bool = False,
        cache_max_age: int | None = None,
        security_headers: bool = True,
        compress: bool = True,
        qr: bool = False,
        discoverable: bool = False,
        acme: tuple[str, ...] = (),
        acme_email: str | None = None,
        acme_staging: bool = True,
        access_log: str | None = None,
        access_log_format: str = "clf",
        timeout: float = 30.0,
        max_workers: int | None = None,
        http2: bool = False,
        wsgi_app: str | None = None,
        cgi_dir: str | None = None,
        asgi_app: str | None = None,
        proxy: list[str] | None = None,
        tftp: bool = False,
        tftp_port: int = 69,
        tftp_write: bool = False,
    ) -> Config:
        """Build a Config, resolving ``directory`` to an absolute path."""
        proxy_routes = _parse_proxy_routes(proxy or [])
        # Numeric sanity — fail at config time with a clear message, not later with
        # an opaque OSError/UploadError. (port 0 is valid: an ephemeral port.)
        if not 0 <= port <= 65535:
            raise ValueError(f"--port must be 0-65535, got {port}")
        if not 0 <= tftp_port <= 65535:
            raise ValueError(f"--tftp-port must be 0-65535, got {tftp_port}")
        if tftp_write and not tftp:
            raise ValueError("--tftp-write requires --tftp")
        if max_upload_size <= 0:
            raise ValueError("--max-upload-size must be a positive number of bytes")
        if timeout <= 0:
            raise ValueError("--timeout must be a positive number of seconds")
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
        if upload_extract and not upload:
            raise ValueError("--upload-extract requires --upload")
        if dav_write and not dav:
            raise ValueError("--dav-write requires --dav")
        if access_log_format not in ("clf", "combined", "json"):
            raise ValueError("--access-log-format must be clf, combined, or json")
        if dav and (dynamic or http2 or proxy_routes):
            raise ValueError("--dav is HTTP/1.1 file serving only")
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
            allow_overwrite=allow_overwrite,
            upload_extract=upload_extract,
            dav=dav,
            dav_write=dav_write,
            cors=cors,
            spa=spa,
            cache_max_age=cache_max_age,
            security_headers=security_headers,
            compress=compress,
            qr=qr,
            discoverable=discoverable,
            acme=tuple(acme),
            acme_email=acme_email,
            acme_staging=acme_staging,
            access_log=access_log,
            access_log_format=access_log_format,
            timeout=timeout,
            max_workers=max_workers,
            http2=http2,
            wsgi_app=wsgi_app,
            cgi_dir=cgi_dir,
            asgi_app=asgi_app,
            proxy_routes=proxy_routes,
            tftp=tftp,
            tftp_port=tftp_port,
            tftp_write=tftp_write,
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
