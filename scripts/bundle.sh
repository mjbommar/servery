#!/usr/bin/env bash
# Build the single-file servery distributions and self-test each one:
#   dist/servery.py   — stdlib amalgamation, runs from a pipe (curl … | python3 -)
#   dist/servery.pyz  — stdlib zipapp, a download-and-run single file
# Both are pure standard library (zero runtime dependencies).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist

version="$(uv run python -c 'from servery._version import __version__; print(__version__)')"

echo ">> building single-file artifacts for servery ${version}"
uv run python scripts/amalgamate.py > dist/servery.py
rm -f dist/servery.pyz
uv run python -m zipapp src -m "servery.cli:main" -o dist/servery.pyz -p "/usr/bin/env python3"

# Serve a temp file through each artifact and assert the byte comes back.
self_test() {
  local label="$1" port="$2"; shift 2
  local tmp; tmp="$(mktemp -d)"
  echo "bundle-ok" > "${tmp}/probe.txt"
  "$@" "${tmp}" -p "${port}" -q &
  local pid=$!
  sleep 1.5
  local got=""; got="$(curl -fsS "http://127.0.0.1:${port}/probe.txt" 2>/dev/null || true)"
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  rm -rf "${tmp}"
  if [[ "${got}" == "bundle-ok" ]]; then
    echo "  ✓ ${label} serves correctly"
  else
    echo "  ✗ ${label} FAILED (got: '${got}')" >&2
    return 1
  fi
}

echo ">> self-test"
self_test "dist/servery.py" 8761 python3 dist/servery.py
self_test "dist/servery.pyz" 8762 python3 dist/servery.pyz

echo ">> done"
ls -lh dist/servery.py dist/servery.pyz | awk '{print "   "$5"\t"$9}'
