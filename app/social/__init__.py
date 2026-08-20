"""SOCIAL-TAPE-001 — social-media collection infrastructure.

COLLECTION ONLY. This package contains no signal, no score, no ranking and no
prediction. It records what was said, by whom, and — critically — *when we
first saw the bytes*, which is the one quantity that cannot be reconstructed
after the fact.

Nothing in this package is deployed or activated. Activation requires, jointly:
  1. a named source universe (`app.social.sources`), and
  2. an explicitly configured monthly cost cap (`app.social.cost_guard`), and
  3. separate authorization recorded in the milestone doc.

See `docs/milestones/SOCIAL-TAPE-001.md`.
"""
