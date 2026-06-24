# Running apps & proxying

servery's core lane is serving files, but it has **opt-in** modes to run a dynamic
application or forward to an upstream — useful for local development and small
deployments. These replace file serving (you pick one).

!!! note "Scope"

    These are intentionally minimal, stdlib-only handlers — not a production app
    server. See [DYNAMIC.md](../DYNAMIC.md) for the design and the boundaries.

## WSGI

Serve any [PEP 3333](https://peps.python.org/pep-3333/) WSGI app (`Flask`, `Django`,
bare callables):

```bash
servery --wsgi myapp:application
```

The argument is `module:callable`. servery imports `myapp` and serves the
`application` object — streaming request bodies in and the response out over
HTTP/1.1.

```python
# myapp.py
def application(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello from WSGI\n"]
```

## ASGI (experimental)

Serve an [ASGI 3.0](https://asgi.readthedocs.io/) app (`FastAPI`, `Starlette`,
`Quart`):

```bash
servery --asgi myapp:app
```

servery runs an asyncio HTTP/1.1 server for the app (TLS supported). It's marked
experimental — for the full async lifecycle and websockets you'll still want a
dedicated server like uvicorn, but for local dev it's zero-extra-dependencies.

## CGI

Execute classic CGI scripts from a directory as a `cgi-bin`:

```bash
servery --cgi ./cgi-bin
```

!!! warning "CGI runs code"

    `--cgi` executes the scripts in the directory. It's off by default and should
    only point at scripts you trust.

## Reverse proxy

Forward requests under a path prefix to an upstream server — handy for putting a
file share and an API behind one origin during development:

```bash
servery --proxy /api=http://localhost:8001 --proxy /ws=http://localhost:8002
```

`--proxy PREFIX=URL` is repeatable. Requests whose path starts with `PREFIX` are
proxied to the upstream; everything else is served as files.

## One at a time

`--wsgi`, `--asgi`, `--cgi`, and file serving are mutually exclusive — servery does
one job per process. `--proxy` composes with file serving (proxied prefixes first,
files for the rest).

| Flag | Serves |
| --- | --- |
| *(none)* | files + directory listings |
| `--wsgi module:app` | a WSGI application |
| `--asgi module:app` | an ASGI application (experimental) |
| `--cgi DIR` | CGI scripts from DIR |
| `--proxy /p=URL` | forward `/p…` to an upstream (with file serving for the rest) |
