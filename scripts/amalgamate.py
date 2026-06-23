#!/usr/bin/env python3
"""Generate a single-file, stdlib-only servery that runs straight from a pipe.

Every ``servery/*.py`` module is embedded as source into one script with a tiny
in-memory import hook, so the package's absolute ``from servery.x import y``
imports resolve with no package on disk. Pure standard library — no third-party
bundler — so the output is something we generate and can fully audit.

Writes the amalgamation to stdout; ``scripts/bundle.sh`` drives it and self-tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "servery"

_HEADER = '''\
#!/usr/bin/env python3
# servery {version} — single-file build (zero dependencies, pure standard library).
#
# GENERATED amalgamation of the servery package — every module embedded into one
# file so it runs straight from a pipe:
#
#     curl -fsSL <url> | python3 - ./public -p 8000
#
# It is exactly the released package, just concatenated. Audit the real, unbundled
# source at https://github.com/mjbommar/servery (or `pip install servery`).
# Do not edit by hand; regenerate with scripts/bundle.sh.
"""servery {version} single-file build. See https://github.com/mjbommar/servery"""

import importlib.util
import sys
'''

_LOADER = '''

class _ServeryBundleLoader:
    """Import servery modules from the embedded ``_SERVERY_SOURCES`` table."""

    def find_spec(self, name, path=None, target=None):
        if name not in _SERVERY_SOURCES:
            return None
        return importlib.util.spec_from_loader(
            name, self, origin="servery-bundle", is_package=(name in _SERVERY_PACKAGES)
        )

    def create_module(self, spec):
        return None  # use the default module object

    def exec_module(self, module):
        code = compile(_SERVERY_SOURCES[module.__name__], f"<servery:{module.__name__}>", "exec")
        exec(code, module.__dict__)  # noqa: S102 - executing our own embedded source


sys.meta_path.insert(0, _ServeryBundleLoader())

if __name__ == "__main__":
    from servery.cli import main

    raise SystemExit(main())
'''


def _iter_modules():
    """Yield (dotted_name, is_package, source) for every module under servery/."""
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG.parent).with_suffix("")  # e.g. servery/http2/frames
        parts = list(rel.parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts.pop()
        yield ".".join(parts), is_package, path.read_text(encoding="utf-8")


def build() -> str:
    sys.path.insert(0, str(PKG.parent))
    from servery._version import __version__

    modules = list(_iter_modules())
    packages = sorted(name for name, is_pkg, _ in modules if is_pkg)

    out = [_HEADER.format(version=__version__)]
    out.append("\n_SERVERY_SOURCES = {")
    for name, _is_pkg, source in modules:
        out.append(f"    {name!r}: {source!r},")
    out.append("}")
    out.append(f"_SERVERY_PACKAGES = frozenset({packages!r})")
    out.append(_LOADER)
    return "\n".join(out)


if __name__ == "__main__":
    sys.stdout.write(build())
