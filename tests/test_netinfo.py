"""LAN IP detection tests."""

from __future__ import annotations

import ipaddress
import unittest

from servery import _netinfo


class NetinfoTest(unittest.TestCase):
    def test_primary_lan_ipv4_returns_usable_address(self):
        ip, status = _netinfo.primary_lan_ipv4()
        self.assertIn(status, ("ok", "loopback", "offline"))
        ipaddress.ip_address(ip)  # always a valid IPv4 literal
        if status == "ok":
            self.assertFalse(ipaddress.ip_address(ip).is_loopback)

    def test_display_host_passes_through_concrete_address(self):
        host, status = _netinfo.display_host("203.0.113.7")
        self.assertEqual((host, status), ("203.0.113.7", "ok"))

    def test_display_host_substitutes_lan_ip_for_wildcard(self):
        host, _status = _netinfo.display_host("0.0.0.0")
        # Wildcard isn't connectable, so it's replaced with the detected LAN IP
        # (or a loopback/offline fallback). Never returns the wildcard itself.
        self.assertNotEqual(host, "0.0.0.0")
        ipaddress.ip_address(host)


if __name__ == "__main__":
    unittest.main()
