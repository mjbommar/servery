# Production-edge threat model

Status: accepted hostile-input inventory for `EDGE-002` as of 2026-07-11.
Implementation and release assurance remain assigned to the task IDs below.

This model covers servery acting as the public TLS endpoint for one static site
or local Python application without nginx, Caddy, or an external process
manager. It describes current controls honestly; a listed control is not proof
that a surface is ready for hostile production traffic. The production claim is
earned only when the follow-up task and release gates pass against the packaged
artifact.

## Assets and trust boundaries

The assets are availability, response and file integrity, confidentiality of the
served root and credentials, TLS private keys and ACME account state, correct
client identity, application isolation from malformed wire input, and bounded
CPU, memory, descriptors, processes, threads, queues, and disk.

Data crosses these boundaries:

1. **Public network to protocol parser.** Every TCP byte, UDP datagram, TLS
   handshake, HTTP field, URL, WebSocket frame, and HTTP/2 or HTTP/3 control
   frame is attacker-controlled.
2. **Protocol layer to application or filesystem.** Normalized request data
   becomes a path, WSGI/ASGI event, CGI environment, proxy request, upload,
   WebDAV operation, archive selection, or listing filter.
3. **Server to upstream authority.** ACME directories and reverse-proxy targets
   are separate network principals. Their JSON, headers, certificates, status,
   timing, and failures are untrusted input.
4. **Configuration and local state to privileged runtime.** Configuration,
   application import strings, certificate/key files, password files, the
   served tree, and ACME cache are trusted only to the degree that their owner,
   permissions, provenance, and atomicity are verified.
5. **Worker to parent and old generation to new generation.** Exit status,
   readiness, metrics, inherited descriptors, and replacement state can be
   stale, partial, or malicious if operator-loaded application code is
   compromised.

The socket peer is the client identity. Forwarding headers have no authority by
default. WSGI, ASGI, and CGI applications are operator-trusted code, but their
exceptions, cancellation behavior, response fields, and resource consumption
are not trusted to preserve server availability.

## Non-configurable invariants

- Ambiguous HTTP framing, invalid field syntax, invalid protocol state, path
  escape, partial committed writes, unvalidated certificate activation, and
  forwarding-header trust fail closed. No production option weakens them.
- Attacker-controlled sizes, counts, rates, recursion, expansion, queues, and
  retention have finite production limits. A larger operator-selected limit is
  policy; an unbounded production default is not.
- Filesystem mutation uses containment checks, same-target coordination,
  private temporary state, and atomic replacement. A path checked before an
  attacker-controlled blocking interval is revalidated against the opened or
  destination identity where the platform permits it.
- A malformed connection is closed or reset at the narrowest safe boundary.
  Healthy clients continue when a stream can be isolated.
- Secrets, raw URLs/query strings, filenames, addresses, application messages,
  and exception text never become metric labels or stable reason codes.
- Optional HTTP/3 does not weaken the core release gate. TFTP, CGI, anonymous
  writes, self-signed TLS, and public administration are rejected by the first
  production profile unless explicitly removed from that profile's claim.

## Public parser and expensive-operation inventory

“Signal” names either a stable code from the
[observability vocabulary](production-edge-observability.md) or the bounded
status/close evidence available today. Codes not implemented yet are owned by
`EDGE-040`; attack-shaped verification is owned by `EDGE-062` or the named
release gate.

| Surface and boundary | Threat to assets | Current disposition/control | Missing control or decision | Signal | Verification owner |
| --- | --- | --- | --- | --- | --- |
| HTTP/1 request line, fields, Host, framing, chunked body | request smuggling, oversized heads, slowloris, parser disagreement, connection pinning | shared strict field/Host/framing policy; 64 KiB per-line and 100-field ceilings; conflicting or invalid length rejected; socket, keep-alive, total-head, total-body, write, and request-count policies exist | unify finite production defaults; add combined header-byte admission and equivalent backend behavior (`EDGE-030`) | `400`/`414` or close today; `request_head_timeout`, `request_body_timeout`, `connection_capacity` | `EDGE-030` boundary tests; `EDGE-062` HTTP/1 corpus; `EDGE-061` wire interop |
| HTTP/2 frame codec, HPACK, stream state and flow control | HPACK expansion/table abuse, continuation floods, rapid resets, control-frame CPU amplification, stream starvation, buffered-body exhaustion | maximum frame, encoded block, decoded header-list and dynamic-table sizes; continuation, reset and acknowledgement-triggering control budgets; finite advertised/enforced streams; fair response-data pass; protocol errors close/reset | make abuse budgets cohesive/configurable; cover every control-frame class, per-stream/body buffers, global/client rates, and cross-client fairness (`EDGE-031`) | `h2_stream_capacity`, `h2_rapid_reset`, `h2_control_rate`, transport reset | `EDGE-031` attack replays; `EDGE-062` HPACK/H2 corpus; `EDGE-061` h2spec/nghttp2 |
| TLS ClientHello, certificate/key loading and ALPN | handshake CPU/descriptor flood, downgrade or advertising unsupported protocols, malformed/mismatched/expired material, key disclosure | Python `ssl` performs wire parsing; ALPN advertises only configured live protocols; password-file support; invalid material normally fails startup | production crypto/store decision, private atomic state, cert/key/SAN/expiry validation (`EDGE-050`); admission budgets (`EDGE-030`); live activation (`EDGE-052`) | handshake transport error today; `invalid_material`, `activation_failure`, `certificate_expired` | `EDGE-050` malformed-state tests; `EDGE-052` sustained handshakes; `EDGE-061` pinned TLS scan |
| Optional QUIC/HTTP/3 parser (`aioquic`) | UDP flood, QUIC amplification, stream/control exhaustion, dependency vulnerabilities | isolated optional dependency; live UDP endpoint alone emits `Alt-Svc`; separate stream limit and bounded large-file streaming | pin and report dependency closure (`EDGE-060`); H3 stream/control/amplification policy (`EDGE-031`); explicit restart on TLS change (`EDGE-052`) | `h3_stream_capacity`, transport error | `EDGE-031`; isolated H3 cohort in `EDGE-061`; package audit in `EDGE-060` |
| ACME directory, nonce, JSON/JWS, challenge and returned PEM | malicious/compromised CA response, replay/state confusion, issuance storms, corrupt cache, challenge hijack, renewal outage | HTTPS CA connection, ACME v2 flow, staging default, cached reuse, HTTP-01 token validation | audited crypto/store choice (`EDGE-050`); expiry-driven singleton scheduler, bounded retry/jitter and challenge service (`EDGE-051`); atomic activation (`EDGE-052`) | `challenge_failure`, `ca_rejected`, `network_failure`, `invalid_material`, `certificate_expired` | `EDGE-050`; fake-clock/mock-CA cases in `EDGE-051`; failure soak in `EDGE-063` |
| WebSocket upgrade and frame parser | payload allocation, fragmented-message accumulation, ping/message flood, invalid UTF-8/opcodes, slow peer, application fanout amplification | handshake validation; 16 MiB frame and reassembled-message cap; client masking and control-frame rules; UTF-8 check; transport write deadline and graceful close support | configurable production message/fragment-count limits, queue and rate budgets, ping/idle policy, fanout fairness (`EDGE-031`); cross-backend parity (`EDGE-022`) | `websocket_message_capacity`, `write_timeout`, `deadline_websocket` | `EDGE-031` attack replay; `EDGE-062` frame corpus; Autobahn/slow-consumer `EDGE-061` |
| Multipart headers, boundary scanner, filenames and upload stream | malformed boundary/header ambiguity, path injection, memory/disk exhaustion, many partial uploads, overwrite race | custom streaming parser; bounded total upload; sanitized basename; private temp plus atomic replace; overwrite denied by default; same-target lock; bounded partial count and lazy TTL | bound part count, per-part headers and boundary-search work; global disk/expensive-work budgets; multi-worker coordination (`EDGE-030`, `EDGE-021`, `EDGE-013`) | `413`/`400`/`409` today; `expensive_work_capacity`, `request_body_timeout` | `EDGE-030`; multipart corpus in `EDGE-062`; disk/failure soak in `EDGE-063` |
| Resumable `Content-Range` upload state | sparse/overlapping chunks, sidecar exhaustion, stale-state collision, cross-worker race | ordered contiguous chunks; maximum final size; same-target in-process lock; partial-count ceiling; TTL; atomic final commit | parent/singleton or filesystem-safe cross-worker ownership (`EDGE-013`); global disk quotas (`EDGE-030`); crash consistency (`EDGE-063`) | `409`/`413`/`507` today; `expensive_work_capacity` | `EDGE-013` concurrent workers; `EDGE-062` range/state corpus; `EDGE-063` crash/disk-full injection |
| WebDAV XML, `Depth`, `Destination`, lock tokens and write methods | XML CPU/memory abuse, traversal, huge enumeration, destructive unauthenticated writes, lock-table exhaustion, cross-worker inconsistency | write mode opt-in; shared containment/atomic writes; `Destination` containment; Depth-1 entry cap; in-memory enforced locks; read-only class 1 available | bound XML bytes/nesting/property count and lock table; coordinate or reject multi-worker writable DAV; production profile forbids unsafe combinations (`EDGE-030`, `EDGE-013`, `EDGE-070`) | `400`/`403`/`423`/`507`; `expensive_work_capacity` | WebDAV corpus `EDGE-062`; cross-worker `EDGE-013`; production config `EDGE-070` |
| Uploaded ZIP/TAR inspection and extraction | zip/tar bombs, path traversal, symlink/device escape, huge member count, decompression CPU/disk exhaustion, partial tree | opt-in; validates members without `extractall`; rejects unsafe kinds/paths and excessive expansion; same-target coordination | one cohesive CPU/disk/member/ratio budget, bounded offload, crash-atomic directory semantics (`EDGE-021`, `EDGE-030`, `EDGE-063`) | rejection today; `compression_expansion`, `expensive_work_capacity` | archive corpus `EDGE-062`; mixed-work gate `EDGE-021`; disk/crash soak `EDGE-063` |
| Download archive selection and TAR.GZ/ZIP generation | directory walk amplification, symlink/TOCTOU disclosure, compressor CPU, slow-reader worker pinning | selection is contained; symlinks skipped; output streams with valid framing; write-progress timeout available | snapshot/opened-identity policy, file/member/byte and concurrent archive budgets, bounded blocking pool (`EDGE-021`, `EDGE-030`) | `expensive_work_capacity`, `write_timeout`, work class `archive` | `EDGE-021` mixed load; archive corpus/invariants `EDGE-062`; scaling `EDGE-064` |
| Static compression and encoded cache | compression CPU/memory amplification, cache-key confusion/poisoning, stale representation, eviction races | MIME/size eligibility; `max_compress_size`; byte-bounded optional cache; key includes canonical path, size, mtime, coding, level; `Vary` and coding-specific ETag | global expensive-work concurrency and single-flight bounds across workers; adversarial cache/eviction tests (`EDGE-021`, `EDGE-030`) | `compression_expansion`, `expensive_work_capacity`, cache result | `EDGE-021`; cache corpus/concurrency `EDGE-062`; performance `EDGE-064` |
| Integrity digest | attacker forces full-file reads for range/HEAD-like work, CPU/I/O amplification, stale digest after replacement | opt-in negotiation; digest uses opened file identity; bounded cache; hashing can be offloaded in selector prototype | shared bounded digest queue/concurrency, cancellation and cross-worker cache policy (`EDGE-021`, `EDGE-030`) | `expensive_work_capacity`, cache result | `EDGE-021` mixed work; identity/race corpus `EDGE-062`; `EDGE-064` |
| Directory listing/query parser and WebDAV enumeration | huge directories, pathological sort/filter/query inputs, HTML injection, metadata race | escaped rendering; query parsing; finite scanned/matched/rendered limits and pagination; `PROPFIND` cap | move blocking scan/render behind bounded scheduler; global expensive-work budget (`EDGE-021`, `EDGE-030`) | `507` or request rejection; `expensive_work_capacity` | `EDGE-021`; listing/path corpus `EDGE-062`; `EDGE-064` |
| URL, percent decoding, query, Range/conditional/auth fields and filesystem path translation | traversal, double decoding, NUL/control injection, symlink race, open redirect, cache-validator confusion, auth timing leak | common URL split; separator-aware realpath containment; symlink escape denied by default; strict single-range and conditional selection; escaped output; constant-time credential comparison | opened-identity operations consistently across every backend; trusted identity only under explicit CIDRs (`EDGE-022`, `EDGE-032`); TOCTOU failure injection (`EDGE-063`) | `400`/`403`/`404`/`416`; access status without raw path label | path corpus `EDGE-062`; identity tests `EDGE-032`; race soak `EDGE-063` |
| Reverse-proxy request/response parser and configured upstream URL | SSRF by configuration, hop-by-hop/framing confusion, credential leakage, upstream stall, cache poisoning, unsafe retry/replay, response amplification | routes are operator configuration; only HTTP(S) upstream; prefix dispatch; client authorization stripped; hop-by-hop fields filtered; ordinary request/body/write limits | strict upstream response framing/size/deadlines; bounded proxy concurrency; DNS/rebinding decision; no retries/cache in direct-edge v1 (`EDGE-030`, `EDGE-022`) | work class `proxy`; `request_body_timeout`, `write_timeout`, `expensive_work_capacity` | proxy differential/corpus `EDGE-062`; integration `EDGE-022`; failure soak `EDGE-063` |
| TOML/CLI/environment, app import string, secrets and effective config | malicious or accidental unsafe combination, secret disclosure, partial reload, import side effects before validation, parser resource abuse | immutable typed `Config`; many invalid combinations fail; core CLI parsing is stdlib | typed schema, unknown-key rejection, precedence, redaction, secret references and validation before bind/import (`EDGE-003`, `EDGE-041`); atomic generation reload (`EDGE-014`) | `invalid_config`, startup failure; redacted event only | schema tests `EDGE-003`/`EDGE-041`; config corpus `EDGE-062`; reload injection `EDGE-063` |
| WSGI/ASGI event and response-field adapters | application blocks, allocates, suppresses cancellation, emits invalid framing/fields, leaks exception/secret, or exhausts background work | strict ASGI response state/framing; body and write deadlines; lifespan startup gate; active-work registry and drain deadline | bounded application admission/execution and selector parity (`EDGE-021`, `EDGE-022`, `EDGE-030`); broader framework/slow-consumer gates (`EDGE-061`) | `worker_queue_capacity`, `write_timeout`, `cancellation_suppressed`, `deadline_application` | `EDGE-021`/`EDGE-022`; framework cohort `EDGE-061`; soak `EDGE-063` |
| CGI process and response parser | arbitrary trusted-code execution, process fork flood, environment/header injection, child hang/output amplification | explicit opt-in directory and contained script resolution; not a production-profile application mode | production profile rejects CGI (`EDGE-070`); if later claimed, it requires process/count/output/time budgets under `EDGE-030` and a dedicated corpus | startup rejection in production; otherwise `worker_queue_capacity`/timeout | exclusion test `EDGE-070`; future parser work must enter `EDGE-062` |
| TFTP request/options/path parser and UDP transfer state | spoofing/reflection, amplification, traversal, transfer flood, anonymous overwrite, retransmit storm | explicitly LAN-only; off by default; writes separately opt-in; contained paths; finite transfer ceiling and retransmission timeout | production profile rejects TFTP (`EDGE-070`); it is not part of the public-edge claim | startup rejection in production; `connection_capacity` if used outside it | exclusion test `EDGE-070`; existing TFTP regression suite |

## Cross-cutting attack scenarios

| Scenario | Required disposition | Observable evidence | Owning tasks |
| --- | --- | --- | --- |
| Slow head, body, idle peer, or response reader | Distinct finite production deadlines release the relevant connection, stream, task, and queue slot without extending total head/body time on trickle progress. | `request_head_timeout`, `request_body_timeout`, `keepalive_timeout`, or `write_timeout`; active gauges return to baseline | `EDGE-030`, `EDGE-031`, `EDGE-063` |
| Connection, request, stream, or client flood | Layered global and per-client admission uses bounded state; overload returns `429` or `503` when safe and never queues without a ceiling. | `connection_capacity`, `request_rate`, `client_rate`, `worker_queue_capacity`, `h2_stream_capacity` | `EDGE-030`, `EDGE-031`, `EDGE-040`, `EDGE-064` |
| Rapid reset/control-frame flood | Charge control work before doing it, bound per-connection and global rates, isolate the abusive stream when safe, and prove unrelated clients progress. | `h2_rapid_reset`, `h2_control_rate`; CPU and healthy-client latency remain bounded | `EDGE-031`, `EDGE-062`, `EDGE-064` |
| Compression, digest, listing, archive, proxy, or application amplification | Admit from separate bounded expensive-work capacity; cancel abandoned work; preserve a cheap-request lane; cap retained output/cache bytes. | `expensive_work_capacity`, `compression_expansion`, queue depth/capacity and cache result | `EDGE-021`, `EDGE-030`, `EDGE-040`, `EDGE-064` |
| Upload/partial/archive disk exhaustion | Reject declared excess early, meter actual expansion and temp/partial state, survive disk-full without a visible partial destination, and recover quota after cleanup. | `413`/`507`, `expensive_work_capacity`; no partial committed file | `EDGE-030`, `EDGE-062`, `EDGE-063` |
| Symlink, rename, overwrite, and cross-worker race | Contain the opened/final identity, serialize the decision and commit across supported workers, and use atomic replace; fail closed where platform guarantees are insufficient. | explicit conflict/forbidden status; no outside-root or partial artifact | `EDGE-013`, `EDGE-022`, `EDGE-062`, `EDGE-063` |
| Cache poisoning or stale representation | Key every negotiated representation by all selectors and opened identity; bound cache; invalidate atomically; never share proxy and compression caches accidentally. | cache hit/miss/reject plus exact response hash/ETag/Vary | `EDGE-021`, `EDGE-030`, `EDGE-062` |
| Worker crash loop or poisoned generation | Parent gates readiness, uses bounded exponential restart backoff and a crash budget, keeps the last ready generation, and exposes terminal failure rather than busy-looping. | `unexpected_exit`, `startup_failure`, restart count, worker states | `EDGE-012`, `EDGE-013`, `EDGE-014`, `EDGE-063` |
| Failed, corrupt, or late certificate renewal | Keep a still-valid identity active, retry with jitter/backoff, reject invalid material, alert before expiry, and make readiness false at the defined terminal safety point. | ACME reason codes and minimum expiry gauge | `EDGE-050`, `EDGE-051`, `EDGE-052`, `EDGE-063` |
| Log/metric injection or cardinality exhaustion | Fixed labels/reasons only, structured escaping and redaction, bounded handoff with observable drop/block/stderr policy. | `servery_log_events_dropped_total`; no attacker text in labels | `EDGE-040`, `EDGE-062`, `EDGE-063` |
| Reload/shutdown race | Validate before bind/import, atomically select a generation, stop admission, drain registered work, and force only at the configured deadline. | reload result, lifecycle state, forced-drain reason | `EDGE-010`, `EDGE-014`, `EDGE-063` |

## Verification ownership and release effect

`EDGE-062` owns a bounded harness, seed corpus, mutation job, reproducer, and
regression case for every parser row above that is in the public-edge claim.
`EDGE-063` owns blocking, crash, disk, clock, descriptor, and race injection.
`EDGE-061` owns independent protocol and framework peers. `EDGE-064` owns proof
that admission and expensive-work controls preserve progress under controlled
load. `EDGE-065` makes unresolved crashes, hangs, unexplained skips, corpus
regressions, and expired security waivers release-blocking.

A surface cannot disappear from the matrix merely because it is hard to fuzz.
It must instead be rejected by production configuration and tested as rejected,
as with CGI and TFTP in `EDGE-070`. General proxy pools and shared proxy caching
remain non-goals; adding either requires a new threat-model row before code.

## Supported versions and vulnerability response

The public reporting and support policy is in the repository
[`SECURITY.md`](https://github.com/mjbommar/servery/blob/main/SECURITY.md). In
summary:

- Only versions explicitly listed there are security-supported. The current
  direct-edge work is pre-release and carries no production-readiness claim.
- A production-edge release must test every supported CPython minor and platform
  in package metadata; a newly released interpreter is not production-supported
  merely because `Requires-Python` permits installation.
- Vulnerabilities are reported privately, acknowledged and triaged to published
  targets, fixed on supported lines according to severity, and disclosed with
  affected versions, mitigations, and artifact provenance.
- CPython, OpenSSL, the operating system, and optional `aioquic` remain separate
  upstream security boundaries. Operators must run supported patched versions;
  servery does not silently vendor their fixes.

## Acceptance disposition

Every public parser and expensive operation has an asset/trust boundary, current
control, named remaining task, bounded signal, and verification owner. Every
cross-cutting threat named by `EDGE-002` has an explicit disposition. Unknown
implementation sufficiency is not recorded as “safe”; it is assigned to
`EDGE-021`, `EDGE-030`, `EDGE-031`, `EDGE-040`, `EDGE-050..052`, or
`EDGE-060..065`, while intentionally unsupported production modes are assigned
to `EDGE-070`.
