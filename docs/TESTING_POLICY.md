# TESTING_POLICY

1. **Everything green before done.** `.venv/bin/python -m pytest -q` must pass in full. Never mark a milestone complete with failing or skipped-for-convenience tests.
2. **No live LLM or web calls in unit tests.** Every external provider (Kalshi, MLB Stats API, ESPN, DEX Screener, GoPlus, SolanaTracker, Anthropic) must be mocked/injected. The suite must run offline and without credentials; provider API keys must never appear in fixtures, logs, or CLI output (agent-context redaction is tested).
3. **Gated live tests skip by default.** `tests/test_live_kalshi.py` runs only with `RUN_LIVE_TESTS=true` and must stay out of CI paths.
4. **Live smoke ≠ unit tests.** Milestones additionally verify against real APIs manually (documented in the commit message), but that evidence never becomes a required test.
5. **Migrations require up/down tests.** Every Alembic revision gets: upgrade-creates assertions, downgrade-removes assertions, and the ORM-parity test must stay green. SQLite batch mode requires named constraints.
6. **Safety grep before completion** (clean = no implementation surface; boundary-statement docstrings are acceptable):

   ```bash
   grep -rinE "expected_value|kelly|position_siz|paper_trad|place_order|submit_order|create_order|wallet|recommended_side|trade_recommend|execute_trade" app/ --include="*.py"
   ```
7. **Determinism is a feature.** Deterministic components (gates, template collectors/forecasters, scoring math, domain classification) get determinism tests (same input ⇒ identical output). Time-dependent logic must tolerate SQLite's naive-datetime round-trip.
8. **Fallbacks are tested, not assumed.** Every model-assisted or external path needs explicit tests for its failure/fallback behavior, and the fallback must be *honest* (template content stays labeled template).
9. **Central guarantees get adversarial tests.** Confidence caps, evidence-depth recomputation, and status gates are tested against deliberately misbehaving providers (e.g. an overconfident mock forecaster).
10. **Session/fixture hygiene.** In-memory SQLite with `StaticPool` when the TestClient is involved; each test file owns its fixtures; tests may import helpers across test modules but must not depend on execution order.
