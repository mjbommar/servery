# Shared directory redirect and index selection — 2026-07-10

Status: accept the two shared directory primitives and the production adapter
refactor. Accept redirect/index support and explicit cache policy in the
benchmark-only selector. Directory listings remained deliberately unsupported
in this slice; the follow-on bounded scan/render policy is recorded in
[Shared listing policy and bounded selector rendering](2026-07-10-selector-listings.md).
Do not expose a public backend yet.

## Boundary and tradeoffs

Directory serving contains several distinct concerns: URL canonicalization,
archive/selection queries, contained index lookup, generated listing policy, and
transport emission. Sharing all of them at once would pull a large handler into
the static plan and risk changing production behavior. This slice shares only:

- slash-appending redirect construction, including query preservation and the
  existing `%2f`/`%2F` canonical form;
- ordered `index.html`/`index.htm` lookup with the same realpath containment
  check used by production.

The production order remains redirect, archive/selection query, contained
index, then listing. The selector has no archive or listing implementation: it
redirects, serves a contained index through the same opened-file plan, or returns
an honest bodyless `501`. Returning `404` would incorrectly claim the directory
does not exist; silently generating a reduced listing would create a new
security and resource-policy surface.

Directory lookup remains part of the existing filesystem scheduling choice. It
runs inline for the warm default and inside the same bounded executor in the
explicit slow-storage mode. No second queue is introduced.

## Response policy and configuration

The selector now applies the same logical regular-file cache policy shape:
`Cache-Control`, `Vary: Accept-Encoding` for compressible types, validators,
range advertisement, and `nosniff`. Cache policy is operator intent, so the
prototype `Policy` and benchmark CLI accept a validated Latin-1
`cache_control`; the shipped server continues to use its existing
`cache_max_age`/`cache_control` configuration. Redirect/index discovery itself is
protocol/filesystem correctness and is not configurable.

Compression remains unsupported in the selector. Advertising `Vary` preserves
cache correctness and parity but does not imply that the prototype negotiates a
coding.

## Correctness gates

Tests cover query-preserving redirect construction, already-canonical literal
and percent-encoded slashes, ordered contained index selection, GET/HEAD index
responses, cache/Vary headers, invalid cache-policy field injection, and the
explicit listing gap. Existing production tests continue to cover redirects,
indexes, and an index symlink escaping the root.

The external harness adds an opt-in `static-index-1k` scenario. It creates the
same `/indexed/index.html` corpus entry for every static server and validates the
status, exact 1 KiB body, and SHA-256 before timing.

## Performance result

CPython 3.15.0b3 with the GIL enabled, one server CPU, two isolated client
processes, 64 keep-alive connections, warm cache, seven rotated three-second
trials, and zero errors:

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production after refactor | 15.09k | 6.6% | 18.09 ms | 30.4 MiB |
| pre-refactor production | 14.84k | 0.5% | 17.74 ms | 30.3 MiB |
| selector prototype | 16.20k | 6.6% | 4.61 ms | 27.2 MiB |

The production paired result is +1.2% RPS and -2.1% p99, with ratio MAD of
7.8% and 9.8%; the refactor is performance-neutral. Selector/production
within-trial ratios have median +6.8% RPS but 10.7% MAD and a -5.6% to +34.7%
range, so there is no defensible throughput claim. Selector p99 is 72.6% lower
with 2.5% ratio MAD (range -76.3% to -68.7%), preserving the architecture's
repeatable tail-latency signal after directory and index work.

Raw evidence is the ignored artifact
`benchmarks/artifacts/shared-directory-index-selection-2026-07-10.json`.

## Decision and next gate

Keep the helpers narrow. Listing generation stays with production until a shared
logical listing plan can preserve pagination, scan/detail limits, hidden-file
policy, archive links, security headers, and bounded metadata work. A selector
listing should be attempted only with those policies explicit and benchmarked on
1k/10k/100k-entry directories; it must not perform unbounded synchronous scans
on the event loop.

The next lower-risk semantic slice is download disposition and SPA fallback, or
compression negotiation with an explicit worker/cache plan. Listings are the
larger resource-policy project and should not be smuggled into a connection-
architecture prototype.

## Verification

- 753 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds; each run has four expected optional-integration skips.
- Repository-wide Ruff format/lint, ty, Bandit, strict MkDocs, and
  `git diff --check` gates pass.
- The wheel and source distribution build, the wheel declares zero runtime
  dependencies, and its installed `servery --version` runs outside the source
  tree.
