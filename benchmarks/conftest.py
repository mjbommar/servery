"""Shared fixtures for the benchmark suite."""

from __future__ import annotations

from pathlib import Path

import pytest

SMALL = b"x" * 1024  # 1 KiB
LARGE_MIB = 8  # large-file / sendfile body size
LISTING_FILES = 50


@pytest.fixture(scope="session")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A served directory: a 1 KiB file, an 8 MiB file, and 50 listing entries."""
    root = tmp_path_factory.mktemp("bench-tree")
    (root / "small.txt").write_bytes(SMALL)
    (root / "large.bin").write_bytes(b"x" * (LARGE_MIB * 1024 * 1024))
    for i in range(LISTING_FILES):
        (root / f"file{i:02d}.txt").write_text("listing entry")
    return root
