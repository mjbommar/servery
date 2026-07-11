# Shared listing policy and bounded selector rendering — 2026-07-10

Status: accept shared directory-listing request policy and generated-page CSP in
production. Accept generated listings as a benchmark-only selector capability
with explicit worker, queue, scan, pagination, and detail budgets. Use one
listing worker as the conservative one-CPU research baseline; retain worker
count as policy and do not expose a public selector backend from these results.

## Problem and boundary

The selector previously returned an honest `501` for a directory without an
index. Calling `listing.render` directly on its event loop would close the
semantic gap but violate the architecture: a listing scans directory entries,
may stat thousands of them, sorts and filters, builds HTML, and may compress the
generated page. That work can block every unrelated selector connection.

Production already bounds one listing with `max_listing_entries`,
`listing_page_size`, and `listing_details_threshold`, but its thread-per-
connection architecture can still run many bounded listings concurrently. The
selector needs a second aggregate bound: how many scans/renders may run or wait
at once.

## Shared semantics

`listing.request_options` now owns transport-neutral interpretation of:

- decoded display path;
- Apache-compatible `C`/`O` sort controls;
- `q` and `ext` filters;
- positive page selection with malformed-input fallback;
- explicit `auto`/`light`/`dark` theme selection;
- persisted `servery_theme` cookie fallback and whether to set a new cookie.

Production HTTP/1 and the selector consume the same immutable request facts.
The generated-page CSP also moved to `_static.GENERATED_CSP`, removing the
buffered response planner's dependency on a private handler constant. Production
wire behavior is unchanged: generated listings retain exact content length,
optional gzip/zstd, `Vary: Accept-Encoding`, theme cookie, CSP, referrer policy,
and `nosniff` behavior. HEAD performs the same bounded planning but emits no
body.

## Selector scheduling and ownership

The benchmark-only `_ListingPlanner` is disabled when `listing_workers=0`; an
otherwise listable directory continues to receive `501`. When enabled it has:

- a dedicated fixed worker count and bounded queue, separate from warm file
  acquisition, file compression, and digest hashing;
- immediate `503` when worker plus queue capacity is full;
- the existing maximum-entry, page-size, detail-threshold, and hidden-file
  policies passed unchanged to the shared renderer;
- scan, render, and generated-body compression in the worker, never on the event
  loop;
- request cancellation that does not abandon a running worker;
- graceful shutdown that cancels queued jobs, waits for the running job, and
  reports both outcomes separately;
- bounded-cardinality submission, rejection, cancellation, late-completion,
  shutdown-cancellation, and error counters.

No listing cache or same-key single-flight was added. A directory has no opened
immutable identity comparable to a regular file handle. Sharing or retaining a
page would require a defensible directory-generation/invalidation model covering
entry creation, removal, rename, metadata, query, theme, and policy. The current
design prefers fresh bounded work over a fast stale page.

Worker count, queue slots, and per-page scan/render limits are distinct operator
policies. A worker controls active filesystem/Python work; a queue controls
waiting connection state; entry/detail/page bounds control one job. None should
be inferred from `max_connections`.

## Correctness and failure gates

Direct tests cover:

- shared query, sort, page, theme-param, theme-cookie, and invalid-input policy;
- default disabled behavior remaining explicit `501`;
- exact selector body parity with the production renderer;
- GET, bodyless HEAD, content length, gzip, `Vary`, CSP, referrer policy,
  `nosniff`, theme cookie, and hidden-file policy;
- bounded saturation, immediate `503`, and recovery;
- request cancellation while a worker finishes under planner ownership;
- shutdown cancellation of queued jobs while the running job is awaited;
- expected scan errors mapping to `404` and unexpected worker errors to `500`.

The external probe resolves expected bytes once from the fixed candidate corpus
under UTC, then validates exact length, SHA-256, CSP, referrer policy, and `Vary`
for every adapter before timing. This is a cross-adapter equality oracle, not an
independent HTML correctness oracle; the direct semantic tests provide the
independent checks.

## Fair benchmark cohorts

Both cohorts use CPython 3.15.0b3 with the GIL, one server CPU, two isolated
client processes, seven balanced-rotated trials, and zero timed errors. Files
have deterministic names, contents, and mtimes. Generated bodies are identity
encoded.

- `static-listing-100`: 100 entries, 56,762-byte page, 64 keep-alive clients,
  three-second trials;
- `static-listing-1000`: 1,000 entries, 519,786-byte page, 16 keep-alive clients,
  five-second trials.

Production candidate and pre-change production use the same public listing
limits. Selector variants use one or four listing workers, 64 queue slots,
100,000 maximum entries, a 1,000-row page, and the 10,000-entry detail threshold.
Client CPU remained 2–6%, well below the saturation threshold.

### 100 entries

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production | 1,473 | 1.2% | 162.86 ms | 77.4 MiB |
| pre-change production | 1,444 | 0.8% | 155.39 ms | 77.5 MiB |
| selector, one listing worker | 1,467 | 0.5% | 46.58 ms | 27.8 MiB |
| selector, four listing workers | 1,560 | 1.0% | 59.50 ms | 34.1 MiB |

Production's paired change is -0.5% RPS / +0.9% p99, both inside dispersion and
treated as neutral. Against current production within each trial, one selector
worker is -0.7% RPS / -71.1% p99 and uses 49.5 MiB less peak memory. Four workers
are +6.1% RPS / -63.9% p99 and use 43.2 MiB less.

### 1,000 entries

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production | 169 | 0.3% | 272.67 ms | 94.3 MiB |
| pre-change production | 168 | 0.6% | 270.89 ms | 94.1 MiB |
| selector, one listing worker | 180 | 0.6% | 95.77 ms | 32.6 MiB |
| selector, four listing workers | 174 | 0.2% | 119.54 ms | 46.4 MiB |

Production's paired change is +0.6% RPS / -0.3% p99, both neutral. Against
current production, one selector worker is +6.3% RPS / -65.0% p99 and uses 61.9
MiB less peak memory. Four workers are +2.9% RPS / -57.0% p99 and use 47.8 MiB
less.

Artifacts (gitignored):

- `benchmarks/artifacts/selector-listing-final-2026-07-10.json`;
- `benchmarks/artifacts/selector-listing-1000-final-2026-07-10.json`.

The corresponding files without `final` are diagnostic runs before queued-job
shutdown cancellation was added; they support the same decision but are not the
reported source shape.

## Decision and remaining work

Accept the production sharing refactor because it removes duplicated policy and
a handler import from the buffered planner with neutral measured throughput.
Keep bounded selector listing support and use one worker for the next one-CPU
prototype baseline: it has the best heavy-listing throughput, p99, and memory.
Four workers help the 100-entry throughput case but lose on every measured
1,000-entry resource metric. This is not a universal default—multi-CPU and slow
storage may justify more workers.

Do not ship selector listing controls yet. Before promotion, run 10,000/100,000-
entry overload and truncation tiers, mixed file/listing fairness, slow or remote
filesystems, multiple server CPUs, free-threaded Python, compressed listings,
client aborts during large generated-body writes, and macOS/Windows behavior.
Access logging, TLS/platform emission, explicit routing of write/dynamic features,
and styled error parity remain open.

## Verification

- 788 functional tests pass under CPython 3.15 normal-GIL and free-threaded
  builds. The normal environment has 40 optional-dependency skips; the populated
  free-threaded environment has four.
- Repository-wide Ruff format/lint, ty, Bandit, strict MkDocs, and
  `git diff --check` gates pass. Ty reports only two pre-existing unused
  suppression warnings.
- The final benchmark artifacts' product-tree hash matches the current source.
- Wheel and source distribution build, the wheel declares zero runtime
  dependencies, and its installed CLI/import smoke passes outside the source tree.
