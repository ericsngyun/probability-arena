"""MARKET-MICROSTRUCTURE-EDGE-001 prospective sampling contract.

The panel-decision core is deliberately pure: it consumes typed observations
and emits typed decisions, so every invariant in
MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001 can be proven without a
socket. The collector is NOT modified by this package.
"""
