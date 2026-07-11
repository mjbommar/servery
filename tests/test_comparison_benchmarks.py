from __future__ import annotations

import argparse
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from benchmarks.comparison.apps import asgi_app, expected_body, wsgi_app
from scripts.compare_servers import (
    APP_FIXTURE,
    FASTAPI_APP_FIXTURE,
    SERVER_FAMILIES,
    _docker_python_runtime,
    _paired_summary,
    _python_runtime,
    _scenario_allows_server,
    _summary,
    caddy_config,
    format_cpu_set,
    nginx_config,
    parse_cpu_set,
    process_tree_cpu,
    process_tree_snapshot,
    resolve_scenario_expectation,
    scenarios,
    server_specs,
)


class ComparisonAppsTest(unittest.TestCase):
    def test_wsgi_and_asgi_return_identical_dynamic_body(self) -> None:
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured.update(status=status, headers=headers)

        wsgi_body = b"".join(wsgi_app({"PATH_INFO": "/bytes/1024"}, start_response))
        sent: list[dict[str, object]] = []
        received = False

        async def receive() -> dict[str, object]:
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        asyncio.run(asgi_app({"type": "http", "path": "/bytes/1024"}, receive, send))
        asgi_body = sent[-1]["body"]

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(wsgi_body, expected_body("/bytes/1024"))
        self.assertEqual(asgi_body, wsgi_body)
        self.assertEqual(len(wsgi_body), 1024)

    def test_wsgi_and_asgi_stream_identical_chunks(self) -> None:
        for path, chunk_size in (("/stream/65536", 4 * 1024), ("/stream/1048576", 64 * 1024)):
            with self.subTest(path=path):
                self._assert_identical_stream(path, chunk_size)

    def test_long_stream_allocates_distinct_chunks(self) -> None:
        def start_response(_status: str, _headers: list[tuple[str, str]]) -> None:
            pass

        chunks = iter(wsgi_app({"PATH_INFO": "/stream/67108864"}, start_response))
        first = next(chunks)
        second = next(chunks)
        self.assertEqual(len(first), 64 * 1024)
        self.assertEqual(len(second), 64 * 1024)
        self.assertIsNot(first, second)
        self.assertNotEqual(first[:8], second[:8])

    def _assert_identical_stream(self, path: str, chunk_size: int) -> None:
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured.update(status=status, headers=headers)

        wsgi_chunks = list(wsgi_app({"PATH_INFO": path}, start_response))
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        asyncio.run(asgi_app({"type": "http", "path": path}, receive, send))
        asgi_chunks = [
            message["body"] for message in sent if message["type"] == "http.response.body"
        ]

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(len(wsgi_chunks), 16)
        self.assertTrue(all(len(chunk) == chunk_size for chunk in wsgi_chunks))
        self.assertEqual(asgi_chunks, wsgi_chunks)
        self.assertEqual(b"".join(wsgi_chunks), expected_body(path))


class ComparisonHarnessTest(unittest.TestCase):
    def test_python_runtime_metadata_is_structured(self) -> None:
        runtime = _python_runtime()
        self.assertIsInstance(runtime["version"], str)
        self.assertIsInstance(runtime["implementation"], str)
        self.assertIsInstance(runtime["gil_api_available"], bool)
        self.assertIsInstance(runtime["gil_enabled"], bool)

    def test_docker_python_runtime_parses_last_json_line(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                'notice\n{"implementation":"CPython","version":"3.15.0b3",'
                '"gil_api_available":true,"gil_enabled":false}\n'
            ),
            stderr="",
        )
        with mock.patch("scripts.compare_servers._run", return_value=completed):
            runtime = _docker_python_runtime("candidate")
        self.assertEqual(runtime["version"], "3.15.0b3")
        self.assertFalse(runtime["gil_enabled"])

    def test_process_tree_snapshot_aggregates_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            self._write_proc_process(proc, 100, 1, "supervisor", 10, 5, 2048, 1536)
            self._write_proc_process(proc, 101, 100, "worker one", 20, 10, 4096, 3072)
            self._write_proc_process(proc, 102, 101, "worker-two", 5, 5, 1024, None)
            self._write_proc_process(proc, 200, 1, "unrelated", 100, 100, 8192, 8192)
            snapshot = process_tree_snapshot(100, proc_root=proc)

        self.assertEqual(snapshot["process_count"], 3)
        self.assertEqual(snapshot["rss_mib"], 7.0)
        self.assertEqual(snapshot["pss_mib"], 4.5)
        processes = cast(list[dict[str, object]], snapshot["processes"])
        self.assertEqual(
            [row["pid"] for row in processes],
            [100, 101, 102],
        )

    def test_process_tree_cpu_reports_average_cores(self) -> None:
        before: dict[str, object] = {
            "processes": [
                {"pid": 100, "cpu_ticks": 10},
                {"pid": 101, "cpu_ticks": 20},
            ]
        }
        after: dict[str, object] = {
            "processes": [
                {"pid": 100, "cpu_ticks": 30},
                {"pid": 101, "cpu_ticks": 50},
            ]
        }
        with mock.patch("scripts.compare_servers.os.sysconf", return_value=100, create=True):
            cpu = process_tree_cpu(before, after, 0.25)
        self.assertEqual(cpu["cpu_seconds"], 0.5)
        self.assertEqual(cpu["average_cores"], 2.0)

    @staticmethod
    def _write_proc_process(
        proc: Path,
        pid: int,
        ppid: int,
        name: str,
        utime: int,
        stime: int,
        rss_kib: int,
        pss_kib: int | None,
    ) -> None:
        directory = proc / str(pid)
        directory.mkdir()
        # Fields after comm begin with state (3); pad through stime (15).
        fields = ["S", str(ppid), *(["0"] * 9), str(utime), str(stime)]
        (directory / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
        (directory / "status").write_text(f"Name:\t{name}\nVmRSS:\t{rss_kib} kB\n")
        if pss_kib is not None:
            (directory / "smaps_rollup").write_text(f"Pss:\t{pss_kib} kB\n")

    def test_scenario_names_are_unique_and_cover_all_families(self) -> None:
        declared = scenarios()
        self.assertEqual(len({item.name for item in declared}), len(declared))
        self.assertEqual({item.family for item in declared}, {"static", "wsgi", "asgi"})
        self.assertTrue(any(item.connection_close for item in declared))
        self.assertTrue(any(not item.default for item in declared))
        self.assertEqual(set(SERVER_FAMILIES), {"static", "wsgi", "asgi"})
        asgi_churn = next(item for item in declared if item.name == "asgi-churn-1k")
        self.assertTrue(asgi_churn.connection_close)
        self.assertEqual(asgi_churn.concurrency_cap, 32)
        asgi_headers = next(item for item in declared if item.name == "asgi-headers-32")
        # Host and Connection are emitted by loadgen, for 32 total fields.
        self.assertEqual(len(asgi_headers.request_headers), 30)
        self.assertFalse(asgi_headers.default)
        body_scenarios = {
            item.name: item for item in declared if item.name in {"wsgi-body-64k", "asgi-body-64k"}
        }
        self.assertEqual(set(body_scenarios), {"wsgi-body-64k", "asgi-body-64k"})
        self.assertTrue(
            all(item.request_body_size == 64 * 1024 for item in body_scenarios.values())
        )
        self.assertTrue(all(item.concurrency_cap == 32 for item in body_scenarios.values()))
        self.assertEqual(
            {item.name for item in declared if item.path.startswith("/stream/")},
            {
                "wsgi-stream-64k",
                "wsgi-stream-1m",
                "asgi-stream-64k",
                "asgi-stream-1m",
                "asgi-slow-reader-64m",
            },
        )

        slow = next(item for item in declared if item.name == "asgi-slow-reader-64m")
        self.assertEqual(slow.expected_length, 64 * 1024 * 1024)
        self.assertEqual(slow.concurrency_cap, 4)
        self.assertEqual(slow.read_chunk_size, 16 * 1024)
        self.assertEqual(slow.read_delay, 0.001)

        byte_range = next(item for item in declared if item.name == "static-range-64k")
        self.assertEqual(byte_range.expected_status, 206)
        self.assertEqual(byte_range.request_headers, (("Range", "bytes=0-1023"),))
        self.assertEqual(byte_range.expected_length, 1024)
        self.assertFalse(byte_range.default)

        not_modified = next(item for item in declared if item.name == "static-not-modified")
        self.assertEqual(not_modified.expected_status, 304)
        self.assertEqual(not_modified.expected_length, 0)

        digest = next(item for item in declared if item.name == "static-digest-miss-64k")
        self.assertEqual(digest.request_headers, (("Want-Repr-Digest", "sha-256"),))
        self.assertEqual(digest.expected_headers[0][0], "Repr-Digest")
        self.assertTrue(digest.expected_headers[0][1].startswith("sha-256=:"))
        self.assertEqual(
            digest.servers,
            (
                "servery-digest-miss",
                "servery-digest-miss-baseline",
                "servery-selector-digest-miss",
            ),
        )

        generated = next(item for item in declared if item.name == "static-listing-100")
        self.assertEqual(generated.expected_fixture, "listing-100")
        self.assertEqual(generated.path, "/listing/")
        self.assertEqual(
            generated.servers,
            (
                "servery",
                "servery-baseline",
                "servery-selector-listing-w1",
                "servery-selector-listing-w4",
            ),
        )
        generated_large = next(item for item in declared if item.name == "static-listing-1000")
        self.assertEqual(generated_large.expected_fixture, "listing-1000")
        self.assertEqual(generated_large.concurrency_cap, 16)
        self.assertTrue(not_modified.request_headers)
        self.assertFalse(not_modified.default)

        indexed = next(item for item in declared if item.name == "static-index-1k")
        self.assertEqual(indexed.path, "/indexed/")
        self.assertEqual(indexed.expected_length, 1024)
        self.assertFalse(indexed.default)

        download = next(item for item in declared if item.name == "static-download-1k")
        self.assertEqual(download.path, "/small.bin?download=1")
        self.assertEqual(download.expected_headers[0][0], "Content-Disposition")
        self.assertIn("servery-selector-prototype", download.servers or ())

        spa = next(item for item in declared if item.name == "static-spa-1k")
        self.assertEqual(spa.path, "/client/side/route")
        self.assertEqual(
            spa.servers,
            ("servery-spa", "servery-selector-prototype-spa"),
        )

        gzip_cache = next(item for item in declared if item.name == "static-gzip-cache-64k")
        self.assertEqual(gzip_cache.request_headers, (("Accept-Encoding", "gzip"),))
        self.assertIn(("Content-Encoding", "gzip"), gzip_cache.expected_headers)
        self.assertIn("servery-gzip-cache-baseline", gzip_cache.servers or ())

        gzip_miss = next(item for item in declared if item.name == "static-gzip-miss-64k")
        self.assertNotIn("servery-gzip-cache", gzip_miss.servers or ())
        self.assertIn("servery-gzip-miss-baseline", gzip_miss.servers or ())

        frameworks = {item.name: item for item in declared if item.app_spec is not None}
        self.assertEqual(
            set(frameworks),
            {
                "asgi-starlette-json",
                "asgi-starlette-stream-64k",
                "asgi-fastapi-json",
                "asgi-fastapi-validation",
            },
        )
        self.assertTrue(
            all(
                item.servers == ("servery-asgi", "uvicorn", "uvicorn-native")
                for item in frameworks.values()
            )
        )
        self.assertEqual(frameworks["asgi-starlette-stream-64k"].expected_length, 64 * 1024)
        self.assertEqual(frameworks["asgi-fastapi-validation"].expected_status, 422)

    def test_cpu_set_round_trip(self) -> None:
        self.assertEqual(parse_cpu_set("0,2-4"), {0, 2, 3, 4})
        self.assertEqual(format_cpu_set({4, 0, 2, 3}), "0,2,3,4")
        for invalid in ("", "x", "4-2", "1,,2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_cpu_set(invalid)

    def test_static_configs_use_common_plaintext_endpoint(self) -> None:
        self.assertIn("listen 127.0.0.1:8123", nginx_config(8123, 1))
        caddy = caddy_config(8123)
        self.assertIn("http://127.0.0.1:8123", caddy)
        self.assertIn("auto_https off", caddy)

    def test_static_adapters_mount_the_same_corpus_read_only(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            concurrency=64,
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                scenarios()[0],
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertEqual([spec.name for spec in specs], ["servery", "nginx", "caddy"])
        self.assertTrue(all(spec.mounts[0] == (corpus, "/srv", True) for spec in specs))
        self.assertEqual(specs[2].command[:2], ("caddy", "run"))

    def test_servery_worker_pool_is_an_explicit_comparison_control(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=32,
            servery_small_file_buffer=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertIn("--max-workers", specs[0].command)
        self.assertEqual(specs[0].command[specs[0].command.index("--max-workers") + 1], "32")
        self.assertEqual(specs[0].command[specs[0].command.index("--workers") + 1], "1")

    def test_servery_process_count_reaches_production_candidates_only(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=4,
            servery_max_workers=16,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            server=None,
        )
        selected = (
            ("static-1k", "servery", "servery-baseline"),
            ("wsgi-1k", "servery-wsgi", "servery-wsgi-baseline"),
            ("asgi-1k", "servery-asgi", "servery-asgi-baseline"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            for scenario_name, candidate_name, baseline_name in selected:
                with self.subTest(scenario=scenario_name):
                    specs = server_specs(
                        next(item for item in scenarios() if item.name == scenario_name),
                        port=8123,
                        concurrency=64,
                        corpus=corpus,
                        config_dir=config,
                        args=args,
                    )
                    candidate = next(spec for spec in specs if spec.name == candidate_name)
                    baseline = next(spec for spec in specs if spec.name == baseline_name)

                    self.assertEqual(
                        candidate.command[candidate.command.index("--workers") + 1], "4"
                    )
                    self.assertEqual(
                        candidate.command[candidate.command.index("--max-workers") + 1], "16"
                    )
                    self.assertNotIn("--workers", baseline.command)

    def test_selector_spike_is_explicit_and_uses_the_same_corpus(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            include_selector_spike=True,
            server=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        spike = next(spec for spec in specs if spec.name == "servery-selector-spike")
        self.assertIn("benchmarks.comparison.selector_spike", spike.command)
        self.assertEqual(spike.mounts, ((corpus, "/srv", True),))

    def test_selector_prototype_is_explicit_and_bounded(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            include_selector_spike=False,
            server=["servery-selector-prototype", "servery-selector-prototype-fs4"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        prototype = next(spec for spec in specs if spec.name == "servery-selector-prototype")
        self.assertIn("benchmarks.comparison.selector_prototype", prototype.command)
        self.assertIn("--max-connections", prototype.command)
        self.assertEqual(prototype.mounts, ((corpus, "/srv", True),))
        pooled = next(spec for spec in specs if spec.name == "servery-selector-prototype-fs4")
        self.assertIn("--filesystem-workers", pooled.command)
        self.assertIn("--filesystem-queue", pooled.command)

    def test_capability_scoped_download_and_spa_adapters_are_explicit(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            include_selector_spike=False,
            server=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            download_specs = server_specs(
                next(item for item in scenarios() if item.name == "static-download-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )
            spa_specs = server_specs(
                next(item for item in scenarios() if item.name == "static-spa-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        download_selector = next(
            spec for spec in download_specs if spec.name == "servery-selector-prototype"
        )
        self.assertNotIn("--spa", download_selector.command)
        production_spa = next(spec for spec in spa_specs if spec.name == "servery-spa")
        selector_spa = next(
            spec for spec in spa_specs if spec.name == "servery-selector-prototype-spa"
        )
        self.assertIn("--spa", production_spa.command)
        self.assertIn("--spa", selector_spa.command)

    def test_gzip_adapters_make_cache_and_worker_policy_explicit(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            include_selector_spike=False,
            server=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-gzip-cache-64k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        production = next(spec for spec in specs if spec.name == "servery-gzip-cache")
        baseline = next(spec for spec in specs if spec.name == "servery-gzip-cache-baseline")
        selector = next(spec for spec in specs if spec.name == "servery-selector-gzip-cache")
        self.assertIn("--compression-cache-size", production.command)
        self.assertIn("--compression-cache-size", baseline.command)
        self.assertEqual(baseline.image, "baseline")
        self.assertIn("--compression-workers", selector.command)
        self.assertIn("--compression-queue", selector.command)

    def test_digest_adapters_make_zero_retention_and_worker_policy_explicit(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            include_selector_spike=False,
            server=None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-digest-miss-64k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        production = next(spec for spec in specs if spec.name == "servery-digest-miss")
        baseline = next(spec for spec in specs if spec.name == "servery-digest-miss-baseline")
        selector = next(spec for spec in specs if spec.name == "servery-selector-digest-miss")
        self.assertEqual(production.image, "candidate")
        self.assertEqual(baseline.image, "baseline")
        self.assertNotIn("--digest-cache-size", selector.command)
        self.assertIn("--digest-workers", selector.command)
        self.assertIn("--digest-queue", selector.command)

    def test_access_log_scenario_pairs_unlogged_and_explicit_overflow_policies(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            include_selector_spike=False,
            server=None,
        )
        scenario = next(item for item in scenarios() if item.name == "static-access-log-1k")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                scenario,
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        by_name = {spec.name: spec for spec in specs}
        self.assertEqual(set(scenario.servers or ()), set(by_name) - {"nginx", "caddy"})
        self.assertEqual(by_name["servery-access-log"].image, "candidate")
        self.assertEqual(by_name["servery-access-log-sync"].image, "candidate")
        self.assertEqual(by_name["servery-access-log-drop"].image, "candidate")
        self.assertEqual(by_name["servery-access-log-baseline"].image, "baseline")
        for name in (
            "servery-access-log",
            "servery-access-log-sync",
            "servery-access-log-drop",
            "servery-access-log-baseline",
            "servery-selector-access-drop",
            "servery-selector-access-wait",
        ):
            self.assertIn("--access-log", by_name[name].command)
        sync = by_name["servery-access-log-sync"].command
        production_drop = by_name["servery-access-log-drop"].command
        self.assertEqual(sync[sync.index("--access-log-queue") + 1], "0")
        self.assertEqual(
            production_drop[production_drop.index("--access-log-overflow") + 1],
            "drop",
        )
        drop = by_name["servery-selector-access-drop"].command
        wait = by_name["servery-selector-access-wait"].command
        self.assertEqual(drop[drop.index("--access-log-overflow") + 1], "drop")
        self.assertEqual(wait[wait.index("--access-log-overflow") + 1], "wait")
        self.assertIn("--access-log-queue", drop)
        self.assertIn("--access-log-queue", wait)
        self.assertEqual(drop[drop.index("--access-log-batch-size") + 1], "8")
        self.assertEqual(wait[wait.index("--access-log-batch-size") + 1], "8")
        self.assertEqual(drop[drop.index("--access-log-batch-wait-ms") + 1], "1")
        self.assertEqual(wait[wait.index("--access-log-batch-wait-ms") + 1], "1")

    def test_listing_expectation_and_adapters_make_worker_and_render_bounds_explicit(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            include_selector_spike=False,
            server=None,
        )
        scenario = next(item for item in scenarios() if item.name == "static-listing-100")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            listing_dir = corpus / "listing"
            listing_dir.mkdir(parents=True)
            config.mkdir()
            for index in range(3):
                (listing_dir / f"entry-{index}.txt").write_text("entry\n")
            resolved = resolve_scenario_expectation(scenario, corpus)
            specs = server_specs(
                resolved,
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertIsNone(resolved.expected_fixture)
        self.assertGreater(resolved.expected_length, 5000)
        self.assertNotEqual(resolved.expected_sha256, scenario.expected_sha256)
        production = next(spec for spec in specs if spec.name == "servery")
        baseline = next(spec for spec in specs if spec.name == "servery-baseline")
        one = next(spec for spec in specs if spec.name == "servery-selector-listing-w1")
        four = next(spec for spec in specs if spec.name == "servery-selector-listing-w4")
        self.assertEqual(production.image, "candidate")
        self.assertEqual(baseline.image, "baseline")
        self.assertEqual(one.command[one.command.index("--listing-workers") + 1], "1")
        self.assertEqual(four.command[four.command.index("--listing-workers") + 1], "4")
        for spec in (one, four):
            self.assertIn("--listing-queue", spec.command)
            self.assertIn("--max-listing-entries", spec.command)
            self.assertIn("--listing-page-size", spec.command)
            self.assertIn("--listing-details-threshold", spec.command)

    def test_prebuilt_baseline_image_is_a_rotated_static_adapter(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertEqual([spec.name for spec in specs[:2]], ["servery", "servery-baseline"])
        self.assertEqual(specs[0].image, "candidate")
        self.assertEqual(specs[1].image, "baseline")
        candidate_command = list(specs[0].command)
        worker_index = candidate_command.index("--workers")
        del candidate_command[worker_index : worker_index + 2]
        self.assertEqual(tuple(candidate_command), specs[1].command)
        self.assertNotIn("--workers", specs[1].command)

    def test_write_timeout_control_applies_only_to_candidate(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            servery_baseline_image="baseline",
            servery_write_timeout=30.0,
            servery_request_body_timeout=45.0,
            servery_request_head_timeout=60.0,
            servery_lifespan="off",
            servery_lifespan_timeout=2.5,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "asgi-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        candidate = next(spec for spec in specs if spec.name == "servery-asgi")
        baseline = next(spec for spec in specs if spec.name == "servery-asgi-baseline")
        self.assertIn("--write-timeout", candidate.command)
        self.assertNotIn("--write-timeout", baseline.command)
        self.assertIn("--request-body-timeout", candidate.command)
        self.assertNotIn("--request-body-timeout", baseline.command)
        self.assertIn("--request-head-timeout", candidate.command)
        self.assertNotIn("--request-head-timeout", baseline.command)
        self.assertIn("--lifespan", candidate.command)
        self.assertNotIn("--lifespan", baseline.command)
        self.assertIn("--lifespan-timeout", candidate.command)
        self.assertNotIn("--lifespan-timeout", baseline.command)

    def test_static_strategy_variants_are_isolated_benchmark_launchers(self) -> None:
        args = argparse.Namespace(
            compare_image="compare",
            nginx_image="nginx",
            caddy_image="caddy",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=[0, 1024, 16 * 1024],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                next(item for item in scenarios() if item.name == "static-1k"),
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertEqual(
            [spec.name for spec in specs[:3]],
            ["servery-sendfile", "servery-buffer-1k", "servery-buffer-16k"],
        )
        self.assertIn("--small-file-buffer-size", specs[1].command)

    def test_summary_tolerates_unavailable_cgroup_memory(self) -> None:
        rows = _summary(
            [
                {
                    "scenario": "static-1k",
                    "concurrency": 1,
                    "server": "servery",
                    "rps": 10.0,
                    "mb_s": 1.0,
                    "p99_ms": 2.0,
                    "client_cpu_utilization_pct": 25.0,
                    "errors": 0,
                    "container_memory": {"peak_mib": None},
                }
            ]
        )
        self.assertIsNone(rows[0]["median_peak_mib"])
        self.assertEqual(rows[0]["rps_mad"], 0.0)
        self.assertEqual(rows[0]["p99_mad_ms"], 0.0)
        self.assertFalse(rows[0]["client_limited"])
        self.assertIsNone(rows[0]["median_access_log_delivery_pct"])

    def test_summary_reports_access_log_delivery_median(self) -> None:
        samples = []
        for delivery in (99.0, 100.0, 50.0):
            samples.append(
                {
                    "scenario": "static-access-log-1k",
                    "concurrency": 64,
                    "server": "servery-selector-access-drop",
                    "rps": 10.0,
                    "mb_s": 1.0,
                    "p99_ms": 2.0,
                    "client_cpu_utilization_pct": 25.0,
                    "errors": 0,
                    "container_memory": {"peak_mib": 20.0},
                    "access_log_delivery_pct": delivery,
                }
            )

        self.assertEqual(_summary(samples)[0]["median_access_log_delivery_pct"], 99.0)

    def test_summary_reports_dispersion_and_client_saturation(self) -> None:
        samples = []
        for rps, p99, cpu in ((90.0, 4.0, 95.0), (100.0, 5.0, 92.0), (130.0, 9.0, 99.0)):
            samples.append(
                {
                    "scenario": "static-1k",
                    "concurrency": 64,
                    "server": "servery",
                    "rps": rps,
                    "mb_s": 1.0,
                    "p99_ms": p99,
                    "client_cpu_utilization_pct": cpu,
                    "errors": 0,
                    "container_memory": {"peak_mib": 20.0},
                }
            )

        row = _summary(samples)[0]

        self.assertEqual(row["median_rps"], 100.0)
        self.assertEqual(row["min_rps"], 90.0)
        self.assertEqual(row["max_rps"], 130.0)
        self.assertEqual(row["rps_mad"], 10.0)
        self.assertEqual(row["rps_mad_pct"], 10.0)
        self.assertEqual(row["median_p99_ms"], 5.0)
        self.assertEqual(row["min_p99_ms"], 4.0)
        self.assertEqual(row["max_p99_ms"], 9.0)
        self.assertEqual(row["p99_mad_ms"], 1.0)
        self.assertTrue(row["client_limited"])

    def test_paired_summary_uses_trial_ratios_not_ratio_of_medians(self) -> None:
        results = []
        for trial, candidate_rps, baseline_rps, candidate_p99, baseline_p99 in (
            (1, 110.0, 100.0, 9.0, 10.0),
            (2, 180.0, 200.0, 12.0, 10.0),
            (3, 105.0, 100.0, 10.0, 10.0),
        ):
            for server, rps, p99 in (
                ("servery", candidate_rps, candidate_p99),
                ("servery-baseline", baseline_rps, baseline_p99),
            ):
                results.append(
                    {
                        "scenario": "static-1k",
                        "concurrency": 64,
                        "trial": trial,
                        "server": server,
                        "rps": rps,
                        "p99_ms": p99,
                    }
                )

        results.extend(
            (
                {
                    "scenario": "static-gzip-cache-64k",
                    "concurrency": 64,
                    "trial": 1,
                    "server": server,
                    "rps": rps,
                    "p99_ms": p99,
                }
                for server, rps, p99 in (
                    ("servery-gzip-cache", 100.0, 10.0),
                    ("servery-gzip-cache-baseline", 100.0, 10.0),
                )
            )
        )

        rows = _paired_summary(results)
        row = next(item for item in rows if item["candidate"] == "servery")

        self.assertEqual(row["paired_trials"], 3)
        self.assertAlmostEqual(row["median_rps_change_pct"], 5.0)
        self.assertAlmostEqual(row["min_rps_change_pct"], -10.0)
        self.assertAlmostEqual(row["max_rps_change_pct"], 10.0)
        self.assertAlmostEqual(row["median_p99_change_pct"], 0.0)
        special = next(item for item in rows if item["candidate"] == "servery-gzip-cache")
        self.assertEqual(special["paired_trials"], 1)
        self.assertEqual(special["median_rps_change_pct"], 0.0)

        external = []
        for trial, servery_rps, uvicorn_rps in ((1, 120.0, 100.0), (2, 90.0, 100.0)):
            for server, rps in (("servery-asgi", servery_rps), ("uvicorn", uvicorn_rps)):
                external.append(
                    {
                        "scenario": "asgi-starlette-json",
                        "concurrency": 64,
                        "trial": trial,
                        "server": server,
                        "rps": rps,
                        "p99_ms": 1.0,
                    }
                )
        external_row = _paired_summary(external)[0]
        self.assertEqual(external_row["candidate"], "servery-asgi")
        self.assertEqual(external_row["baseline"], "uvicorn")
        self.assertAlmostEqual(external_row["median_rps_change_pct"], 5.0)

        native = [
            {**row, "server": "uvicorn-native" if row["server"] == "uvicorn" else row["server"]}
            for row in external
        ]
        native_row = _paired_summary(native)[0]
        self.assertEqual(native_row["candidate"], "servery-asgi")
        self.assertEqual(native_row["baseline"], "uvicorn-native")

    def test_prebuilt_baseline_supports_dynamic_servery_adapters(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            servery_baseline_image="baseline",
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            include_uvicorn_native=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            for family, expected in (
                ("wsgi", "servery-wsgi-baseline"),
                ("asgi", "servery-asgi-baseline"),
            ):
                scenario = next(item for item in scenarios() if item.family == family)
                specs = server_specs(
                    scenario,
                    port=8123,
                    concurrency=64,
                    corpus=corpus,
                    config_dir=config,
                    args=args,
                )
                baseline = next(spec for spec in specs if spec.name == expected)
                self.assertEqual(baseline.image, "baseline")
                self.assertEqual(
                    baseline.mounts,
                    ((APP_FIXTURE, "/opt/servery/benchmarks/comparison/apps.py", True),),
                )

    def test_framework_scenarios_mount_and_run_the_same_pinned_app(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            servery_baseline_image=None,
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
        )
        scenario = next(item for item in scenarios() if item.name == "asgi-fastapi-json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                scenario,
                port=8123,
                concurrency=64,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        self.assertEqual({spec.name for spec in specs}, {"servery-asgi", "uvicorn"})
        for spec in specs:
            self.assertIn(scenario.app_spec, spec.command)
            self.assertEqual(
                spec.mounts,
                (
                    (
                        FASTAPI_APP_FIXTURE,
                        "/opt/servery/benchmarks/comparison/fastapi_apps.py",
                        True,
                    ),
                ),
            )

    def test_framework_allowlist_retains_requested_servery_baseline(self) -> None:
        scenario = next(item for item in scenarios() if item.name == "asgi-starlette-json")
        self.assertTrue(_scenario_allows_server(scenario, "servery-asgi"))
        self.assertTrue(_scenario_allows_server(scenario, "servery-asgi-baseline"))
        self.assertTrue(_scenario_allows_server(scenario, "uvicorn"))
        self.assertFalse(_scenario_allows_server(scenario, "gunicorn-gthread"))

    def test_native_uvicorn_adapter_is_explicit_and_pins_native_protocols(self) -> None:
        args = argparse.Namespace(
            compare_image="candidate",
            servery_baseline_image=None,
            app_workers=1,
            servery_max_workers=None,
            servery_small_file_buffer=None,
            include_uvicorn_native=True,
        )
        scenario = next(item for item in scenarios() if item.name == "asgi-wait-10ms")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            config = root / "config"
            corpus.mkdir()
            config.mkdir()
            specs = server_specs(
                scenario,
                port=8123,
                concurrency=10_000,
                corpus=corpus,
                config_dir=config,
                args=args,
            )

        native = next(spec for spec in specs if spec.name == "uvicorn-native")
        portable = next(spec for spec in specs if spec.name == "uvicorn")
        self.assertIn("asyncio", portable.command)
        self.assertIn("h11", portable.command)
        self.assertIn("uvloop", native.command)
        self.assertIn("httptools", native.command)
        self.assertIn("20000", native.command)


if __name__ == "__main__":
    unittest.main()
