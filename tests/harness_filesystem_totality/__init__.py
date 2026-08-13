"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001 harness A1.

Filesystem-totality matrix over (filesystem shape) x (archive artifact) x
(production entry point). Nothing in this package is imported by
`app/`; it exists purely to drive the archive modules from the outside,
across every shape a real filesystem can present, with every
potentially-hanging call executed in a subprocess under an
externally-enforced wall-clock timeout.
"""
