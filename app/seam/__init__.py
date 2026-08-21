"""SOCIAL-FILL-MEASUREMENT-SEAM-001 — the typed seam between SOCIAL-TAPE-001
and REALIZED-FILL-CORPUS-001.

Implements `docs/milestones/EVIDENCE-JOIN-CONTRACT-001.md`. Four types:

``Measurement``          two independent dimensions (availability, observation)
``ObservationTimestamp`` a cross-process clock contract
``TokenResolution``      mint equality is not token identity
``DeliveryCohort``       ``delivery_mode`` as a binding cohort dimension

CONTAINS NO SIGNAL, NO MODEL, NO SCORING. It decides which quantities may be
compared, never what they mean.
"""

from __future__ import annotations

__all__ = ["SEAM_VERSION"]

#: Written onto joined rows. Bumped when the meaning of any seam field moves.
SEAM_VERSION = "social-fill-measurement-seam-001.v1"
