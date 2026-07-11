# Strict ASGI HTTP/1 field syntax — 2026-07-11

Status: accepted. The specialized ASGI parser now rejects malformed non-`Host`
field lines with `400` and closes the connection. This closes the RFC 9112
field-syntax compliance gap without routing the minimal ASGI path through the
shared request-head object model.

## Problem and policy boundary

The threaded and selector adapters already reject colonless fields, empty or
invalid field names, whitespace before the colon, forbidden control bytes in
values, and malformed line folding. ASGI used `bytes.partition(b":")` and then
accepted every non-`Host` result, including a missing colon. Different parsers
disagreeing on message boundaries is a request-desynchronization risk when a
proxy and origin are composed.

This is not an operator compatibility/performance choice. Servery exposes
resource tradeoffs such as connection counts, deadlines, and buffering, but it
does not expose a switch that accepts malformed wire syntax. The implementation
crossover described below is also internal: changing the validation call shape
does not change accepted input, so a public threshold would add configuration
surface without an operational policy benefit.

## Constraints from earlier experiments

The preceding parser work established a hard performance boundary:

- incremental, whole-block, and byte-native adapters around the complete shared
  request parser regressed minimal ASGI throughput by 18.1–27.9%;
- one compiled validator over every ordinary field block regressed throughput
  14.2% and increased p99 22.7%; and
- the accepted specialized `Host` cardinality/authority check remained inside
  budget at -0.7% RPS at 64 connections and -2.2% at concurrency one.

Repeating those object-heavy or unconditional whole-block designs would not
produce new evidence. This slice therefore keeps the current byte parser and
shares only compiled RFC field grammar primitives from `_request.py`.

## Accepted design

The parser still splits the bounded request head once and constructs the same
ASGI byte header pairs. Validation is fused into that existing walk:

1. `Host` retains its stricter byte-native authority validation, so the common
   minimal line pays no second grammar match.
2. Heads with at most eight fields validate each non-`Host` line with the shared
   compiled field grammar. This avoids scanning the entire block for ordinary
   two-field load-generator and browser requests.
3. Heads above eight fields use one compiled block match. Possessive name,
   value, and outer repetitions eliminate unnecessary backtracking and avoid a
   Python-to-regex crossing for every cookie/proxy field.
4. The existing 100-field budget remains distinct: malformed syntax is `400`,
   while a valid over-budget block is `431`.

The eight-field crossover is a measured implementation detail, not a semantic or
resource limit. Both sides implement the same token-name and field-value grammar.
ASGI rejects obsolete folding; the shared threaded parser normalizes it to one
space. Both behaviors are permitted by RFC 9112 section 5.2.

## Rejected shapes in this slice

Microprobes on CPython 3.15 found a compiled bytes expression faster than Python
byte loops, `bytes.translate()` combinations, or split name/value expressions.
End-to-end gates then rejected three otherwise correct forms:

| Shape | Relevant result | Decision |
|---|---:|---|
| Per-line match for every non-`Host` field | 32 fields: -15.5% RPS / +14.1% p99 | Reject; cost scales with field count |
| Small per-line path plus ordinary block regex | 32 fields: -6.7% / +6.4% in the short gate | Optimize; just outside protected budget |
| Bounded valid-name cache plus bulk control scans | 32 fields: -8.1% / +7.7% | Reject; lookups and scans cost more than one match |

The accepted possessive block expression was about 22% faster than the ordinary
block expression in an isolated 32-field CPython 3.15 probe. Binding the compiled
match methods once at module import also removes repeated pattern/method lookup.

The first expanded run against an older frozen image produced an apparent 17.7%
churn regression, but that image's churn rate jumped far above its own immediately
preceding final result. A source-identical rebuilt no-validator control was added
before deciding. This is why image identity, paired order, dispersion, and a
resolution cohort are retained instead of selecting one favorable aggregate.

## Correctness evidence

Wire tests reject all of the following before application dispatch:

- missing colon, empty name, whitespace before colon, and invalid token
  separators;
- NUL, vertical-tab, DEL, and stray carriage-return bytes in values;
- obsolete folded lines; and
- the same failures in a block large enough to take the bulk path, including a
  colonless name seen in an earlier request.

Valid token punctuation, optional whitespace, horizontal tab, and `obs-text`
remain accepted. A byte-at-a-time socket test exercises both valid and invalid
heads, proving that TCP fragmentation does not change the result. Existing
`Host`, framing, keep-alive, body, TLS, and 100-field-limit tests continue to use
the same parser.

## Performance evidence

The final comparison uses CPython 3.15.0b3 with the GIL, one isolated server CPU,
disjoint client CPUs, one-second warmup, five five-second trials, balanced server
order, correct response probes, and a source-identical rebuilt control with only
the new validation branch removed. Every timed sample recorded zero errors.

Primary artifact (gitignored):
`benchmarks/artifacts/asgi-field-syntax-final-2026-07-11.json`.

| Scenario | Median paired RPS change | RPS ratio MAD | Median paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|
| `asgi-1k`, 64 connections | -3.35% | 3.91 points | +3.66% | 1.76 points |
| `asgi-headers-32`, 64 connections | -5.01% | 3.53 points | +3.60% | 0.83 points |
| `asgi-churn-1k`, 32 connections | -13.86% | 17.53 points | +0.76% | 19.19 points |

The churn row is not decision-grade: one baseline sample reached the harness's
90% client-limit marker, and both paired metrics are much smaller than their
dispersion/range. It is not treated as a regression claim. A seven-trial,
five-second concurrency-one resolution run reduced client pressure:

| Scenario | Median paired RPS change | RPS ratio MAD | Median paired p99 change | p99 ratio MAD |
|---|---:|---:|---:|---:|
| `asgi-churn-1k`, one connection | -0.65% | 17.60 points | +0.15% | 11.43 points |
| `asgi-headers-32`, one connection | -4.17% | 2.58 points | +9.85% | 5.86 points |

Churn remains too dispersed for a directional claim, but its point estimate is
neutral. The header-heavy throughput estimate is inside the 5% budget; its p99
moves from about 0.10 to 0.11 ms, so the percentage is large relative to a very
small absolute latency. At 64 connections its p99 effect is +3.60%. The evidence
supports a correctness acceptance, not a performance improvement claim.

Resolution artifact (gitignored):
`benchmarks/artifacts/asgi-field-syntax-c1-final-2026-07-11.json`.
Rejected diagnostic artifacts retain the `asgi-field-v1` through `v3` labels.
The accepted image is `sha256:16b8e335...`, the rebuilt control is
`sha256:630b5db9...`, and the final artifact records product-tree hash
`25fcedaf...`.

The harness now includes opt-in `asgi-headers-32`: loadgen supplies 30 stable
custom fields in addition to `Host` and `Connection`. It is intentionally not a
default external-comparison scenario because it exists to protect parser-cost
scaling rather than represent a universal request mix.

## Decision and follow-up

Accept the hybrid validator. It closes a mandatory message-boundary gap, keeps
ordinary and header-heavy throughput at the protected budget, preserves the
specialized minimal-ASGI engine, and introduces no permissive configuration.

Future parser work should add large cookie values, high-cardinality field names,
near-64-KiB heads, slow fragmented heads, and proxy differential corpora. A
complete shared request object remains rejected until a different representation
can beat the earlier 18–28% cost. Synthetic post-response disconnect,
including the closed-send follow-up, is now completed by the later ASGI
lifecycle experiments. Larger request-body streaming, native Uvicorn cohorts,
and framework compatibility remain separate ASGI roadmap items.

## Verification

- 802 functional tests pass on CPython 3.15 with the GIL and 802 pass on the
  free-threaded build; both populated environments have four optional skips.
- The targeted field-syntax, fragmentation, and shared-parser tests also pass on
  CPython 3.14.
- Repository-wide Ruff lint/format, Bandit, strict MkDocs, and
  `git diff --check` pass. Ty reports only two pre-existing unused-suppression
  warnings.
- The final benchmark artifact's product-tree hash matches the current product
  source exactly.
- Fresh wheel and source distributions build, the wheel has no unconditional
  runtime dependencies, and installed import/CLI smoke passes outside the source
  tree.
