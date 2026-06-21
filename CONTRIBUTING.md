# Contributing to servery

## The one rule that overrides everything

**Zero third-party *runtime* dependencies.** The shipped wheel must declare no
`Requires-Dist`. This is enforced in CI by `scripts/check_zero_deps.py` — if a
runtime dependency ever appears, the build fails. Dev/CI tooling (ruff, mypy,
bandit, coverage, build, twine, pre-commit) is fine: it lives in the `dev`
[dependency group](pyproject.toml) and never ships in the wheel.

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

## Standards

servery aims to be a conformant HTTP/1.1 origin server (RFC 9110/9111/9112);
see [`docs/STANDARDS.md`](docs/STANDARDS.md). New behavior should cite the RFC
section it implements and ship with a test. Security-relevant changes should
keep the safe-by-default posture described in
[`docs/PRINCIPLES.md`](docs/PRINCIPLES.md).

## Commits

Small, focused commits. Reference the milestone from
[`docs/ROADMAP.md`](docs/ROADMAP.md) where relevant.
