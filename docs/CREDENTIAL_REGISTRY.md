# Credential registry

**Metadata only. No key ids, no fingerprints, no values — ever.** Publishing a
fingerprint into this repository is what caused the 2026-08-20 disclosure
incident; this file exists to make credentials *accountable*, not visible.

> **The rule: every production credential has ONE documented purpose and ONE
> active consumer. An unexplained credential is unnecessary attack surface and
> is revoked, not archived.**

Schema per row: alias · venue · purpose · host · scope · created · rotation due ·
owner · status. The alias is a human label chosen here; it is not derived from
the credential.

---

## Production

| alias | venue | purpose | host | scope | created | rotation due | owner | status |
|---|---|---|---|---|---|---|---|---|
| `kalshi-observer-prod` | Kalshi | read-only market-data observation (WS subscriptions + `api_keys` scope audit). Consumer: `app/realtime/collector.py` via `KALSHI_OBSERVER_*` | observation host | `["read"]`, attested by venue at B1 closure and re-attested after rotation | — | 2027-02-21 (6 mo) | Eric | **ACTIVE** — rotated 2026-08-21 |
| `kalshi-prod-unidentified-2` | Kalshi | **UNKNOWN** | unknown | unknown | unknown | — | unknown | **INVENTORY REQUIRED** |

### `kalshi-prod-unidentified-2` — open item

The production account holds **two** keys. One is the active observer; the
second is **not** used by this project and is **not** the key exposed in the
2026-08-20 incident (verified by fingerprint comparison — the leaked key is
gone from the account).

**Do not delete it blindly.** Inventory first: name, created date, intended
owner, current consumer, purpose. If no legitimate owner or purpose can be
identified, revoke it. A credential nobody can account for is exactly the thing
this registry exists to surface.

## Non-production

| alias | venue | purpose | host | scope | status |
|---|---|---|---|---|---|
| `kalshi-observer-demo` | Kalshi demo | DEMO validation and load-shape work; the installer supports `--env demo` | observation host | read | present; **unaudited by this registry** |

## Declared but unregistered

`app/config.py` declares these credential fields. **Declared is not the same as
populated**, and this registry does not read any host's environment to find out
— that audit belongs to an operator, not to a document.

| config field | apparent purpose | registry status |
|---|---|---|
| `tennis_provider_api_key` | tennis data provider | **unregistered** |
| `goalserve_tennis_api_key` | Goalserve tennis feed | **unregistered** |
| `goplus_api_key` | token risk screening | **unregistered** |
| `solana_tracker_api_key` | Solana discovery/coverage | **unregistered** |
| `birdeye_api_key` | Solana market data | **unregistered** |

Each needs the same treatment as the Kalshi row: one purpose, one consumer, one
owner — or removal of the field. A config field that no live consumer reads is
a standing invitation to populate it for no reason.

## Operating rules

1. **Never record a key id, fingerprint, or value here**, or in any tracked
   file. `tests/test_no_infrastructure_disclosure_001.py` enforces the literal
   case; the rest is discipline.
2. **Secrets arrive on stdin only** — see
   `docs/KALSHI_OBSERVER_KEY_ROTATION.md`. An argv secret is visible in `ps`
   and lands in shell history, and neither is recoverable after the fact.
3. **Scope is attested, never asserted.** "We created it read-only" is not
   evidence; `credential_audit.audit_scopes` asking the venue is. It halts
   rather than degrading, because *we could not check* must never read the same
   as *we checked and it was fine*.
4. **Rotate on a schedule, and on any disclosure** — even a disclosure of
   non-secret material such as a fingerprint, since rotation is what makes the
   published artifact worthless.
5. **A credential with no active consumer is revoked.** Not disabled, not left
   for later.
