from __future__ import annotations

import http.server
import socket
import threading
import unittest
from typing import ClassVar, cast

from scripts.loadgen import LoadCohort, _LatencySampler, run_load, run_mixed_load

_BODY = b"loadgen-body"


class _FixedHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received_body_sizes: ClassVar[list[int]] = []

    def do_GET(self) -> None:
        if self.path == "/not-modified" and self.headers.get("If-Modified-Since"):
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.received_body_sizes.append(len(body))
        self.do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class LoadGeneratorTest(unittest.TestCase):
    def test_latency_sampler_retains_a_bounded_deterministic_reservoir(self) -> None:
        first = _LatencySampler(10, seed=7)
        second = _LatencySampler(10, seed=7)
        for value in range(100):
            first.add(float(value))
            second.add(float(value))

        self.assertEqual(first.seen, 100)
        self.assertEqual(len(first.samples), 10)
        self.assertEqual(first.samples, second.samples)
        self.assertNotEqual(first.samples, [float(value) for value in range(10)])

    def test_programmatic_keepalive_run(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/payload",
                concurrency=2,
                duration=0.1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        requests = cast(int, result["requests"])
        self.assertGreater(requests, 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["status_errors"], 0)
        self.assertEqual(result["transport_errors"], 0)
        self.assertEqual(result["status_counts"], {"200": requests})
        intervals = cast(dict[str, int], result["completion_intervals"])
        self.assertEqual(sum(intervals.values()), requests)
        self.assertEqual(result["bytes"], requests * len(_BODY))
        self.assertGreater(cast(float, result["rps"]), 0)
        self.assertEqual(result["latency_samples_seen"], requests)
        self.assertEqual(result["latency_samples_retained"], requests)
        self.assertEqual(result["latency_sampling"], "all")

    def test_programmatic_run_can_bound_latency_samples(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/payload",
                concurrency=2,
                duration=0.1,
                max_latency_samples=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        requests = cast(int, result["requests"])
        self.assertGreater(requests, 5)
        self.assertEqual(result["latency_samples_seen"], requests)
        self.assertEqual(result["latency_samples_retained"], 5)
        self.assertEqual(result["latency_sampling"], "reservoir-stratified")
        self.assertEqual(result["max_latency_samples"], 5)

    def test_persistent_warmup_is_excluded_from_timed_accounting(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/payload",
                concurrency=1,
                warmup=0.05,
                connection_ramp=0.01,
                duration=0.05,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result["warmup_s"], 0.05)
        self.assertEqual(result["connection_ramp_s"], 0.01)
        self.assertGreater(cast(int, result["requests"]), 0)
        self.assertLess(cast(float, result["elapsed_s"]), 0.2)

    def test_programmatic_throttled_reader_run(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/payload",
                concurrency=1,
                duration=0.05,
                read_chunk_size=2,
                read_delay=0.001,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        requests = cast(int, result["requests"])
        self.assertGreater(requests, 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["bytes"], requests * len(_BODY))

    def test_programmatic_request_body_run(self) -> None:
        _FixedHandler.received_body_sizes.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/body",
                concurrency=1,
                duration=0.05,
                request_body_size=64 * 1024,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertGreater(cast(int, result["requests"]), 0)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(_FixedHandler.received_body_sizes)
        self.assertEqual(set(_FixedHandler.received_body_sizes), {64 * 1024})

    def test_programmatic_bodyless_conditional_run_with_custom_header(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_load(
                f"http://127.0.0.1:{server.server_port}/not-modified",
                concurrency=2,
                duration=0.1,
                expected_status=304,
                request_headers=(("If-Modified-Since", "Wed, 21 Oct 2099 07:28:00 GMT"),),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertGreater(cast(int, result["requests"]), 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["bytes"], 0)

    def test_rejects_more_processes_than_connections(self) -> None:
        with self.assertRaisesRegex(ValueError, "procs cannot exceed concurrency"):
            run_load("http://127.0.0.1/", concurrency=1, procs=2)

    def test_mixed_load_reports_simultaneous_cohorts_separately(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixedHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            result = run_mixed_load(
                (
                    LoadCohort("cheap", f"{base}/small", concurrency=2),
                    LoadCohort(
                        "slow-reader",
                        f"{base}/large",
                        concurrency=1,
                        read_chunk_size=2,
                        read_delay=0.001,
                    ),
                ),
                duration=0.1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        cohorts = cast(dict[str, dict[str, object]], result["cohorts"])
        self.assertEqual(set(cohorts), {"cheap", "slow-reader"})
        for cohort in cohorts.values():
            self.assertGreater(cast(int, cohort["requests"]), 0)
            self.assertEqual(cohort["errors"], 0)
            self.assertEqual(cohort["status_errors"], 0)
            self.assertEqual(
                sum(cast(dict[str, int], cohort["completion_intervals"]).values()),
                cohort["requests"],
            )
        self.assertGreater(cast(float, cohorts["cheap"]["rps"]), 0)
        self.assertGreater(cast(float, cohorts["slow-reader"]["rps"]), 0)

    def test_mixed_load_rejects_ambiguous_cohorts(self) -> None:
        one = LoadCohort("one", "http://127.0.0.1/", concurrency=1)
        with self.assertRaisesRegex(ValueError, "at least two"):
            run_mixed_load((one,), duration=0.01)
        with self.assertRaisesRegex(ValueError, "unique"):
            run_mixed_load((one, one), duration=0.01)
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            run_mixed_load(
                (one, LoadCohort("", "http://127.0.0.1/", concurrency=1)),
                duration=0.01,
            )

    def test_unavailable_endpoint_still_obeys_short_run_deadline(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = run_load(
            f"http://127.0.0.1:{port}/",
            concurrency=2,
            duration=0.05,
        )

        self.assertEqual(result["requests"], 0)
        self.assertGreater(cast(int, result["errors"]), 0)
        self.assertEqual(result["status_errors"], 0)
        self.assertEqual(result["transport_errors"], result["errors"])
        self.assertEqual(result["status_counts"], {})
        self.assertLess(cast(float, result["elapsed_s"]), 0.5)

    def test_rejects_invalid_reader_throttling(self) -> None:
        with self.assertRaisesRegex(ValueError, "read_chunk_size must be positive"):
            run_load("http://127.0.0.1/", read_chunk_size=0)
        with self.assertRaisesRegex(ValueError, "read_delay cannot be negative"):
            run_load("http://127.0.0.1/", read_delay=-0.001)
        with self.assertRaisesRegex(ValueError, "request_body_size cannot be negative"):
            run_load("http://127.0.0.1/", request_body_size=-1)
        with self.assertRaisesRegex(ValueError, "max_latency_samples must be positive"):
            run_load("http://127.0.0.1/", max_latency_samples=0)
        with self.assertRaisesRegex(ValueError, "warmup cannot be negative"):
            run_load("http://127.0.0.1/", warmup=-1)
        with self.assertRaisesRegex(ValueError, "connection_ramp cannot be negative"):
            run_load("http://127.0.0.1/", connection_ramp=-1)

    def test_rejects_unsafe_or_unencodable_request_headers(self) -> None:
        invalid = (
            (("Bad:Name", "value"),),
            (("Name", "line\r\nbreak"),),
            (("Náme", "value"),),
            (("Name", "snowman \N{SNOWMAN}"),),
        )
        for headers in invalid:
            with (
                self.subTest(headers=headers),
                self.assertRaisesRegex(ValueError, "request header"),
            ):
                run_load("http://127.0.0.1/", request_headers=headers)


if __name__ == "__main__":
    unittest.main()
