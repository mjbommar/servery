#!/usr/bin/env python3
"""Fail the build if the wheel declares any runtime dependencies.

servery's defining promise is zero third-party runtime dependencies. This gate
parses the built wheel's METADATA and asserts there are no (non-extra)
``Requires-Dist`` entries. Pure standard library, naturally.

Run after ``uv build`` (or ``python -m build``):

    python scripts/check_zero_deps.py
"""

import email
import sys
import zipfile
from pathlib import Path


def runtime_requires(wheel: Path) -> list[str]:
    """Return the wheel's runtime Requires-Dist, excluding optional extras."""
    with zipfile.ZipFile(wheel) as zf:
        metadata_name = next(name for name in zf.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(zf.read(metadata_name))
    # Dependencies gated behind an extra (e.g. "; extra == 'dev'") are optional
    # and never installed by default, so they don't count against the mandate.
    return [req for req in metadata.get_all("Requires-Dist", []) if "extra ==" not in req]


def main() -> int:
    wheels = sorted(Path("dist").glob("*.whl"))
    if not wheels:
        print("no wheel found in dist/ — run `uv build` first", file=sys.stderr)
        return 1

    failed = False
    for wheel in wheels:
        reqs = runtime_requires(wheel)
        if reqs:
            failed = True
            print(f"FAIL {wheel.name}: runtime dependencies present: {reqs}", file=sys.stderr)
        else:
            print(f"OK   {wheel.name}: zero runtime dependencies")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
