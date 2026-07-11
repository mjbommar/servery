"""Binding and adoption primitives for supervised TCP listeners.

The process that calls :func:`bind_tcp_listener` owns the returned socket.  A
runtime adopts a listener by duplicating its descriptor with
:func:`adopt_tcp_listener`; the runtime owns and closes only that duplicate.
This keeps listener lifetime independent across a future supervisor and its
workers without requiring either runtime to know about process management.
"""

from __future__ import annotations

import contextlib
import errno
import socket


def _address_family(host: str) -> socket.AddressFamily:
    """Match the server's established literal-host address-family policy."""
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def bind_tcp_listener(
    host: str,
    port: int,
    *,
    port_scan: int = 64,
    backlog: int = 128,
) -> socket.socket:
    """Bind and listen on a TCP socket, returning it to the caller as owner.

    A nonzero busy port scans forward by at most ``port_scan`` ports.  Port zero
    is passed directly to the kernel, and errors other than address-in-use are
    never hidden by scanning.
    """
    if port_scan < 0:
        raise ValueError("port_scan must be non-negative")
    family = _address_family(host)
    in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
    stop = 1 if port == 0 else min(port + port_scan + 1, 65536)
    last: OSError | None = None
    for candidate in range(port, stop):
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                with contextlib.suppress(OSError):
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind((host, candidate))
            listener.listen(backlog)
        except OSError as exc:
            listener.close()
            if port == 0 or exc.errno not in in_use:
                raise
            last = exc
            continue
        return listener
    raise last if last is not None else OSError("no free port found")


def adopt_tcp_listener(listener: socket.socket, *, host: str) -> socket.socket:
    """Validate ``listener`` and return a runtime-owned descriptor duplicate.

    The caller continues to own ``listener``.  The returned socket has an
    independent descriptor lifetime, so either owner may close its handle
    without invalidating the other.  As with all duplicated POSIX descriptors,
    kernel file-status flags are shared by the underlying open file description.
    """
    if listener.fileno() < 0:
        raise ValueError("listener socket is closed")
    expected_family = _address_family(host)
    if listener.family != expected_family:
        raise ValueError(
            f"listener address family {listener.family!r} does not match host {host!r}"
        )
    if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise ValueError("listener must be a TCP stream socket")
    if hasattr(socket, "SO_ACCEPTCONN") and not listener.getsockopt(
        socket.SOL_SOCKET, socket.SO_ACCEPTCONN
    ):
        raise ValueError("listener socket must already be listening")
    # getsockname() also rejects descriptors that are not usable sockets on
    # platforms where SO_ACCEPTCONN is unavailable.
    listener.getsockname()
    return listener.dup()
