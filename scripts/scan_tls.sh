#!/usr/bin/env bash
# Audit servery's HTTPS/TLS with testssl.sh — the industry-standard TLS scanner
# (https://testssl.sh). This is the SSL/TLS analogue of running h2spec for HTTP/2.
#
# Usage:
#   scripts/scan_tls.sh                # spin up `servery --tls-self-signed` and scan it
#   scripts/scan_tls.sh HOST:PORT      # scan an already-running HTTPS endpoint
#
# Requires: bash, git, openssl. servery must be importable (run after `uv sync`,
# or the script will fall back to `uv run` if uv is on PATH).
set -euo pipefail

target="${1:-}"
srv_pid=""
srv_dir=""
cleanup() {
  [ -n "$srv_pid" ] && kill "$srv_pid" 2>/dev/null || true
  [ -n "$srv_dir" ] && rm -rf "$srv_dir" || true
}
trap cleanup EXIT

# Locate testssl.sh, cloning it once into a cache dir if it isn't already around.
if command -v testssl.sh >/dev/null 2>&1; then
  testssl=testssl.sh
else
  cache="${TMPDIR:-/tmp}/servery-testssl.sh"
  if [ ! -x "$cache/testssl.sh" ]; then
    echo "fetching testssl.sh -> $cache ..."
    git clone --depth 1 https://github.com/testssl/testssl.sh.git "$cache" >/dev/null
  fi
  testssl="$cache/testssl.sh"
fi

# Pick how to launch servery (prefer the project's uv env if present).
if command -v uv >/dev/null 2>&1; then
  servery=(uv run python -m servery)
else
  servery=(python -m servery)
fi

if [ -z "$target" ]; then
  srv_dir="$(mktemp -d)"
  echo "scan target" >"$srv_dir/index.html"
  "${servery[@]}" -p 8443 -q --tls-self-signed "$srv_dir" >/dev/null 2>&1 &
  srv_pid=$!
  sleep 3
  target="127.0.0.1:8443"
  echo "started servery --tls-self-signed on $target (pid $srv_pid)"
fi

echo "scanning https://$target with testssl.sh ..."
exec "$testssl" --quiet --color 0 --protocols --server-defaults --fs --vulnerable "$target"
