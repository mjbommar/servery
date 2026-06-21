"""Command-line interface for servery."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from servery._version import __version__
from servery.config import Config
from servery.server import serve

TLS_HELP = (
    "servery serves user-provided certificates; the standard library cannot mint "
    "one for you.\nGenerate a self-signed certificate with openssl:\n\n"
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
        "--tls-cert",
        metavar="PATH",
        help="TLS certificate chain (PEM); enables HTTPS",
    )
    parser.add_argument("--tls-key", metavar="PATH", help="TLS private key (PEM)")
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
    )


def main(
    argv: Sequence[str] | None = None,
) -> int:  # pragma: no cover - CLI entry, blocks on serve()
    """Run the servery CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if args.tls_help:
        print(TLS_HELP)
        return 0
    config = config_from_args(args)
    try:
        serve(config)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
