# Opened-identity digests and bounded selector hashing — 2026-07-10

Status: accept opened-identity hashing and transient same-key digest sharing in
production HTTP/1. Accept representation digests as a benchmark-only selector
capability with separate worker, queue, and optional retained-entry budgets. Do
not add a public digest-cache setting or expose the selector backend from this
result alone.

## Problem and correctness finding

`Want-Repr-Digest` is opt-in but expensive: every miss reads and hashes the full
identity representation, even for a small range or HEAD response. The old
HTTP/1 path also had an identity race:

1. `_static.open_file` opened a file and derived size, validators, and eventual
   response bytes from that handle;
2. `_send_repr_digest(path)` reopened the pathname and hashed the second handle;
3. an atomic path replacement between those operations could therefore send the
   original bytes with a digest for the replacement.

The fix hashes the already-opened identity. `field_value_for_handle` seeks and
streams in bounded 256 KiB chunks, restores the caller's position, and accepts an
exact planned size. Truncation before the hash finishes now produces `500`
before file headers are committed. Growth hashes only the byte extent the body
plan will send. The ordinary response path still performs no digest work unless
the client asks for a supported algorithm.

## Transient single-flight and retention tradeoff

`DigestCache` keys results by canonical path, device, inode, mtime, ctime, size,
and algorithm. Concurrent callers for the same identity share one immutable
field value. With the production default of zero retained entries, the flight is
discarded after the last concurrent caller returns and a later request hashes
again. Different keys can hash concurrently under the caller's scheduling
budget, and an error reaches every waiter before the flight is reclaimed.

This is deliberately not a public retained-cache feature yet. A digest value is
small, so retained memory would be easy to bound by entry count, but correctness
still depends on filesystem metadata changing whenever bytes change. That is the
same broad assumption as the current ETag, with additional risk on coarse or
unusual filesystems. Sequential reuse also changes operational expectations for
mutable files. The internal cache supports a positive entry count for the
prototype and tests, but production instantiates it with zero. A future shipped
setting should be an operator policy such as retained entries or TTL—not an
implicit forever cache—and needs mutation-filesystem evidence first.

## Selector scheduling and ownership

Hashing is not event-loop work. The benchmark-only selector adds a digest planner
that is disabled when `digest_workers=0`. When enabled it provides:

- a dedicated fixed worker count and bounded queue, separate from filesystem
  lookup and compression budgets;
- cache lookup and same-key future sharing before capacity admission;
- immediate `503` for a distinct miss when worker plus queue capacity is full;
- an optional entry-count retained cache, default zero;
- bounded 256 KiB hashing rather than whole-file `read()` allocation;
- a duplicated descriptor for a large opened identity, owned by the worker
  through success, failure, request cancellation, and server drain;
- bounded-cardinality hit/submission/share/rejection/cancellation/error counters.

A small GET may hash the already-buffered immutable body. Large GET, HEAD, and
range requests use the duplicated descriptor. `Repr-Digest` covers the full
identity for `200` and `206`; it is omitted for `304`, `416`, unsupported
algorithms, and content-coded responses, matching production HTTP/1 semantics.

Workers and queue slots are separate configuration dimensions because they
govern CPU/IO concurrency and waiting memory respectively. Retained entries are
a third dimension. Inferring any of them from `max_connections` would hide the
resource tradeoff. These controls remain prototype CLI flags, not public servery
configuration.

## Correctness and failure gates

Direct tests cover:

- SHA-256/SHA-512 negotiation and exact RFC 9530 field values;
- file and opened-handle hashing with bounded memory and position restoration;
- full-file digest semantics for GET, HEAD, and byte ranges;
- omission for coded, `304`, `416`, and unsupported representations;
- retained-entry eviction and zero-retention same-key sharing;
- distinct-key concurrency and shared failure reclamation;
- selector cache hits, same-key sharing, bounded distinct-key saturation, and
  recovery;
- cancellation while a worker owns a duplicated descriptor;
- atomic replacement preserving one digest/body identity;
- in-place truncation failing before a stale successful response is emitted.

The comparison probe validates status, the complete 64 KiB body hash, and the
exact `Repr-Digest` header before every timed server sample.

## Fair benchmark cohort

The `static-digest-miss-64k` scenario uses CPython 3.15.0b3 with the GIL, one
server CPU, two isolated client processes, 64 keep-alive connections, seven
balanced-rotated three-second trials, and zero timed errors. Retained digest
entries are zero for all implementations. The selector has four digest workers
and 64 bounded queue slots; production uses its connection threads. Client CPU
was 15–16%, well below the harness saturation threshold.

The pre-change production image is the exact image from the preceding selector
compression checkpoint. The candidate artifact records the dirty research tree
and product/harness content hashes; it is decision evidence for this slice, not a
clean release-baseline artifact.

| Server | Median RPS | RPS MAD | Median p99 | Peak memory |
| --- | ---: | ---: | ---: | ---: |
| production with opened-identity single-flight | 9.23k | 2.1% | 27.00 ms | 42.0 MiB |
| pre-change production | 7.80k | 2.1% | 28.08 ms | 40.1 MiB |
| selector with bounded digest workers | 11.76k | 2.0% | 6.28 ms | 27.7 MiB |

Production versus its paired baseline is **+15.9% RPS** with 0.9% ratio MAD.
Median paired p99 is -0.3%, but its 6.7% ratio MAD is much wider than the point
estimate, so latency is treated as neutral. The roughly 2 MiB peak-memory
difference is small relative to trial/process variation and does not establish a
memory direction.

Against improved production within each trial, the selector is +29.2% RPS with
4.0% ratio MAD and -77.0% p99 with 2.1% ratio MAD. The remaining gap is connection
scheduling and owned worker dispatch, not missing digest semantics or an unsafe
event-loop hash shortcut.

Artifact: `benchmarks/artifacts/selector-digest-2026-07-10.json` (gitignored).

## Decision and remaining work

Accept the production identity fix and transient same-key sharing. They improve
correctness and throughput without retaining results or changing public
configuration. Keep the selector design experimental, with hashing disabled
unless its worker policy is explicit.

Do not add a production digest-cache flag from this one warm-filesystem,
same-identity cohort. Research sequential high-cardinality access, in-place
mutation on supported filesystems, large files, SHA-512, multiple server CPUs,
TLS, cancellation pressure, and cold/slow storage first. HTTP/2 and HTTP/3 still
lack representation-digest parity and need protocol-specific flow-control and
cancellation gates before the digest decision can move into a fully shared
response plan.
