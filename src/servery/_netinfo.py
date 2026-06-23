"""LAN network introspection (pure stdlib): which IPv4 a phone would use to reach us."""

from __future__ import annotations

import ipaddress
import socket


def primary_lan_ipv4(target: str = "203.0.113.1") -> tuple[str, str]:
    """Return ``(ip, status)`` for the host's primary LAN IPv4.

    Uses the standard trick: ``connect()`` a UDP socket toward a routable address —
    which sends NO packets, it's just a routing-table lookup — then read back the
    source address the kernel chose. ``target`` only needs to be routable (default is
    a TEST-NET-3 address, RFC 5737, so we never depend on a real host being up).

    ``status`` is ``"ok"`` (a usable LAN address), ``"loopback"`` (only 127.* — phones
    can't reach us), or ``"offline"`` (no route at all).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        ip = sock.getsockname()[0]
    except OSError:
        return "127.0.0.1", "offline"
    finally:
        sock.close()
    if not ip or ip == "0.0.0.0":  # nosec B104 - comparing the result, not binding
        return "127.0.0.1", "offline"
    if ipaddress.ip_address(ip).is_loopback:
        return ip, "loopback"
    return ip, "ok"


def display_host(bind_host: str) -> tuple[str, str]:
    """Pick the host to show in a URL/QR for a server bound to ``bind_host``.

    A wildcard bind (``0.0.0.0``/``::``) is not connectable, so we substitute the
    detected LAN IP. A concrete bind address is shown as-is. Returns ``(host,
    status)`` where status mirrors :func:`primary_lan_ipv4` (``"ok"`` for an explicit
    address).
    """
    try:
        wildcard = ipaddress.ip_address(bind_host).is_unspecified
    except ValueError:
        wildcard = False
    if wildcard:
        return primary_lan_ipv4()
    return bind_host, "ok"
