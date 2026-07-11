from __future__ import annotations

import asyncio
import io
import socket
import unittest
from typing import cast

from servery import _write


class _RecordingSocket:
    def __init__(self) -> None:
        self.timeout: float | None = 30.0
        self.changes: list[float | None] = []

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value
        self.changes.append(value)


class WriteDeadlineTest(unittest.TestCase):
    def test_sync_writer_scopes_and_restores_socket_timeout(self) -> None:
        sock = _RecordingSocket()
        raw = io.BytesIO()
        writer = _write.DeadlineWriter(raw, cast("socket.socket", sock), 0.5)

        self.assertEqual(writer.write(b"payload"), 7)
        self.assertEqual(raw.getvalue(), b"payload")
        self.assertEqual(sock.timeout, 30.0)
        self.assertEqual(sock.changes, [0.5, 30.0])

    def test_async_drain_without_deadline_has_no_timer(self) -> None:
        class Writer:
            def __init__(self) -> None:
                self.drains = 0

            async def drain(self) -> None:
                self.drains += 1

        async def exercise() -> int:
            writer = Writer()
            await _write.drain(writer, None)
            return writer.drains

        self.assertEqual(asyncio.run(exercise()), 1)

    def test_async_drain_deadline_cancels_stalled_wait(self) -> None:
        class Writer:
            async def drain(self) -> None:
                await asyncio.Future()

        async def exercise() -> None:
            with self.assertRaises(TimeoutError):
                await _write.drain(Writer(), 0.01)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
