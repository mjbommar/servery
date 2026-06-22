# Contributing to servery

## The one rule that overrides everything

**Zero third-party *runtime* dependencies.** The shipped wheel must declare no
`Requires-Dist`. This is enforced in CI by `scripts/check_zero_deps.py` — if a
runtime dependency ever appears, the build fails. Dev/CI tooling (ruff, ty,
bandit, coverage, build, twine, pre-commit, httpx) is fine: it lives in the
`dev`/`test` [dependency groups](pyproject.toml) and never ships in the wheel.

Tests use the standard-library `unittest` only — no pytest, no hypothesis.

## Setup

```bash
uv sync                      # create the env with dev tooling
uv run pre-commit install    # local commit gates
```

## The gates

Run everything locally before pushing:

```bash
make check     # lint + type + security + test
make build     # build wheel/sdist + zero-dependency gate
```

Or individually:

| Gate | Command | What it enforces |
|------|---------|------------------|
| Lint & format | `make lint` / `make format` | `ruff check` + `ruff format` |
| Types | `make type` | `ty check` over `src` and `tests` |
| Security (SAST) | `make security` | `bandit` over `src` |
| Tests + coverage | `make test` | `unittest` discovery, ≥90% coverage |
| Packaging | `make build` | builds, asserts zero runtime deps, `twine check` |

CI (`.github/workflows/`) runs the same gates plus a test matrix
(Linux/macOS/Windows × Python 3.13/3.14, free-threaded 3.13t/3.14t, and
3.15/3.15t allowed to fail), a GitHub Actions audit (`zizmor`), and a secret
scan (`gitleaks`). servery targets free-threaded builds as a first-class
configuration — no module-level mutable state, no reliance on the GIL.

## External validation

Beyond the in-tree `unittest` suite, servery is cross-checked against the
standard external tools for each protocol surface. These are **not** required to
develop (they need third-party binaries) but are how we keep the hand-rolled
parts honest:

| Surface | Tool | How to run |
|---------|------|-----------|
| **TLS / HTTPS** | [`testssl.sh`](https://testssl.sh) (and `sslyze`) | `make scan-tls` / `scripts/scan_tls.sh [host:port]` — spins up `servery --tls-self-signed` and audits protocols, ciphers, the generated cert, and the usual CVE checklist. Expected: TLS 1.2/1.3 only, forward-secret AEAD ciphers, SAN trust OK, all vuln checks clean (the self-signed chain is "incomplete" by design). `tests/test_tls.py::TlsHardeningTest` encodes the key findings as a stdlib regression. |
| **HTTP/2** | [`h2spec`](https://github.com/summerwind/h2spec) + `curl --http2` | run `h2spec -h 127.0.0.1 -p PORT generic hpack` against `servery --http2` (generic 50/52, hpack 8/8). |
| **HTTP/1.1 + HTTP/2** | `httpx`, `curl` | exercised directly inside the test suite as independent clients. |
| **Performance** | `scripts/bench.py`, `scripts/microbench.py` | throughput/latency, benchmarked before/after changes. |

## Standards

servery aims to be a conformant HTTP/1.1 origin server (RFC 9110/9111/9112);
see [`docs/STANDARDS.md`](docs/STANDARDS.md). New behavior should cite the RFC
section it implements and ship with a test. Security-relevant changes should
keep the safe-by-default posture described in
[`docs/PRINCIPLES.md`](docs/PRINCIPLES.md).

## Commits

Small, focused commits. Reference the milestone from
[`docs/ROADMAP.md`](docs/ROADMAP.md) where relevant.
