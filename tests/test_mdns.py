"""mDNS / DNS-SD responder tests (RFC 6762/6763): wire encoding + matching."""

from __future__ import annotations

import socket
import struct
import unittest

from servery import _mdns


def _parse_answers(packet: bytes) -> list[tuple[str, int, int, int]]:
    """Return (name, rtype, rclass, ttl) for each Answer record in a response."""
    _id, _flags, qd, an, _ns, _ar = struct.unpack_from("!HHHHHH", packet, 0)
    assert qd == 0
    offset = 12
    out = []
    for _ in range(an):
        name, offset = _mdns._read_name(packet, offset)
        rtype, rclass, ttl, rdlen = struct.unpack_from("!HHIH", packet, offset)
        offset += 10 + rdlen
        out.append((name, rtype, rclass, ttl))
    return out


class WireTest(unittest.TestCase):
    def test_encode_name(self):
        self.assertEqual(
            _mdns._encode_name("_http._tcp.local"),
            b"\x05_http\x04_tcp\x05local\x00",
        )

    def test_read_name_roundtrip(self):
        for name in ("_http._tcp.local", "myhost.local", "servery on box._http._tcp.local"):
            decoded, _ = _mdns._read_name(_mdns._encode_name(name), 0)
            self.assertEqual(decoded, name.lower())

    def test_build_answer_records(self):
        packet = _mdns.build_answer("servery on box (8000)", "box", "192.168.1.7", 8000)
        answers = _parse_answers(packet)
        types = {rtype: (name, rclass, ttl) for name, rtype, rclass, ttl in answers}
        self.assertEqual(
            set(types), {_mdns._TYPE_PTR, _mdns._TYPE_SRV, _mdns._TYPE_TXT, _mdns._TYPE_A}
        )
        # PTR is a shared record (no cache-flush, long TTL); A/SRV are unique (flush).
        self.assertEqual(types[_mdns._TYPE_PTR][0], "_http._tcp.local")
        self.assertEqual(types[_mdns._TYPE_PTR][1], _mdns._CLASS_IN)
        self.assertEqual(types[_mdns._TYPE_A][1], _mdns._CLASS_FLUSH)
        self.assertEqual(types[_mdns._TYPE_A][2], _mdns._TTL_HOST)
        self.assertEqual(types[_mdns._TYPE_SRV][2], _mdns._TTL_HOST)

    def test_goodbye_has_zero_ttl(self):
        packet = _mdns.build_answer("i", "h", "10.0.0.1", 80, goodbye=True)
        self.assertTrue(all(ttl == 0 for _n, _t, _c, ttl in _parse_answers(packet)))

    def test_a_record_holds_the_ip(self):
        packet = _mdns.build_answer("i", "h", "192.168.1.7", 80)
        self.assertIn(socket.inet_aton("192.168.1.7"), packet)


class MatchTest(unittest.TestCase):
    def setUp(self):
        self.r = _mdns._Responder("servery on box (8000)", "box", "192.168.1.7", 8000)

    def _query(self, name: str, qtype: int) -> bytes:
        return (
            struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
            + _mdns._encode_name(name)
            + struct.pack("!HH", qtype, 1)
        )

    def test_matches_service_ptr(self):
        self.assertTrue(self.r._matches(self._query("_http._tcp.local", _mdns._TYPE_PTR)))
        self.assertTrue(self.r._matches(self._query("_http._tcp.local", _mdns._TYPE_ANY)))

    def test_matches_our_host(self):
        self.assertTrue(self.r._matches(self._query("box.local", _mdns._TYPE_A)))

    def test_ignores_other_services(self):
        self.assertFalse(self.r._matches(self._query("_ssh._tcp.local", _mdns._TYPE_PTR)))
        self.assertFalse(self.r._matches(self._query("other.local", _mdns._TYPE_A)))


if __name__ == "__main__":
    unittest.main()
