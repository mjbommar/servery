# Design: TFTP tier (RFC 1350)

Status: implemented. Scope: a separate UDP listener, opt-in `--tftp`. Zero-dep
(stdlib `socket`/`struct`/`tempfile`).

## Goal
Serve the same directory over TFTP for the niche nothing modern replaced — **PXE
network boot** and pushing firmware/configs to switches, routers, phones, and
embedded gear — without burdening the zero-dependency core or the safe defaults.

## Scope-rubric fit (PRINCIPLES.md §7)
- **Zero-dep:** pure stdlib; TFTP is a tiny line/struct protocol.
- **File-server lane:** "share a folder," over a different transport — not an app.
- **Safe-default:** TFTP has **no auth and no encryption** and is a DDoS-amplification
  surface, so it is **off by default**, **read-only** unless `--tftp-write`, prints a
  loud startup warning, and is documented as trusted-LAN-only. The safe default
  (a bare `servery`) is unchanged.
- **Smallness:** ~300 LOC, isolated in one module off the default path.

## Architecture
- A **separate UDP listener** (not the HTTP socket), started by the unified
  `server.serve()` lifecycle alongside HTTP/HTTP3 and stopped in a `finally`.
- `servery/_tftp.py :: TftpServer`: a main socket accepts RRQ/WRQ; each transfer runs
  in a worker thread on a **fresh ephemeral socket** (the per-transfer TID, RFC 1350
  §4), bound to the operator's configured address.
- **Path safety reuses the one HTTP-agnostic choke-point**, `security.safe_join`, so a
  filename can never escape the served root — the same guarantee as the HTTP side.

## Protocol coverage
- RRQ (read) and WRQ (write); DATA/ACK lockstep with **timeout-retransmit** (5 tries).
- **octet** and **netascii** modes. Netascii uses a stateful streaming converter, so
  CR/LF pairs remain correct across filesystem reads and TFTP blocks without an
  entire-file allocation.
- **RFC 2347-2349 options** — `blksize`, `tsize`, `timeout` — negotiated via OACK
  (the bits PXE relies on). `blksize` clamped to `[8, 65464]`.
- ERROR packets: file-not-found, access-violation (writes disabled / escape),
  file-exists (no-overwrite default), disk-full (size cap), illegal-operation
  (bad mode / malformed request), unknown-TID (stray peer).
- Writes stream to a tempfile in the target dir and commit with an atomic
  `os.replace`; existing files are refused (no silent overwrite); the total is bounded
  by `--max-upload-size`. The same canonical-target coordinator used by HTTP writes
  covers the entire TFTP transfer.

## Config / CLI
- `Config.tftp` / `tftp_port` (default 69) / `tftp_write` and
  `max_tftp_transfers` (default 32); `--tftp-write` requires `--tftp`. Saturation is
  rejected before a worker/thread is created and capacity recovers after the full
  transfer lifetime. Startup warnings cover no auth/encryption and anonymous writes.

## Out of scope (now)
- FTP / FTPS (deprecated; browsers removed FTP; cleartext + bounce/firewall footguns).
- TFTP "dallying" (re-ACK after the final block) and multicast TFTP (RFC 2090).
- Authentication or encryption for TFTP (the protocol has none; don't pretend).
