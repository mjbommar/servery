"""Command-line entry point for servery.

This is a placeholder surface so the package, its console script, and the CI
gates are real from day one. The actual server lands with the v0.1 walking
skeleton (see ``docs/ROADMAP.md``).
"""

import argparse
import sys
from collections.abc import Sequence

from servery import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the servery argument parser."""
    parser = argparse.ArgumentParser(
        prog="servery",
        description="Zero-dependency, pure-Python HTTP file server.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"servery {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the servery CLI. Returns a process exit code."""
    parser = build_parser()
    parser.parse_args(argv)
    print(
        "servery is not implemented yet — see docs/ROADMAP.md for the plan.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
