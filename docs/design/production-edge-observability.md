# Production-edge observability vocabulary

Status: accepted vocabulary and baseline contract as of 2026-07-11;
implementation is tracked by `EDGE-040`.

This document fixes names, cardinality rules, lifecycle events, and reason codes
before the supervisor, resource policy, ACME scheduler, and metrics endpoint are
implemented. Code may emit only a subset initially, but it should not invent a
parallel vocabulary.

## Rules

- Metric labels are fixed enums or finite configuration slots. Request paths,
  filenames, query strings, domains, client addresses, request IDs, exception
  messages, and application-provided values are forbidden labels.
- Human access logs and structured operational events are separate products.
  Metrics aggregate; events diagnose; access logs describe individual requests.
- Disabled metrics must avoid per-request dictionaries, label construction, and
  histogram allocation. The disabled path must remain within the protected 2%
  RPS/p99 budget, with effects smaller than trial dispersion treated as neutral.
- Counters never decrease. Gauges represent current parent-aggregated state.
  Histograms use fixed buckets selected before runtime.
- A process supervisor aggregates worker snapshots. A worker crash may lose its
  final unflushed counter delta; that loss is exposed by the restart counter and
  documented rather than hidden behind a required external state service.

## Metric names

The eventual Prometheus text endpoint uses the `servery_` prefix. Labels shown
below are the complete allowed families; implementations may begin with fewer.

| Name | Type | Allowed labels | Meaning |
| --- | --- | --- | --- |
| `servery_requests_total` | counter | `transport`, `method_class`, `status_class` | Completed application/static requests |
| `servery_request_duration_seconds` | histogram | `transport`, `work_class` | Request admission through response completion |
| `servery_response_bytes_total` | counter | `transport`, `work_class` | Response payload bytes written |
| `servery_connections_active` | gauge | `transport` | Accepted live connections/sessions |
| `servery_streams_active` | gauge | `transport` | Active H2/H3/WebSocket logical streams |
| `servery_queue_depth` | gauge | `queue` | Current bounded waiting work |
| `servery_queue_capacity` | gauge | `queue` | Configured queue ceiling |
| `servery_rejections_total` | counter | `reason`, `transport` | Work refused before or during admission |
| `servery_timeouts_total` | counter | `phase`, `transport` | Head/body/write/keepalive/drain deadline expiry |
| `servery_drain_forced_total` | counter | `work_class`, `reason` | Work forcibly closed after grace |
| `servery_workers` | gauge | `state` | Workers in starting/ready/draining/failed states |
| `servery_worker_restarts_total` | counter | `reason` | Supervisor replacement attempts |
| `servery_reload_total` | counter | `result` | Validated generation replacement outcomes |
| `servery_certificate_expiry_seconds` | gauge | none | Minimum seconds to expiry across active identities |
| `servery_acme_attempts_total` | counter | `result`, `reason` | Certificate issue/renewal attempts |
| `servery_cache_operations_total` | counter | `cache`, `result` | Compression/digest/static-cache outcomes |
| `servery_log_events_dropped_total` | counter | `sink`, `reason` | Bounded logging handoff loss |

Allowed enum families are deliberately small:

- `transport`: `h1`, `h2`, `h3`, `websocket`, `tftp`;
- `method_class`: `safe`, `write`, `other`;
- `status_class`: `1xx`, `2xx`, `3xx`, `4xx`, `5xx`, `transport_error`;
- `work_class`: `static_small`, `static_stream`, `application`, `proxy`,
  `upload`, `listing`, `archive`, `websocket`;
- `state`: `starting`, `ready`, `draining`, `failed`;
- `result`: a context-specific finite set such as `success`, `failure`,
  `rejected`, `rollback`.

Histogram boundaries are static configuration owned by the implementation, not
user-supplied arbitrary labels. The initial request-duration buckets are
`0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30`
seconds.

## Operational events

Every structured event has `timestamp`, `event`, `reason`, `generation`, and
`worker` where applicable. Optional fields are selected from `transport`,
`work_class`, `count`, `duration_ms`, `deadline_ms`, and a sanitized error class.
Events never contain credentials, certificate material, query data, or raw
application exceptions.

Stable event names are:

- `runtime.state_changed`;
- `listener.started`, `listener.stopped`, `listener.failed`;
- `worker.started`, `worker.ready`, `worker.exited`, `worker.restarted`;
- `drain.started`, `drain.forced`, `drain.completed`;
- `reload.started`, `reload.accepted`, `reload.rolled_back`;
- `admission.rejected`;
- `certificate.renewal_started`, `certificate.renewal_completed`,
  `certificate.renewal_failed`, `certificate.activated`;
- `logging.dropped`.

## Stable reason codes

Reason codes are machine-readable lowercase identifiers. The initial registry is:

| Family | Codes |
| --- | --- |
| Admission | `connection_capacity`, `worker_queue_capacity`, `request_rate`, `client_rate`, `expensive_work_capacity`, `draining` |
| Deadline | `keepalive_timeout`, `request_head_timeout`, `request_body_timeout`, `write_timeout`, `drain_timeout` |
| Forced drain | `deadline_http1`, `deadline_h2`, `deadline_application`, `deadline_websocket`, `cancellation_suppressed` |
| Worker | `clean_exit`, `unexpected_exit`, `startup_failure`, `health_failure`, `recycle_age`, `recycle_requests`, `forced_termination` |
| Reload | `validated`, `invalid_config`, `application_startup_failure`, `readiness_timeout`, `drain_timeout` |
| Protocol abuse | `h2_stream_capacity`, `h2_rapid_reset`, `h2_control_rate`, `h3_stream_capacity`, `websocket_message_capacity`, `compression_expansion` |
| ACME | `issued`, `renewed`, `not_due`, `challenge_failure`, `ca_rejected`, `network_failure`, `invalid_material`, `activation_failure`, `certificate_expired` |

New reason codes require a documentation and test update. Attacker-controlled
text never becomes a reason code.

## Lifecycle meanings

- **Live:** the parent/control loop can answer the administration listener.
- **Ready:** every required listener is active, the configured worker quorum has
  completed application startup, and active certificate material is valid.
- **Draining:** readiness is false and new admission is stopped while previously
  accepted work receives its configured grace period.
- **Failed:** the required listener, worker quorum, application startup, or
  certificate safety condition cannot recover within policy.

These meanings are identical to the production-edge contract and are consumed
by readiness, supervisor, reload, and certificate work.

## Retained baseline

The pre-edge performance and resource baseline remains the exact-source evidence
in:

- [External server comparison benchmarks](server-comparison-benchmarks.md);
- [Performance and production-gap research roadmap](performance-gap-research-roadmap.md);
- [Native Uvicorn and ASGI concurrency scaling](performance-experiments/2026-07-11-asgi-native-scaling.md);
- [ASGI graceful drain](performance-experiments/2026-07-11-asgi-graceful-drain.md);
- [Threaded HTTP/1 and HTTP/2 graceful drain](performance-experiments/2026-07-11-threaded-http-drain.md).

Every future comparison must retain source revision, Python build, GIL mode,
kernel/OS, CPU allocation, server and client identities, workload parameters,
error/status counts, latency samples, memory, and raw artifacts. A timed-error or
client-saturated result remains unrankable.

## Acceptance

This vocabulary is accepted because it supplies bounded names and labels for
the supervisor, admission, drain, ACME, and operations tasks; defines stable
lifecycle and failure reasons; prohibits cardinality/security leaks; specifies
the disabled-cost gate; and points to retained exact-build baseline evidence.
Endpoint, aggregation, and request-context implementation remain `EDGE-040`.
