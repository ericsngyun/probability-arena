# ADR-003: Deterministic hot path, model-assisted warm path

**Status:** Accepted (MVP-003A onward)

## Context
The pipeline runs unattended every 4 hours (and the watcher every 60 s). Model calls in hot paths create nondeterminism, cost, latency, and silent-failure modes exactly where auditability matters most.

## Decision
Hot paths (eligibility gating, ranking, watcher signal detection, scoring math, domain classification, template collectors/forecasters, the baseball evidence model) are deterministic: same input ⇒ same output, testable byte-for-byte. Model-assisted components (LLM judge/collector/forecaster) live on the warm path: behind default-off flags, invoked on selected items (promoted signals, ad-hoc requests), always with a deterministic fallback and honest degradation. Central post-processing (confidence caps, evidence-depth recomputation) applies regardless of which provider produced the output.

## Consequences
- The scheduled loop can never hard-fail or overspend because of a model provider.
- Calibration comparisons are clean: deterministic baselines vs flagged challengers on the same inputs.
- New intelligence must arrive as either a deterministic algorithm or a flag-gated provider with a tested fallback — no third pattern.
