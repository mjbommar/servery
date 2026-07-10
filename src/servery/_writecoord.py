"""Per-target write coordination for the threaded write surfaces."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(slots=True)
class _Entry:
    lock: threading.RLock
    users: int = 0


class TargetLocks:
    """A leak-free registry of locks keyed by canonical target path."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    def _borrow(self, key: str) -> _Entry:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(threading.RLock())
                self._entries[key] = entry
            entry.users += 1
            return entry

    def _return(self, key: str, entry: _Entry) -> None:
        with self._guard:
            entry.users -= 1
            if entry.users == 0:
                self._entries.pop(key, None)

    @contextlib.contextmanager
    def hold(self, path: str, timeout: float = 0.0) -> Iterator[bool]:
        """Yield whether the target lock was acquired within ``timeout``."""
        key = self._key(path)
        entry = self._borrow(key)
        acquired = entry.lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                entry.lock.release()
            self._return(key, entry)

    @contextlib.contextmanager
    def hold_many(self, paths: list[str], timeout: float = 0.0) -> Iterator[bool]:
        """Acquire several targets in stable order, avoiding lock-order deadlocks."""
        unique = sorted({self._key(path) for path in paths})
        borrowed: list[tuple[str, _Entry]] = []
        acquired: list[_Entry] = []
        ok = True
        try:
            for key in unique:
                entry = self._borrow(key)
                borrowed.append((key, entry))
                if not entry.lock.acquire(timeout=timeout):
                    ok = False
                    break
                acquired.append(entry)
            yield ok
        finally:
            for entry in reversed(acquired):
                entry.lock.release()
            for key, entry in reversed(borrowed):
                self._return(key, entry)
