# Dynamic handlers

servery is a **file server**. This document plans an *optional, opt-in* capability
to also run dynamic handlers — CGI scripts, WSGI apps, ASGI apps — **using only
the standard library**, on Python 3.14+.

> **Status (shipped, [Unreleased]):** all three phases are implemented and tested.
> **D1 `--wsgi`** (`servery/wsgi.py`, lean HTTP/1.1 engine, `wsgiref.validate`-gated,
> ~20k req/s). **D2 `--cgi`** (`servery/cgi.py`, RFC 3875 + the full security suite).
> **D3 `--asgi`** (`servery/asgi.py`, asyncio "mini-uvicorn", lifespan, ~19k req/s,
> verified against Starlette). Each is off by default; HTTP/1.1 only. The design
> notes below are kept as the record and for future work (ASGI TLS/WebSocket).

## 0. The boundary (read first)

servery's identity is the file-server lane, not the framework lane
([`VISION.md`](VISION.md) §4). Dynamic handlers do not change that, because they
are governed by one hard rule:

> **Off by default; explicit opt-in, exactly like `--upload`.** A plain
> `servery ./public` serves files and *cannot* execute anything. Dynamic handling
> exists only when the operator names it: `--cgi DIR`, `--wsgi MODULE:app`,
> `--asgi MODULE:app`. No flag → no code execution, no behavior change, no risk.

So servery becomes "a file server that *can* also run a handler when you tell it
to," not "a web framework." It defines no routes, no app object, no middleware —
it *hosts* an interface the operator's own code (or script) implements.

Three more invariants:

- **Zero third-party runtime deps stay zero.** All three interfaces are pure
  stdlib (`subprocess`, `wsgiref`, `asyncio`). The operator's *app* may have its
  own deps — that is the operator's `pip install`, never servery's.
- **Safe by default.** CGI executes processes (a large attack surface); its
  mitigations (below) are requirements, not options. WSGI/ASGI run in-process
  code the operator deliberately chose to load — lower risk, but still bounded.
- **Free-threading aware.** WSGI advertises `wsgi.multithread=True` (servery is
  thread-per-connection); ASGI runs an `asyncio` loop. No module-level mutable
  state, per the project rule.

## 1. Feasibility — proven by prototype

Each interface was prototyped against the stdlib before this plan was written
(the same "build it to be sure" approach used for the self-signed cert and the
HTTP/2 server). All three work with **no third-party packages**:

| Interface | stdlib substrate | PoC result |
|---|---|---|
| **CGI** | `subprocess` + RFC 3875 env mapping | script executes; body piped; **httpoxy + `Authorization` leak mitigations verified** |
| **WSGI** | `wsgiref.handlers.SimpleHandler` (engine) + `wsgiref.validate` (compliance) | app runs on the sync model; `wsgiref.validate` passes |
| **ASGI** | `asyncio.start_server` + a small HTTP/1.1 parse → `scope`/`receive`/`send` | real socket round-trip through a spec-shaped echo app |

Note: the `cgi` *module* was removed in 3.13 (PEP 594), but that module only
*parsed forms inside a script* — it has nothing to do with the server side of
CGI, which is just environment variables + a subprocess. No blocker.

## 2. Per-interface design (modern 3.14+)

### 2.1 WSGI — `--wsgi pkg.module:app` (smallest; fits the architecture)

WSGI (PEP 3333) is **synchronous**, so it maps directly onto servery's existing
thread-per-request handler. Per request:

1. Build `environ` from the request (`REQUEST_METHOD`, `SCRIPT_NAME` [mount
   prefix], `PATH_INFO`, `QUERY_STRING`, `CONTENT_*`, `SERVER_*`, `HTTP_*`,
   `wsgi.input` = the body stream, `wsgi.errors`, `wsgi.url_scheme`,
   `wsgi.multithread=True`).
2. Reuse **`wsgiref.handlers.BaseHandler`/`SimpleHandler`** — the stdlib already
   implements the server side of WSGI correctly (status/header buffering,
   `write()` callable, chunk iteration, `close()`). servery supplies the
   request-derived environ and the socket streams; wsgiref does the protocol.
3. The app is imported once at startup (`module:callable`).

Bounds: reuse the existing `--max-upload-size`-style request-body cap and the
socket `--timeout`. Mounting model: app at `/` by default, optionally under a
prefix (the prefix becomes `SCRIPT_NAME`); static files can still be served from
other paths.

### 2.2 CGI — `--cgi DIR` (small core, security-heavy)

CGI/1.1 (RFC 3875): per request, spawn the script as a child process, pass the
request via environment + stdin, read the response from stdout.

- **`subprocess.run(argv, env=…, input=body, capture_output=True, timeout=…)`** —
  `argv` as a list (**`shell=False`**, so no shell, no Shellshock function
  parsing); a **clean, minimal env** (never inherit the server's environment);
  a hard timeout; a `CONTENT_LENGTH` cap (RFC 3875 §9.6).
- Parse the script's response: header block (`\n\n` / `\r\n\r\n` separator),
  honoring `Status:` and `Location:` (document / local-redirect / client-redirect
  responses, RFC 3875 §6.2). Support `nph-` non-parsed-header scripts later if
  ever needed.
- Only files under the named cgi directory, with the executable bit, that pass
  servery's existing realpath/commonpath containment, are runnable.

### 2.3 ASGI — `--asgi pkg.module:app` (largest; a parallel async stack)

ASGI (3.0) is **asynchronous**, so it cannot ride the sync handler — it needs an
event loop. This phase adds a second, opt-in server built on
**`asyncio.start_server`** with a small HTTP/1.1 codec that maps each request to
an ASGI `scope` + `receive`/`send`:

- **`scope`** (`type:"http"`, method, path, `raw_path`, `query_string`, headers,
  `http_version`, `scheme`, `server`, `client`, `asgi:{version:"3.0"}`).
- **`receive()`** yields `http.request` events (body, `more_body` for streaming
  request bodies); **`send()`** consumes `http.response.start` +
  `http.response.body` events (with backpressure via `drain()`).
- **lifespan** protocol (`startup`/`shutdown`) handled once per process.
- Scope it to the **HTTP** ASGI scope first; **WebSocket** scope is a later,
  separate sub-phase (servery has no WebSocket support today). HTTP/2 for ASGI is
  out of this plan.

This is effectively a minimal `uvicorn` in the stdlib. It is the highest-effort
phase and the one furthest from servery's sync core — hence last, and explicitly
**experimental** until proven.

## 3. Security model (the hard part is CGI)

From RFC 3875 §9 and the well-known CGI CVEs, the following are **requirements**
for the CGI phase (and informed the PoC):

| Threat | Requirement |
|---|---|
| **httpoxy** (CVE-2016-5385): `Proxy:` request header → `HTTP_PROXY` env → SSRF/MITM | **Never** set `HTTP_PROXY`; drop the `Proxy` request header before building the env. |
| **`Authorization` leak** (RFC 3875 §9.2) | Do **not** forward `Authorization` / `Proxy-Authorization` to the script (servery validated Basic auth itself). |
| **Shellshock** (CVE-2014-6271) | `shell=False` (exec the script directly) + a clean, minimal env so crafted values are never shell-parsed. |
| **Path traversal** (RFC 3875 §9.8) | Resolve `..` and run the script + `PATH_INFO` through servery's existing realpath/commonpath containment before exec. |
| **Resource exhaustion** (RFC 3875 §9.6) | Cap `CONTENT_LENGTH`; enforce a per-request `timeout`; bound concurrency. |
| **Privilege** (RFC 3875 §9.5) | Document that scripts run as the server user; recommend a dedicated low-privilege user; never run servery as root. |

WSGI/ASGI run in-process code the operator explicitly imported, so the threat
model is "the operator chose to load this code," not "execute arbitrary files."
They still inherit servery's request-body cap, timeout, and the no-`Authorization`
default unless the app opts in.

## 4. External validation harnesses (incorporated from the start)

Mirroring h2spec (HTTP/2) and testssl.sh (TLS), each interface gets an
independent, standard validator wired into its phase from day one:

| Interface | Harness | Role |
|---|---|---|
| **WSGI** | **`wsgiref.validate.validator`** (stdlib!) | Official PEP 3333 compliance: wrap every test app — it raises on any server- or app-side spec violation. Zero-dep, runs in CI. (Already caught a missing `SCRIPT_NAME` in the PoC.) |
| **WSGI interop** | a real app (Flask/Werkzeug via a *dev/test* extra) driven over the socket with **httpx**/`curl` | proves servery hosts real-world WSGI apps end-to-end. |
| **CGI** | **security regression suite** modeled on the CVEs (httpoxy → no `HTTP_PROXY`; `Authorization` not forwarded; PATH_INFO traversal contained; CONTENT_LENGTH cap; timeout kills a runaway script) + RFC 3875 conformance tests | the safety net is the test suite, since there is no single famous CGI scanner. |
| **CGI interop** | run real CGI scripts (a Python and a shell `echo`/env script) | cross-language interop, like `curl --http2` for h2. |
| **ASGI** | **`asgiref.compatibility`** (app detection/validation) + **httpx `ASGITransport`** + **[`async-asgi-testclient`](https://github.com/vinissimus/async-asgi-testclient)** spec-compliance tests | the closest thing to an ASGI conformance suite. |
| **ASGI interop** | host a real **Starlette** app (dev/test extra) and drive it over the socket with httpx | proves servery runs production ASGI apps; uvicorn's behavior is the reference. |
| **Benchmark** | extend `scripts/bench.py` / `scripts/microbench.py` | WSGI/ASGI throughput vs static; CGI is process-per-request (orders of magnitude slower — measured and documented, not optimized). |

`wsgiref.validate` is zero-dep and belongs in CI. The interop/harness deps
(`flask`, `starlette`, `async-asgi-testclient`) live in the `test` dependency
group only — never runtime — and tests skip when absent, exactly like `httpx`.

## 5. Phased plan

Order = lowest effort + best architectural fit first; highest risk last. Each
phase ships behind its own off-by-default flag with its validator and security
tests green before merge.

| Phase | Scope | Flag | Harness gating the phase | Risk |
|---|---|---|---|---|
| **D1 — WSGI** | sync app hosting via `wsgiref` engine; environ mapping; mount prefix; body/timeout bounds | `--wsgi M:app` | `wsgiref.validate` (CI) + Flask interop (test extra) | Low — maps to the sync model |
| **D2 — CGI** | `subprocess` execution; RFC 3875 env + response parsing; **all §3 mitigations** | `--cgi DIR` | CVE-modeled security suite + RFC 3875 conformance + real-script interop | Medium — security surface; mitigations are the work |
| **D3 — ASGI (experimental)** | `asyncio` HTTP server; HTTP `scope`/`receive`/`send`; lifespan | `--asgi M:app` | `asgiref.compatibility` + `async-asgi-testclient` + Starlette interop | High — a parallel async server |
| **D3b — ASGI WebSocket** (stretch) | `websocket` scope | (same) | httpx-ws / Starlette ws app | High — new protocol surface |

Suggested release shape: D1 in one minor release, D2 in the next, D3 behind an
`experimental` label (like the 3.15 CI tier) until the async stack is proven —
each independently revertable.

## 6. Non-goals / open questions

- **Not** a framework: no routing, no middleware, no app scaffolding. servery
  hosts an interface; the operator brings the app.
- **WebSocket / HTTP-2-for-ASGI**: out of the initial plan (D3b stretch / never).
- **Hot reload, process managers, multiple workers**: out of scope — that is
  gunicorn/uvicorn territory. servery hosts one app, one process.
- **Mounting**: does a dynamic handler take the whole server, or mount under a
  path with static files elsewhere? (Lean: whole-server by default, optional
  prefix.) — to settle in D1.
- **CGI on Windows**: shebang handling differs; D2 should test or scope to POSIX
  first.

## 7. References

- RFC 3875 — The Common Gateway Interface (CGI) Version 1.1 (esp. §4.1 meta-vars,
  §6 response, §9 security). Local copy: the RFC corpus.
- PEP 3333 — Python Web Server Gateway Interface v1.0.1 (WSGI).
- ASGI 3.0 specification — <https://asgi.readthedocs.io/en/latest/specs/main.html>.
- httpoxy — <https://httpoxy.org/> (CVE-2016-5385 and friends).
- Validators/harnesses: `wsgiref.validate` (stdlib), `asgiref.compatibility`,
  httpx `ASGITransport`, `async-asgi-testclient`.
