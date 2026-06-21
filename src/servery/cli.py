"""Command-line interface for servery."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from servery._version import __version__
from servery.config import Config
from servery.server import serve


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
    parser.add_argument("--version", action="version", version=f"servery {__version__}")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    """Convert parsed arguments into a :class:`Config`."""
    return Config.create(
        args.directory,
        host=args.host,
        port=args.port,
        show_hidden=args.show_hidden,
        quiet=args.quiet,
    )


def main(
    argv: Sequence[str] | None = None,
) -> int:  # pragma: no cover - CLI entry, blocks on serve()
    """Run the servery CLI. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    try:
        serve(config)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
