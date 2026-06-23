#!/usr/bin/env bash
# Run the servery benchmark suite and save a reproducible JSON artifact.
#
#   scripts/run_benchmarks.sh                 # run all (no-aioquic) benchmarks
#   scripts/run_benchmarks.sh --compare       # FAIL if median regresses >20% vs last save
#   scripts/run_benchmarks.sh --http3         # ALSO run the aioquic HTTP/3 e2e (GIL build)
#
# pytest-benchmark autosaves a machine-keyed run under .benchmarks/ (gitignored) and
# this script also writes a timestamped JSON under benchmarks/artifacts/ for sharing.
# `--compare` uses the most recent autosave as the baseline.
set -euo pipefail
cd "$(dirname "$0")/.."

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p benchmarks/artifacts
json="benchmarks/artifacts/bench-${stamp}.json"

compare_args=()
http3=0
for arg in "$@"; do
  case "$arg" in
    --compare) compare_args=(--benchmark-compare --benchmark-compare-fail=median:20%) ;;
    --http3) http3=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

echo ">> core benchmark suite (default interpreter)"
uv run --group bench pytest benchmarks/ \
  --benchmark-autosave \
  --benchmark-json="$json" \
  "${compare_args[@]}"
echo ">> wrote $json"

if [[ "$http3" == 1 ]]; then
  echo ">> HTTP/3 e2e (aioquic, GIL build via --python 3.13 --extra http3)"
  uv run --python 3.13 --group bench --extra http3 pytest \
    benchmarks/test_bench_http3.py -k end_to_end \
    --benchmark-json="benchmarks/artifacts/bench-h3-${stamp}.json"
fi
