#!/usr/bin/env python3
"""Measure bounded directory rendering at 1k/10k/100k entries.

The files are created before each timed sample. Evidence includes elapsed time,
rendered page bytes, and Python peak allocation; it is intended for trend artifacts,
not a strict noisy-runner regression gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

from servery import listing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[1000, 10_000, 100_000])
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    counts = sorted(set(args.counts))
    if not counts or counts[0] <= 0:
        parser.error("--counts must contain positive integers")

    samples: list[dict[str, float | int]] = []
    evidence: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "page_size": 1000,
        "details_threshold": 10_000,
        "samples": samples,
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        created = 0
        for count in counts:
            for index in range(created, count):
                root.joinpath(f"entry-{index:06d}.txt").touch()
            created = count
            tracemalloc.start()
            started = time.perf_counter()
            body = listing.render(
                str(root),
                "/",
                show_hidden=True,
                per_page=1000,
                max_entries=count,
                details_threshold=min(10_000, count),
            )
            elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            sample = {
                "entries": count,
                "elapsed_ms": elapsed * 1000,
                "python_peak_mib": peak / 1024 / 1024,
                "rendered_bytes": len(body),
            }
            samples.append(sample)
            print(
                f"{count:>7,} entries  {sample['elapsed_ms']:>9.1f} ms  "
                f"python peak {sample['python_peak_mib']:>7.1f} MiB  "
                f"page {sample['rendered_bytes']:>8,} B"
            )

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(evidence, indent=2) + "\n")
    print(f">> wrote {args.json}")


if __name__ == "__main__":
    main()
