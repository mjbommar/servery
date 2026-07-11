# Production-edge configuration design

Status: accepted design for `EDGE-003`; the loader and CLI integration are
separately tracked by `EDGE-041`.

This document defines the configuration contract for servery's first direct-edge
release. It is deliberately implementable with `dataclasses`, `tomllib`, `json`,
`os`, and `pathlib` from the standard library. It does not add a runtime parser
dependency, and it does not claim that the current CLI can load these files yet.

## Decisions and invariants

- Configuration has seven typed sections: runtime, listener, TLS/ACME,
  resources, logging, metrics, and application.
- The effective-value order is **built-in defaults < selected profile < TOML <
  environment < explicit CLI**.
- A configuration is an indivisible generation. All keys, types, values,
  cross-field constraints, referenced secret files, certificate material, and
  application specification syntax are validated before any socket is bound or
  application module is imported.
- Unknown or misspelled keys are errors. Unsupported schema versions are errors.
  There is no silent forward-compatibility mode.
- A scalar always replaces a lower-layer scalar. A collection replaces the
  lower-layer collection unless an explicitly named append operation is used.
- Secrets are references, not ordinary interpolated strings. Diagnostic output
  never includes their values.
- The frozen public `Config` remains the runtime object and library API. The new
  inputs compile to `Config.create()` arguments; feature tasks add validated
  fields to `Config` before exposing corresponding TOML or CLI keys.

The schema version is the integer top-level key `schema_version = 1`. A file
must contain it. `profile` is the only other top-level key; all settings live in
the tables below.

## Typed schema

Types use `path` for a filesystem path, `duration` for a finite number of
seconds (integer or float), `bytes` for a non-negative integer byte count, and
`secret-ref` for the tagged reference described later. Numeric TOML values do
not accept human-readable suffixes: `1048576`, not `"1 MiB"`.

| Section | Key | Type and default | Runtime mapping or status |
| --- | --- | --- | --- |
| top level | `schema_version` | required integer, currently `1` | input contract only |
| top level | `profile` | string, default `local` | profile selector; `production` is the direct-edge preset |
| `runtime` | `workers` | positive integer or `"auto"`; local `1`, production `"auto"` | `Config.workers`; distinct from `Config.max_workers` threads |
|  | `minimum_workers` | positive integer, default effective worker count | future supervisor field |
|  | `drain_timeout` | duration, default `30` | `Config.drain_timeout` |
|  | `force_timeout` | duration, default `1` | `Config.force_timeout` |
|  | `restart_backoff_initial` / `restart_backoff_max` | duration, target defaults `0.25` / `30` | `EDGE-013` target; not exposed until recovery is proven |
|  | `worker_restart_limit` / `worker_restart_window` | non-negative count / positive duration, target defaults `5` / `60` | `EDGE-013` target; zero limit disables crash recovery |
|  | `worker_max_age` / `worker_max_age_jitter` | non-negative duration / fraction `0..1`, target defaults `0` / `0.1` | `EDGE-013` target; zero age disables age recycling |
|  | `max_requests_per_worker` | non-negative integer, default `0` (unlimited) | future recycling field |
| `listener` | `host` | string, default `127.0.0.1`; production `0.0.0.0` | `Config.host` |
|  | `port` | integer `0..65535`, default `8000`; production `443` | `Config.port`; production forbids `0` |
|  | `http1` | boolean, default `true` | future explicit protocol field |
|  | `http2` | boolean, default `false` until the dynamic H2 adapter passes | `Config.http2` |
|  | `http3` | boolean, default `false` | `Config.http3`; optional dependency gate |
|  | `http3_port` | integer `1..65535` or absent | `Config.http3_port` |
|  | `trusted_proxy_cidrs` | array of CIDR strings, default `[]` | future trust-boundary field; empty means trust none |
| `tls` | `mode` | `"off"`, `"manual"`, `"acme"`, or `"self-signed"`; default `off` | selects exactly one TLS variant; production forbids `off` and `self-signed` |
| `tls.manual` | `certificate_file` / `private_key_file` | required paths | `Config.tls_cert` / `Config.tls_key` |
|  | `private_key_password` | optional secret-ref | `Config.tls_password` after protected resolution |
| `tls.acme` | `domains` | non-empty array of DNS names | `Config.acme` |
|  | `email` | email string or absent | `Config.acme_email` |
|  | `directory` | `"staging"` or `"production"`, default `staging` | `Config.acme_staging` |
|  | `state_directory` | path, production default `/var/lib/servery/acme` | future renewal/store field |
|  | `renew_before` | duration, production default `2592000` (30 days) | future renewal field |
| `resources` | `max_connections` | positive integer, default `256` | `Config.max_connections` |
|  | `blocking_workers` | positive integer or absent | `Config.max_workers`; bounds reusable blocking threads inside each process |
|  | `max_archive_streams` | positive integer or absent; must be below `blocking_workers` when both are set | `Config.max_archive_streams`; per-worker inline stream lease |
|  | `max_requests_per_connection` | non-negative integer, default `0` | `Config.max_requests_per_connection` |
|  | `max_h2_streams` | positive integer, default `100` | `Config.max_h2_streams` |
|  | `max_request_body` / `max_upload_size` | positive bytes, each default `104857600` | matching `Config` fields |
|  | `keepalive_drain_limit` | non-negative bytes, default `65536` | `Config.keepalive_drain_limit` |
|  | `request_head_timeout` / `request_body_timeout` / `write_timeout` | positive duration or absent | matching `Config` fields; absent disables that total/progress-specific budget while the general socket timeout remains |
|  | `keepalive_timeout` | positive duration or absent | `Config.keepalive_timeout` |
|  | `application_queue` / `background_tasks` | positive integers, production defaults `256` / `128` | future bounded-work fields |
| `logging` | `level` | `debug|info|warning|error`, default `info` | future structured-log field |
|  | `format` | `human|json`, default `human`; production `json` | future event-log field |
|  | `access` | boolean, default `true` | `false` maps to current quiet/no file behavior |
|  | `access_file` | path or absent | `Config.access_log` |
|  | `access_format` | `clf|combined|json`, default `clf`; production `json` | `Config.access_log_format` |
|  | `queue_capacity` | non-negative integer, default `0` (synchronous); `256` is the measured async candidate | `Config.access_log_queue` |
|  | `queue_bytes` | positive bytes, default `8388608` | `Config.access_log_queue_bytes` |
|  | `overload` | `block|drop`, default `block`; production candidate `drop` | `Config.access_log_overflow`; `stderr` remains unimplemented because it can also block |
|  | `batch_size` / `batch_wait` | positive integer / non-negative duration, defaults `8` / `0.001` | matching access-log `Config` fields |
|  | `drain_timeout` | non-negative duration, default `5` | `Config.access_log_drain_timeout` |
|  | `include_query` | boolean, default `false` | future redaction policy; never a metric label |
| `metrics` | `enabled` | boolean, default `false`; production `true` | future administration-listener field |
|  | `host` / `port` | string and integer `1..65535`, defaults `127.0.0.1` / `9090` | future administration listener |
|  | `path` / `live_path` / `ready_path` | absolute URL paths, defaults `/metrics`, `/livez`, `/readyz` | future administration paths |
| `application` | `mode` | exactly `static`, `wsgi`, or `asgi` | tagged union selector |
| `application.static` | `root` | path, default `.` | `Config.directory` |
|  | `show_hidden`, `spa`, `compress`, `security_headers` | booleans with current `Config` defaults | matching `Config` fields |
|  | `cache_max_age` | non-negative integer or absent | `Config.cache_max_age` |
|  | `max_buffered_response` / `small_file_buffer_size` | non-negative bytes | matching `Config` fields |
| `application.wsgi` | `target` | required `module:attribute` string | `Config.wsgi_app` |
| `application.asgi` | `target` | required `module:attribute` string | `Config.asgi_app` |
|  | `lifespan` | `auto|on|off`, default `auto` | `Config.lifespan` |
|  | `lifespan_timeout` | positive duration, default `5` | `Config.lifespan_timeout` |

Only the subtable selected by `application.mode` may be present. CGI, TFTP,
WebDAV writes, and general proxy routes remain available through the legacy
library/CLI surface but are deliberately absent from schema version 1's
production claim.

The `production` profile is conservative policy rather than a hidden mode: it
sets the production defaults listed above, requires TLS, enables bounded JSON
logging and loopback metrics, and selects supervised workers. It does not choose
an application, certificate source, domain, secret, or filesystem path.

## Overlay and collection semantics

The profile selector is resolved first from TOML, then environment, then
explicit CLI. Its values are still a defaults layer: choosing a profile on the
CLI does not cause profile values to override TOML. Resolution then proceeds:

1. construct built-in defaults;
2. apply the one selected profile;
3. replace values explicitly present in TOML;
4. replace values explicitly present in environment variables;
5. replace values explicitly present on the CLI.

An overlay parser must preserve absence. In particular, its `argparse` actions
must use suppressed defaults (or an equivalent sentinel), rather than injecting
ordinary CLI defaults that accidentally erase TOML and environment values.

Environment names are `SERVERY__` plus uppercase section and key components,
for example `SERVERY__RESOURCES__MAX_CONNECTIONS=1024`. Top-level profile is
`SERVERY__PROFILE`. Scalars use their TOML spelling (`true`, `false`, decimal
numbers, or unquoted strings). Arrays use a JSON array so commas and whitespace
are unambiguous, for example:

```console
SERVERY__TLS__ACME__DOMAINS='["example.com","www.example.com"]'
```

TOML arrays and environment arrays replace the lower-layer array. For a
repeatable CLI value, the first ordinary occurrence replaces the inherited
array and later occurrences append to that replacement. An explicitly named
append flag appends without replacement, and a clear flag replaces with an
empty array:

```console
# replace inherited domains with exactly these two
--acme example.com --acme www.example.com

# retain inherited domains and add one
--append-acme-domain status.example.com

# explicitly select no trusted proxies
--clear-trusted-proxy-cidrs
```

Mixing the ordinary replace form and its `--append-*` form in one invocation is
an error because order-dependent configuration is too easy to misread. Repeated
map keys, duplicate TOML keys, and duplicate logical items such as an ACME
domain after normalization are errors. A collection's provenance records its
replacement source and, for appended items, each append source.

## Secrets, redaction, and provenance

A `secret-ref` is a TOML inline table containing exactly one of these keys:

```toml
private_key_password = { file = "/run/secrets/tls-key-password" }
# or
private_key_password = { env = "TLS_KEY_PASSWORD" }
```

Secret files are opened without following a final symlink, read with a bounded
size, and required in the production profile to be owned by the effective user
and inaccessible to group/other. A future explicit compatibility switch may
relax the permission check, but cannot be enabled by a profile. Secret
environment references name another environment variable; `${...}` expansion,
shell evaluation, and recursive references are forbidden. Missing, empty,
oversized, malformed, or insecure secret sources fail validation.

Inline secrets in TOML are rejected. Existing `Config.create(tls_password=...)`,
`--auth USER:PASS`, and similar CLI values remain temporarily accepted for API
compatibility, with a startup warning that process arguments and tracebacks may
expose them. New production examples use file or environment references.

Every effective leaf retains a non-secret provenance record:

```text
resources.max_connections = 2048  [toml:/etc/servery.toml]
listener.host = "0.0.0.0"          [profile:production]
tls.manual.private_key_password = <redacted> [secret:file]
```

`--print-effective-config` and validation errors show the dotted key and source
kind. They never show a resolved secret, inline compatibility secret, secret
environment-variable name, or full secret-file path. Normal values may show the
TOML path, environment variable, or CLI flag that won. Logging the complete raw
input mapping is forbidden.

## Unknown keys and atomic validation

`tomllib` parses syntax; a separate stdlib-only schema compiler walks every
table and rejects unknown sections, unknown keys, wrong types (including
booleans where integers are expected), duplicate logical values, and unsupported
`schema_version` values. Environment variables beginning `SERVERY__` are held to
the same rule. Unrecognized CLI flags remain `argparse` errors.

Validation is staged but has no externally visible side effect:

1. parse TOML and collect only explicit environment and CLI overlays;
2. resolve the profile and merge while retaining provenance;
3. validate schema types, ranges, paths, enum values, and the application union;
4. validate cross-field rules and production-profile requirements;
5. resolve and validate secrets into short-lived protected values;
6. construct the frozen `Config` and build TLS context/certificate state;
7. only after steps 1–6 succeed, import the selected WSGI/ASGI target;
8. only after application import/startup succeeds may listener binding and
   generation readiness proceed under the supervisor lifecycle.

Steps 1–6 must not bind TCP/UDP/Unix sockets, import the application, create an
ACME account, write state, or start worker threads/processes. Failure reports all
independent schema errors in stable dotted-key order, but secret failures remain
redacted. Cross-field rules include:

- `runtime.minimum_workers` cannot exceed resolved workers;
- production requires `tls.mode = "manual"` or `"acme"` and a non-ephemeral
  listener port;
- only the selected `tls` and `application` variant tables may be present;
- HTTP/3 requires TLS and the optional dependency; `http3_port` requires HTTP/3;
- WSGI/ASGI plus HTTP/2 remains invalid until the H2 application adapter gate is
  closed, rather than advertising a protocol that bypasses the application;
- ACME requires normalized unique domains, writable protected state, and a
  reachable challenge-listener design; manual TLS requires a matching readable
  certificate/key pair;
- metrics may not share the public listener in schema version 1.

ACME certificate acquisition is a later lifecycle operation and cannot be fully
performed before binding its challenge listener. Atomic configuration validation
therefore proves ACME inputs and protected state first; readiness separately
requires usable certificate state according to the supervisor contract.

## Programmatic `Config` compatibility

`Config` remains frozen, public, and authoritative after input resolution.
`serve(Config.create(...))`, `make_server(Config.create(...))`, and direct reads
of existing fields remain supported. `Config.create()` continues to accept its
flat keyword names and performs all runtime invariants; the configuration
compiler calls that same method rather than constructing a parallel unchecked
object.

The migration sequence is:

1. add any new runtime policy as a typed, validated `Config` field with a safe
   default;
2. add the schema-to-`Config.create()` mapping and provenance metadata outside
   `Config`;
3. add the environment and explicit-CLI overlay aliases;
4. retain existing flags (`--bind`, `--wsgi`, `--asgi`, `--tls-cert`, `--acme`,
   and others) as aliases for the corresponding schema leaves;
5. deprecate only unsafe inline-secret forms, with a documented release window.

No caller is required to adopt TOML. A future nested programmatic builder may be
added for convenience, but it cannot replace the flat `Config.create()` API in
the first production-edge release.

## Examples and CLI equivalence

The checked-in examples are:

- [`production-edge/static.toml`](production-edge/static.toml)
- [`production-edge/wsgi.toml`](production-edge/wsgi.toml)
- [`production-edge/asgi.toml`](production-edge/asgi.toml)
- [`production-edge/acme.toml`](production-edge/acme.toml)
- [`production-edge/manual-tls.toml`](production-edge/manual-tls.toml)

The following commands are the target `EDGE-041` CLI equivalents. Each command
sets the same non-default leaves as its TOML file; omitted keys come from the
same production profile. Existing short/legacy spellings remain aliases.

```console
# static.toml
servery --profile production --application static --directory /srv/www \
  --bind 0.0.0.0 --port 8443 --http2 \
  --tls-cert /etc/servery/site.crt --tls-key /etc/servery/site.key \
  --max-connections 2048 --access-log /var/log/servery/access.json

# wsgi.toml (HTTP/1.1 until the H2 application adapter is accepted)
servery --profile production --application wsgi --wsgi example:wsgi_app \
  --bind 0.0.0.0 --port 8443 --no-http2 \
  --tls-cert /etc/servery/site.crt --tls-key /etc/servery/site.key \
  --workers 4 --max-connections 1024

# asgi.toml (HTTP/1.1 until the H2 application adapter is accepted)
servery --profile production --application asgi --asgi example:asgi_app \
  --lifespan on --lifespan-timeout 10 --bind 0.0.0.0 --port 8443 \
  --no-http2 --tls-cert /etc/servery/site.crt \
  --tls-key /etc/servery/site.key --workers 4 --max-connections 1024

# acme.toml
servery --profile production --application static --directory /srv/www \
  --bind 0.0.0.0 --port 443 --http2 --acme example.com \
  --acme www.example.com --acme-email ops@example.com --acme-production \
  --acme-state-directory /var/lib/servery/acme

# manual-tls.toml; the password value is read, never placed in argv
servery --profile production --application static --directory /srv/www \
  --bind 0.0.0.0 --port 443 --http2 \
  --tls-cert /etc/servery/site.crt --tls-key /etc/servery/site.key \
  --tls-password-file /run/secrets/tls-key-password
```

`--application`, `--workers`, `--no-http2`, and
`--acme-state-directory` are target spellings; `EDGE-041` owns their
implementation. The example ledger below makes equivalence review mechanical:

| TOML leaf | CLI spelling |
| --- | --- |
| `profile` | `--profile` |
| `application.mode` | `--application`; inferred by legacy `--wsgi`/`--asgi` |
| `application.static.root` | positional directory or `--directory` |
| `application.wsgi.target` / `application.asgi.target` | `--wsgi` / `--asgi` |
| `application.asgi.lifespan` / `lifespan_timeout` | `--lifespan` / `--lifespan-timeout` |
| `listener.host` / `port` | `--bind` / `--port` |
| `listener.http2` | `--http2` / `--no-http2` |
| `tls.manual.certificate_file` / `private_key_file` | `--tls-cert` / `--tls-key` |
| `tls.manual.private_key_password.file` | `--tls-password-file` |
| `tls.acme.domains` / `email` / `directory` | repeated `--acme` / `--acme-email` / `--acme-production` |
| `tls.acme.state_directory` | `--acme-state-directory` |
| `runtime.workers` | `--workers` |
| `resources.max_connections` | `--max-connections` |
| `resources.blocking_workers` | `--max-workers` (threads inside each worker process) |
| `resources.max_archive_streams` | `--max-archive-streams` |
| `logging.access_file` | `--access-log` |
| `logging.queue_capacity` / `queue_bytes` | `--access-log-queue` / `--access-log-queue-bytes` |
| `logging.overload` | `--access-log-overflow` |
| `logging.batch_size` / `batch_wait` / `drain_timeout` | matching `--access-log-*` flags |

The examples intentionally contain no metrics overrides because the production
profile supplies the same enabled loopback administration listener to both TOML
and CLI forms.

## EDGE-003 evidence and boundary

The five example files parse with Python's standard-library `tomllib`; each
required variant and its CLI mapping is enumerated above. Their semantic types
and cross-field combinations were reviewed against this schema. No application
code, loader, or third-party parser is involved in this design task.

Automated semantic parsing, provenance output, permission enforcement,
`--check-config`, stable exit codes, and installed-wheel CLI tests are not
evidence for this design task because they do not exist yet. They remain the
explicit acceptance gates for `EDGE-041`.
