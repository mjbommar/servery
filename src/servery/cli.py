"""Command-line interface for servery."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from servery._version import __version__
from servery.config import Config
from servery.server import serve

# Launch presets: a profile sets defaults (keyed by argparse dest); any flag the
# user passes explicitly still wins. Profiles that expose the box to the network
# AND accept writes require --auth (enforced below) so "open writable public
# server" can't be a one-flag accident. TLS-enabling profiles default to a
# self-signed cert, which --tls-cert transparently upgrades to a real one.
PROFILES: dict[str, dict[str, object]] = {
    "local": {},  # the safe default: 127.0.0.1, read-only, cleartext
    "share": {"host": "0.0.0.0", "tls_self_signed": True},
    "inbox": {"host": "0.0.0.0", "tls_self_signed": True, "upload": True},
    "public-readonly": {"host": "0.0.0.0", "tls_self_signed": True, "cache_max_age": 3600},
    "public-readwrite": {"host": "0.0.0.0", "tls_self_signed": True, "upload": True},
    "cdn": {
        "host": "0.0.0.0",
        "tls_self_signed": True,
        "cache_max_age": 31536000,
        "cors": True,
        "http2": True,
    },
    "dev": {"host": "127.0.0.1", "spa": True, "cors": True},
    "app": {"host": "0.0.0.0", "tls_self_signed": True, "max_workers": os.cpu_count() or 4},
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
        "--allow-overwrite",
        action="store_true",
        help="allow uploads to overwrite existing files",
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
        help="bound concurrency to N worker threads (default: unbounded, thread-per-connection); "
        "set N near the CPU core count under high concurrency to avoid thread thrash "
        "(sharply lower tail latency)",
    )
    parser.add_argument(
        "--http2",
        action="store_true",
        help="enable HTTP/2 (ALPN 'h2' over TLS, and h2c prior-knowledge cleartext)",
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
        help="serve an ASGI application (opt-in, experimental, HTTP only), e.g. myapp:app",
    )
    parser.add_argument(
        "--http3",
        action="store_true",
        help="serve HTTP/3 over QUIC (requires TLS and the 'servery[http3]' extra)",
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
        allow_overwrite=args.allow_overwrite,
        cors=args.cors,
        spa=args.spa,
        cache_max_age=args.cache_max_age,
        security_headers=args.security_headers,
        timeout=args.timeout,
        max_workers=args.max_workers,
        http2=args.http2,
        wsgi_app=args.wsgi,
        cgi_dir=args.cgi,
        asgi_app=args.asgi,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the servery CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.tls_help:
        print(TLS_HELP)
        return 0
    try:
        config = config_from_args(parse_args(argv))
        if args.http3:
            from servery.http3 import Http3UnavailableError, serve_http3

            try:
                serve_http3(config)  # pragma: no cover - blocking server loop
            except Http3UnavailableError as exc:
                print(f"servery: error: {exc}", file=sys.stderr)
                return 2
        else:
            serve(config)  # pragma: no cover - blocking server loop
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except (ValueError, OSError) as exc:
        # Bad --auth spec, unreadable --tls-password-file, etc.: fail cleanly.
        print(f"servery: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
