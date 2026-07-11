# Shared download disposition and SPA fallback — 2026-07-10

Status: accept shared query/disposition semantics, containment-safe production
SPA fallback, and opt-in SPA support in the benchmark-only selector. No shipped
default changes: SPA remains disabled unless the operator selects it.

## Boundary and configuration

This slice closes two regular-file semantic gaps without moving routing or wire
emission into the shared plan:

- `_static.download_requested` owns the existing `?download=<value>` query
  interpretation, and `_static.content_disposition` owns the injection-safe
  ASCII plus RFC 8187 filename value used by files and archives;
- production and selector SPA fallback both resolve the root `index.html`
  through the same contained-index helper, then open and describe that identity
  through the existing file plan.

Download query interpretation and safe field construction are protocol/product
semantics, not resource policy, so they are not configurable. SPA fallback is a
routing policy and remains explicit: shipped servery reuses `Config.spa` and
`--spa`; the prototype adds the same disabled-by-default `Policy.spa` and
benchmark flag. A selector backend must never turn SPA on merely because an
index exists.

The selector rejects a path that fails initial containment instead of treating
it as an SPA route. A contained missing path may fall back; an escaping path may
not.

## Security result

The audit found that production's previous SPA branch joined
`root_real/index.html` and checked only `isfile`. A symlinked root index could
therefore point outside the served root even though the original missing URL had
passed normal translation. The accepted implementation reuses
`find_contained_index`, so both the requested path and the fallback identity must
be contained. Regression tests create an escaping SPA index symlink and prove
production and selector return `404` without secret bytes.

Disposition tests cover CR/LF and quote removal from the ASCII fallback, UTF-8
percent encoding in `filename*`, regular files, and SPA-rewritten index names.
Conditional `304` and unsatisfiable `416` continue to omit disposition, matching
production's existing decision order.

## Fair benchmark design

Two opt-in capability-scoped scenarios were added:

- `static-download-1k` verifies the exact `Content-Disposition` before timing
  production, its paired baseline, and the selector;
- `static-spa-1k` launches distinct production and selector adapters with
  `--spa`, requests a missing client route, and validates the shared root index
  bytes.

The scenario records its permitted adapters. nginx/Caddy are not silently
included because their default file-server configurations do not implement
servery's query or SPA routing semantics. This keeps the workload comparison
semantic rather than merely byte-shaped.

## Performance results

CPython 3.15.0b3 with the GIL enabled, one server CPU, two isolated client
processes, warm cache, 64 keep-alive connections, seven rotated three-second
trials, and zero errors:

| Scenario/server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| download, production | 14.96k | 2.2% | 18.85 ms | 30.5 MiB |
| download, pre-refactor production | 16.17k | 4.1% | 18.52 ms | 30.5 MiB |
| download, selector | 14.58k | 1.8% | 5.22 ms | 27.2 MiB |
| SPA, production | 14.26k | 2.6% | 20.09 ms | 30.7 MiB |
| SPA, selector | 15.05k | 6.1% | 5.57 ms | 26.2 MiB |

Aggregate download medians are distorted by host/order variation; the paired
candidate result is the decision evidence: -2.3% RPS and +4.0% p99, with
4.1%/3.6% ratio MAD. The refactor is inside the protected 5% budget and is
accepted for shared semantics and the security fix.

Selector versus current production within-trial ratios show download at -2.1%
RPS (2.9% MAD) and -73.3% p99 (2.6% MAD). SPA is +3.9% RPS with 5.6% MAD and a
-7.0% to +17.0% range, so no throughput direction is claimed; p99 is 71.1%
lower with 2.0% MAD.

Because the shared query helper is called on every production file response, a
separate no-query protected gate was required:

| Protected workload | Paired RPS change | RPS ratio MAD | Paired p99 change | p99 ratio MAD |
| --- | ---: | ---: | ---: | ---: |
| 1 KiB keep-alive | -0.5% | 2.8% | +0.6% | 10.7% |
| 1 KiB churn | -0.8% | 2.1% | -1.2% | 1.0% |

The common path is neutral. Raw ignored artifacts are:

- `benchmarks/artifacts/shared-download-spa-selection-2026-07-10.json`;
- `benchmarks/artifacts/shared-download-spa-hotpath-paired-2026-07-10.json`.

## Decision and next gate

Keep both shared helpers and the contained SPA lookup. Keep SPA opt-in across
backends. Capability-scoped scenarios are the required harness pattern whenever
server configuration is necessary for semantic parity.

The next regular-file gap is compression negotiation and coded-body ownership.
That work must not perform compression on the event loop or invent a second
cache: it should reuse the existing representation decision/cache, define a
bounded worker path for misses, and retain the identity/range rule. Generated
directory listings remained the separate, larger metadata-budget project in
this slice and are addressed by the follow-on
[bounded listing experiment](2026-07-10-selector-listings.md).

## Verification

- 758 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds; each run has four expected optional-integration skips.
- Repository-wide Ruff format/lint, ty, Bandit, strict MkDocs, and
  `git diff --check` gates pass.
- The wheel and source distribution build, the wheel declares zero runtime
  dependencies, and its installed `servery --version` runs outside the source
  tree.
