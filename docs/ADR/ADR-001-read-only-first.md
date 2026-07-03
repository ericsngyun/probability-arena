# ADR-001: Read-only-first architecture

**Status:** Accepted (MVP-001, reaffirmed every milestone)

## Context
Prediction-market tooling drifts toward trading features long before forecasting skill is proven. Unproven forecasting + trading capability is how money is lost and how agent-built systems become dangerous.

## Decision
The entire system is read-only toward the outside world until calibration proves edge. All external interaction is GETs; all writes go to our own database; no credentials with trading scope are ever configured. Capability expansion is milestone-gated (see `docs/SAFETY_BOUNDARIES.md`).

## Consequences
- Safe to run unattended on a schedule (EVO-X2 timers) without financial risk.
- Every milestone carries a safety grep proving no trading surface exists.
- The cost: the system produces knowledge, not P&L, until the gates are consciously opened — that is the point.
