"""Command-line interface for servery."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from servery._version import __version__
from servery.config import Config
from servery.http3 import Http3UnavailableError
from servery.server import serve

# Launch presets: a profile sets defaults (keyed by argparse dest); any flag the
# user passes explicitly still wins. Profiles that expose the box to the network
# AND accept writes require --auth (enforced below) so "open writable public
# server" can't be a one-flag accident. TLS-enabling profiles default to a
# self-signed cert, which --tls-cert transparently upgrades to a real one.
_ALL = "0.0.0.0"  # nosec B104 (profiles below intentionally expose to the network)
PROFILES: dict[str, dict[str, object]] = {
    "local": {},  # the safe default: 127.0.0.1, read-only, cleartext
    "share": {"host": _ALL, "tls_self_signed": True},
    "inbox": {"host": _ALL, "tls_self_signed": True, "upload": True},
    "public-readonly": {"host": _ALL, "tls_self_signed": True, "cache_max_age": 3600},
    "public-readwrite": {"host": _ALL, "tls_self_signed": True, "upload": True},
    "cdn": {
        "host": _ALL,
        "tls_self_signed": True,
        "cache_max_age": 31536000,
        "cors": True,
        "http2": True,
        "compression_cache_size": 32 * 1024 * 1024,
    },
    "dev": {"host": "127.0.0.1", "spa": True, "cors": True},
    "app": {"host": _ALL, "tls_self_signed": True, "max_workers": os.cpu_count() or 4},
}
# Network-exposed + writable -> auth is mandatory.
_PROFILE_REQUIRES_AUTH = frozenset({"inbox", "public-readwrite"})

TLS_HELP = (
    "For quick HTTPS with no setup, use a generated ad-hoc cert:\n\n"
    "  servery --tls-self-signed\n\n"
    "(zero-dependency, generated at startup; clients see an untrusted-cert "
    "warning — fine for a dev box or LAN).\n\n"
    "To use your own certificate, generate one with openssl:\n\n"
    "  openssl req -x509 -newkey rsa:2048 -nodes \\\n"
    "    -keyout key.pem -out cert.pem -days 365 -subj '/CN=localhost'\n\n"
    "Then run:  servery --tls-cert cert.pem --tls-key key.pem"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the servery argument parser."""
    parser = argparse.ArgumentParser(
        prog="servery",
        description="Zero-dependency, pure-Python HTTP file server.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="directory to serve (default: current directory)",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        metavar="NAME",
        help="apply a preset bundle of flags (any explicit flag still overrides); "
        f"one of: {', '.join(sorted(PROFILES))}",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8000,
        help="port to listen on (default: 8000)",
    )
    parser.add_argument(
        "-b",
        "--bind",
        dest="host",
        default="127.0.0.1",
        metavar="ADDR",
        help="address to bind (default: 127.0.0.1; use 0.0.0.0 to expose)",
    )
    parser.add_argument(
        "--show-hidden",
        action="store_true",
        help="include dotfiles in listings",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress request logging and the startup banner",
    )
    parser.add_argument(
        "--auth",
        metavar="USER:PASS",
        help="require HTTP Basic auth (USER:PASS, or USER:sha256:HEX / USER:sha512:HEX)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="enable file upload (POST multipart/form-data into the served tree)",
    )
    parser.add_argument(
        "--max-upload-size",
        type=int,
        default=100 * 1024 * 1024,
        metavar="BYTES",
        help="maximum accepted upload size in bytes (default: 100 MiB)",
    )
    parser.add_argument(
        "--max-request-body",
        type=int,
        default=100 * 1024 * 1024,
        metavar="BYTES",
        help="maximum request body for apps/proxy/WebDAV (default: 100 MiB)",
    )
    parser.add_argument(
        "--keepalive-drain-limit",
        type=int,
        default=64 * 1024,
        metavar="BYTES",
        help="drain at most BYTES of unused accepted body before closing (default: 64 KiB)",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="allow uploads to overwrite existing files",
    )
    parser.add_argument(
        "--write-lock-timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait for another write to the same target (default: 0, reject immediately)",
    )
    parser.add_argument(
        "--partial-upload-ttl",
        type=float,
        default=24 * 60 * 60,
        metavar="SECONDS",
        help="discard stale resumable-upload sidecars after this age (default: 86400; 0 disables)",
    )
    parser.add_argument(
        "--max-partial-uploads",
        type=int,
        default=128,
        metavar="COUNT",
        help="maximum outstanding resumable sidecars (default: 128; 0 disables)",
    )
    parser.add_argument(
        "--upload-extract",
        action="store_true",
        help="expand uploaded archives (zip/tar) into the target dir, securely "
        "(zip-slip/zip-bomb/symlink guarded); requires --upload",
    )
    parser.add_argument(
        "--cors",
        action="store_true",
        help="send permissive CORS headers (Access-Control-Allow-Origin: *)",
    )
    parser.add_argument(
        "--spa",
        action="store_true",
        help="single-page-app fallback: serve /index.html for unknown paths",
    )
    parser.add_argument(
        "--cache",
        type=int,
        default=None,
        metavar="SECONDS",
        dest="cache_max_age",
        help="Cache-Control max-age for file responses (default: no-cache)",
    )
    parser.add_argument(
        "--no-security-headers",
        action="store_false",
        dest="security_headers",
        help="disable servery's default security response headers",
    )
    parser.add_argument(
        "--no-compress",
        action="store_false",
        dest="compress",
        help="disable on-the-fly gzip of text-like responses",
    )
    parser.add_argument(
        "--max-compress-size",
        type=int,
        default=10 * 1024 * 1024,
        metavar="BYTES",
        help="largest static file compressed in memory "
        "(default: 10 MiB; 0 disables file compression)",
    )
    parser.add_argument(
        "--compression-cache-size",
        type=int,
        default=0,
        metavar="BYTES",
        help="byte budget for cached compressed static responses (default: 0, disabled)",
    )
    parser.add_argument(
        "--max-buffered-response",
        type=int,
        default=1024 * 1024,
        metavar="BYTES",
        help="largest h2/h3 file buffered as one response (default: 1 MiB; 0 streams all files)",
    )
    parser.add_argument(
        "--max-listing-entries",
        type=int,
        default=100_000,
        metavar="N",
        help="maximum entries considered by a directory listing (default: 100000)",
    )
    parser.add_argument(
        "--listing-page-size",
        type=int,
        default=1000,
        metavar="N",
        help="directory-listing rows per page (default: 1000)",
    )
    parser.add_argument(
        "--listing-details-threshold",
        type=int,
        default=10_000,
        metavar="N",
        help="omit expensive listing metadata beyond N entries (default: 10000)",
    )
    parser.add_argument(
        "--dav",
        action="store_true",
        help="enable WebDAV so the share can be mounted as a network drive (read-only)",
    )
    parser.add_argument(
        "--dav-write",
        action="store_true",
        help="allow WebDAV writes (PUT/DELETE/MKCOL/MOVE/COPY); requires --dav (use with --auth)",
    )
    parser.add_argument(
        "--dav-lock-mode",
        choices=("class1", "compat", "enforced"),
        default="enforced",
        help="WebDAV locking: class1 (none), compat (fake token), or enforced (default)",
    )
    parser.add_argument(
        "--max-propfind-entries",
        type=int,
        default=10_000,
        metavar="N",
        help="maximum children returned by WebDAV PROPFIND Depth:1 (default: 10000)",
    )
    parser.add_argument(
        "--qr",
        action="store_true",
        help="print a scannable QR code of the LAN URL on startup",
    )
    parser.add_argument(
        "--discoverable",
        action="store_true",
        help="advertise the server on the local network via mDNS/DNS-SD (Bonjour)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-connection socket timeout (default: 30)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        metavar="N",
        help="bound blocking work to N worker threads (default: thread-per-connection); "
        "set N near the CPU core count under high concurrency to avoid thread thrash "
        "(sharply lower tail latency)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=256,
        metavar="N",
        help="maximum simultaneous HTTP connections/sessions (default: 256)",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        help="enable HTTP/2 (ALPN 'h2' over TLS, and h2c prior-knowledge cleartext)",
    )
    parser.add_argument(
        "--max-h2-streams",
        type=int,
        default=100,
        metavar="N",
        help="maximum active streams per HTTP/2 connection (default: 100)",
    )
    parser.add_argument(
        "--wsgi",
        metavar="MODULE:APP",
        help="serve a WSGI application (opt-in) instead of files, e.g. myapp:application",
    )
    parser.add_argument(
        "--cgi",
        metavar="DIR",
        help="execute CGI scripts (opt-in) from DIR as a cgi-bin; runs code, off by default",
    )
    parser.add_argument(
        "--asgi",
        metavar="MODULE:APP",
        help="serve an ASGI application (opt-in, experimental; supports TLS), e.g. myapp:app",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        metavar="PREFIX=URL",
        help="reverse-proxy requests under PREFIX to an upstream (repeatable), "
        "e.g. /api=http://localhost:8001",
    )
    parser.add_argument(
        "--http3",
        action="store_true",
        help="serve HTTP/3 alongside TCP (requires TLS and the 'servery[http3]' extra)",
    )
    parser.add_argument(
        "--http3-only",
        action="store_true",
        help="serve only HTTP/3/QUIC, without the normal TCP fallback; requires --http3",
    )
    parser.add_argument(
        "--http3-port",
        type=int,
        default=None,
        metavar="PORT",
        help="UDP port for HTTP/3 (default: the TCP --port)",
    )
    parser.add_argument(
        "--tftp",
        action="store_true",
        help="also serve the directory over TFTP on UDP (read-only; LAN/trusted "
        "networks only — no auth or encryption). Useful for PXE boot / network gear",
    )
    parser.add_argument(
        "--tftp-port",
        type=int,
        default=69,
        metavar="PORT",
        help="UDP port for --tftp (default: 69; needs privileges below 1024)",
    )
    parser.add_argument(
        "--tftp-write",
        action="store_true",
        help="allow anonymous TFTP uploads (WRQ); requires --tftp (use on trusted LANs only)",
    )
    parser.add_argument(
        "--max-tftp-transfers",
        type=int,
        default=32,
        metavar="N",
        help="maximum simultaneous TFTP transfers (default: 32)",
    )
    parser.add_argument(
        "--tls-cert",
        metavar="PATH",
        help="TLS certificate chain (PEM); enables HTTPS",
    )
    parser.add_argument("--tls-key", metavar="PATH", help="TLS private key (PEM)")
    parser.add_argument(
        "--tls-self-signed",
        action="store_true",
        help="enable HTTPS with an ad-hoc self-signed certificate generated at "
        "startup (zero-dependency; clients see an untrusted-cert warning)",
    )
    parser.add_argument(
        "--tls-password-file",
        metavar="PATH",
        help="file containing the TLS private-key passphrase",
    )
    parser.add_argument(
        "--tls-help",
        action="store_true",
        help="print how to generate a self-signed certificate, then exit",
    )
    parser.add_argument(
        "--acme",
        metavar="DOMAIN",
        action="append",
        help="obtain a browser-trusted Let's Encrypt cert for DOMAIN via ACME HTTP-01 "
        "(repeatable; needs the HTTP-01 challenge reachable on port 80). Zero-dependency.",
    )
    parser.add_argument("--acme-email", metavar="EMAIL", help="contact email for the ACME account")
    parser.add_argument(
        "--access-log",
        metavar="PATH",
        help="write an access log to PATH (one line per response)",
    )
    parser.add_argument(
        "--access-log-format",
        choices=("clf", "combined", "json"),
        default="clf",
        help="access log format: clf (default), combined (+ referer/user-agent), or json",
    )
    parser.add_argument(
        "--acme-production",
        action="store_false",
        dest="acme_staging",
        help="use the Let's Encrypt PRODUCTION CA (default: staging — safe for testing)",
    )
    parser.add_argument("--version", action="version", version=f"servery {__version__}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse argv, applying any ``--profile`` as a defaults layer (explicit flags win).

    Two-pass: parse once to learn the profile, push its values as parser defaults,
    then re-parse so argparse resolves precedence natively (a flag equal to the
    stock default is still distinguishable from "unset").
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.profile:
        parser.set_defaults(**PROFILES[args.profile])
        args = parser.parse_args(argv)
        # A real --tls-cert supersedes the profile's self-signed default.
        if args.tls_cert is not None:
            args.tls_self_signed = False
        if args.profile in _PROFILE_REQUIRES_AUTH and not args.auth:
            msg = (
                f"--profile {args.profile} exposes writes to the network and "
                "requires --auth USER:PASS"
            )
            raise ValueError(msg)
    return args


def config_from_args(args: argparse.Namespace) -> Config:
    """Convert parsed arguments into a :class:`Config`."""
    tls_password = None
    if args.tls_password_file:
        tls_password = Path(args.tls_password_file).read_text(encoding="utf-8").strip()
    return Config.create(
        args.directory,
        host=args.host,
        port=args.port,
        show_hidden=args.show_hidden,
        quiet=args.quiet,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        tls_password=tls_password,
        tls_self_signed=args.tls_self_signed,
        auth=args.auth,
        upload=args.upload,
        max_upload_size=args.max_upload_size,
        max_request_body=args.max_request_body,
        keepalive_drain_limit=args.keepalive_drain_limit,
        allow_overwrite=args.allow_overwrite,
        write_lock_timeout=args.write_lock_timeout,
        partial_upload_ttl=args.partial_upload_ttl,
        max_partial_uploads=args.max_partial_uploads,
        upload_extract=args.upload_extract,
        cors=args.cors,
        compress=args.compress,
        max_compress_size=args.max_compress_size,
        compression_cache_size=args.compression_cache_size,
        max_buffered_response=args.max_buffered_response,
        max_listing_entries=args.max_listing_entries,
        listing_page_size=args.listing_page_size,
        listing_details_threshold=args.listing_details_threshold,
        dav=args.dav,
        dav_write=args.dav_write,
        dav_lock_mode=args.dav_lock_mode,
        max_propfind_entries=args.max_propfind_entries,
        qr=args.qr,
        discoverable=args.discoverable,
        acme=tuple(args.acme or ()),
        acme_email=args.acme_email,
        acme_staging=args.acme_staging,
        access_log=args.access_log,
        access_log_format=args.access_log_format,
        spa=args.spa,
        cache_max_age=args.cache_max_age,
        security_headers=args.security_headers,
        timeout=args.timeout,
        max_workers=args.max_workers,
        max_connections=args.max_connections,
        http2=args.http2,
        max_h2_streams=args.max_h2_streams,
        http3=args.http3,
        http3_only=args.http3_only,
        http3_port=args.http3_port,
        wsgi_app=args.wsgi,
        cgi_dir=args.cgi,
        asgi_app=args.asgi,
        proxy=args.proxy,
        tftp=args.tftp,
        tftp_port=args.tftp_port,
        tftp_write=args.tftp_write,
        max_tftp_transfers=args.max_tftp_transfers,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the servery CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.tls_help:
        print(TLS_HELP)
        return 0
    try:
        config = config_from_args(parse_args(argv))
        serve(config)  # pragma: no cover - blocking server loop
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except (ValueError, OSError) as exc:
        # Bad --auth spec, unreadable --tls-password-file, etc.: fail cleanly.
        print(f"servery: error: {exc}", file=sys.stderr)
        return 2
    except Http3UnavailableError as exc:
        print(f"servery: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
