"""HTTP Basic authentication (RFC 7617) with a single shared credential.

The credential is specified as ``USER:PASSWORD`` or, to avoid putting a plaintext
password on the command line, as a pre-hashed ``USER:sha256:<hex>`` /
``USER:sha512:<hex>``. All comparisons use :func:`hmac.compare_digest`, and both
the username and password are always compared (no short-circuit) so the response
time does not leak which half was wrong.

Basic auth is meaningless without TLS — the credential is base64, not encrypted —
so :meth:`servery.config.Config.startup_warnings` shouts when it runs over plain
HTTP.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac

_HASH_ALGORITHMS = ("sha256", "sha512")


def _ct_equal(expected: str, given: str) -> bool:
    """Length-independent constant-time string comparison.

    ``hmac.compare_digest`` short-circuits on differing lengths, leaking length
    via timing; hashing both sides to a fixed width removes that signal.
    """
    return hmac.compare_digest(
        hashlib.sha256(expected.encode("utf-8")).digest(),
        hashlib.sha256(given.encode("utf-8")).digest(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class Credential:
    """A single username + password verifier."""

    username: str
    secret: str  # plaintext password, or a hex digest
    algorithm: str  # "plain", "sha256", or "sha512"

    def verify(self, username: str, password: str) -> bool:
        """Constant-time check of a username/password pair (length-independent)."""
        user_ok = _ct_equal(self.username, username)
        if self.algorithm == "plain":
            pass_ok = _ct_equal(self.secret, password)
        else:
            digest = hashlib.new(self.algorithm, password.encode("utf-8")).hexdigest()
            pass_ok = _ct_equal(self.secret.lower(), digest)
        return bool(user_ok & pass_ok)

    def check_header(self, header: str) -> bool:
        """Validate an ``Authorization: Basic ...`` header value."""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        return self.verify(username, password)


def parse(spec: str | None) -> Credential | None:
    """Parse a ``USER:PASSWORD`` / ``USER:sha256:<hex>`` spec into a credential."""
    if spec is None:
        return None
    username, separator, rest = spec.partition(":")
    if not separator or not username:
        raise ValueError("auth must be in the form USER:PASSWORD")
    algorithm, algo_sep, digest = rest.partition(":")
    if algo_sep and algorithm in _HASH_ALGORITHMS:
        if not digest:
            raise ValueError("hashed auth must be USER:sha256:<hex> or USER:sha512:<hex>")
        return Credential(username, digest, algorithm)
    return Credential(username, rest, "plain")
