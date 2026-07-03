# ADR-002: No wallet / private-key handling

**Status:** Accepted (standing)

## Context
Wallets and private keys are the highest-blast-radius capability an automated system can hold. Agent-operated repos are a poor custody boundary: keys in env files, DBs, or logs are one bug away from exfiltration.

## Decision
This repo, its database, and its deployment never handle wallets or private keys — for Kalshi, crypto, or anything else. No key material, no signing code, no custody logic, not even "disabled" scaffolding. Any future execution capability (itself heavily gated, see `docs/SAFETY_BOUNDARIES.md`) would require a dedicated custody design and security review as its own milestone, with key material held outside this system.

## Consequences
- The `~/awaas/trading` project on the shared EVO-X2 host is explicitly out of scope and shares nothing with this deployment.
- Crypto milestones (CRYPTO-001) are read-only scouting only; wallet milestones are deferred indefinitely.
- Safety greps include wallet terms; tests assert the boundary docs say so.
