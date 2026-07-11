# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private **Security advisories → Report a vulnerability** workflow for this
repository. Include the affected version and platform, prerequisites, impact,
minimal reproducer, and any known mitigation. Do not include real credentials,
private keys, or third-party data.

If private advisory reporting is unavailable, open a public issue containing no
exploit details and ask the maintainer for a private contact channel.

We aim to acknowledge a report within three business days and provide an initial
severity/scope assessment within seven calendar days. Remediation targets after
validation are seven days for critical issues, 30 days for high severity, 90
days for moderate severity, and the next planned release for low severity.
These are response targets, not warranties; a coordinated disclosure date will
account for exploitability, upstream dependencies, and the time operators need
to update.

The eventual advisory will credit the reporter unless anonymity is requested,
identify affected and fixed versions, document mitigations, and link exact fixed
release artifacts. Please allow a coordinated fix and release before publishing
exploit details.

## Supported versions

| Version | Security support |
| --- | --- |
| Latest released servery minor, on its newest patch | Supported |
| Immediately preceding servery minor | Supported for 90 days after the next minor release |
| Older releases and unreleased source snapshots | Not supported; reports are still welcome |

Security fixes are released as new immutable versions. Users may need to upgrade
to the latest minor when a safe fix cannot be backported without changing a
protocol or configuration contract.

The package currently requires CPython 3.13 or newer. Production support is
narrower than installation metadata: it covers only CPython minor/platform
combinations exercised by the release's documented CI matrix, on a still-
supported upstream patch release. New CPython minors are not production-
supported until that matrix includes them. Free-threaded builds are supported
only where the release matrix names them.

HTTP/3 is a separate optional tier. Its `aioquic` dependency and transitive
native/runtime dependencies must be on supported, patched versions. Vulnerabilities
in CPython, OpenSSL, the operating system, or optional dependencies may require
an upstream upgrade rather than a servery patch.

## Production-edge status

The directly exposed production profile is under development and is not yet a
released production-readiness claim. Its threat model, required controls, and
release gates are documented in the
[production-edge threat model](docs/design/production-edge-threat-model.md) and
[execution backlog](docs/design/production-edge-execution-backlog.md). Until all
release gates pass, findings in those areas are handled through the same private
reporting process but no production service-level guarantee is implied.
