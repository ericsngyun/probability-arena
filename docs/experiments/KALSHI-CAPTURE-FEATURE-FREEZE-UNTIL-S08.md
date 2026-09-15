# Kalshi capture path — hard feature freeze until S08 closes

**Effective:** 2026-09-15, after S08-r2 authorization revision 2.
**Lifts:** when S08 has a recorded L1–L4 verdict and the coverage/debt ledger
is updated for the next obligation.

## Frozen (do not edit)

* `app/microstructure/lifecycle_compatibility.py`
* `app/microstructure/capture_contract.py`
* `app/microstructure/panel.py` / `rows.py` / `labels.py` / `features.py`
* `scripts/kalshi_microstructure_arm_session.py`
* `scripts/kalshi_microstructure_capture_runner.py`
* `scripts/kalshi_microstructure_schedule_anchor.py`
* S08 freeze artifacts under `docs/evidence/mmedge-s08-r2-freeze/` and
  EVO `~/microstructure-tranche/s08-r2-freeze/`

No lifecycle logic, scheduler improvements, feature engineering, or convenience
refactors on this path until S08 closes.

## Allowed

* Sunday arm/launch of the **already frozen** S08-r2 session
* L1–L4 / support ledger / coverage-debt bookkeeping after capture
* Deterministic S09 scheduler run after S08 verdict
* Unrelated Solana / X observation work

## Sunday arm window

Session start: **2026-09-20T19:40:00Z** (12:40 PM PDT).
Arm only **T−60…T−30** (11:40–12:10 PM PDT).
On failure: refuse + retain S07 replacement debt. No substitution.
