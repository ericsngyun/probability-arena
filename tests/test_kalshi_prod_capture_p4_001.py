"""KALSHI-PROD-QUAL-CAPTURE — positive controls for the production-evidence gates.

**Why this file exists.** The capture instrument's whole job is to REFUSE: it
must not let a frame be archived under the `production` label unless the socket
carrying it has been shown to be production. An untested refusal is the exact
failure class this repository keeps finding — *a plausible benign value emitted
by a broken path*. If `_socket_evidence` silently stopped checking, every run
would still report `passed: true` and a mislabelled tape would sail through,
looking identical to a compliant one forever after.

So every gate here is tested the way AGENTS.md doctrine 7 requires: **force the
underlying condition and prove the gate becomes non-benign.**

| force this | this must happen |
|---|---|
| a certificate that covers a demo name | `passed` is False and the finding names the demo name |
| a certificate that does not cover the expected host | `passed` is False |
| no readable certificate at all | `passed` is False — a missing certificate is not evidence of a good one |
| a non-production URI handed to the connector | it raises before connecting |
| a valid production certificate | `passed` is **True** — the anti-vacuity arm, without which every ban above is satisfied by a gate that always fails |

Nothing here opens a socket, reads a credential or contacts a venue.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


P4 = _load_script("kalshi_prod_capture_p4")

PROD_HOST = "external-api-ws.kalshi.com"


def _cert(*dns_names: str) -> dict:
    """A `getpeercert()`-shaped dict carrying the given SANs."""
    return {
        "subjectAltName": tuple(("DNS", name) for name in dns_names),
        "issuer": ((("organizationName", "Amazon"),),),
        "notAfter": "Feb  2 23:59:59 2027 GMT",
    }


class _FakeTransport:
    def __init__(self, cert) -> None:
        self._cert = cert

    def get_extra_info(self, name):
        return {"peercert": self._cert,
                "cipher": ("TLS_AES_128_GCM_SHA256", "TLSv1.3", 128),
                "peername": ("203.0.113.7", 443)}.get(name)


class _FakeConn:
    def __init__(self, cert) -> None:
        self.transport = _FakeTransport(cert)
        self.response = None


# --- the SAN matcher ------------------------------------------------------------


@pytest.mark.parametrize("names,host,expected", [
    (["*.kalshi.com"], "external-api-ws.kalshi.com", True),
    (["kalshi.com"], "kalshi.com", True),
    # A wildcard covers exactly ONE label. `*.kalshi.com` must not be read as
    # covering a deeper name, or a subdomain nobody vetted would pass.
    (["*.kalshi.com"], "a.b.kalshi.com", False),
    (["*.demo.kalshi.co"], "external-api-ws.kalshi.com", False),
    ([], "external-api-ws.kalshi.com", False),
    (None, "external-api-ws.kalshi.com", False),
])
def test_san_matching_is_label_exact(names, host, expected):
    assert P4._san_covers(names, host) is expected


# --- E5: the certificate on the socket the frames arrive on ---------------------


def test_a_production_certificate_PASSES_the_socket_gate():
    """Anti-vacuity. Without this, every ban below is met by a gate that
    always refuses, and the suite would certify a capture that can never run."""
    connector = P4.ProductionVerifyingConnector(PROD_HOST)
    record = connector._socket_evidence(_FakeConn(_cert("*.kalshi.com")),
                                        P4.SPEC_PRODUCTION_WS)
    assert record["passed"] is True
    assert record["findings"] == []
    assert record["peer_certificate_subject_alt_names"] == ["*.kalshi.com"]


def test_a_demo_certificate_on_the_capture_socket_is_REFUSED():
    connector = P4.ProductionVerifyingConnector(PROD_HOST)
    record = connector._socket_evidence(
        _FakeConn(_cert("*.demo.kalshi.co", "demo.kalshi.co")),
        P4.SPEC_PRODUCTION_WS)
    assert record["passed"] is False
    # It must fail for BOTH reasons, and say so: the cert does not cover the
    # production host, and it covers a demo name.
    assert any("does not cover" in f for f in record["findings"])
    assert any("demo" in f for f in record["findings"])


def test_a_certificate_for_the_wrong_host_is_REFUSED():
    connector = P4.ProductionVerifyingConnector(PROD_HOST)
    record = connector._socket_evidence(_FakeConn(_cert("*.example.com")),
                                        P4.SPEC_PRODUCTION_WS)
    assert record["passed"] is False
    assert any("does not cover" in f for f in record["findings"])


def test_an_unreadable_certificate_is_REFUSED_not_assumed_good():
    """A missing certificate is not evidence of a good one (doctrine 10)."""
    connector = P4.ProductionVerifyingConnector(PROD_HOST)
    for missing in ({}, None):
        record = connector._socket_evidence(_FakeConn(missing),
                                            P4.SPEC_PRODUCTION_WS)
        assert record["passed"] is False
        assert any("UNESTABLISHED" in f for f in record["findings"])


@pytest.mark.asyncio
async def test_the_connector_refuses_a_non_production_uri_before_connecting():
    """The refusal must precede the socket, not follow it."""
    connector = P4.ProductionVerifyingConnector(PROD_HOST)
    with pytest.raises(P4.ProductionEvidenceError, match="not.*production host"):
        await connector("wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2")
    assert connector.connections == []


# --- E1: the host constants -----------------------------------------------------


def test_the_spec_constants_are_independent_of_the_code_under_test():
    """`SPEC_*` must not be read from `WS_HOSTS`, or the check cannot fail.

    This is the arm that would catch `WS_HOSTS[production]` itself being wrong,
    so it has to be a separately written-down value.
    """
    source = (REPO / "scripts" / "kalshi_prod_capture_p4.py").read_text()
    assert 'SPEC_PRODUCTION_WS = "wss://external-api-ws.kalshi.com' in source
    assert 'SPEC_PRODUCTION_REST = "https://api.elections.kalshi.com' in source


# --- typed absences: the contract's unmeasurables stay unmeasurable -------------


def test_ticker_sequence_gaps_are_typed_NOT_MEASURABLE_never_zero():
    from kalshi_collector_p0_wire_probe import WireRecorder

    wire = WireRecorder(samples_per_type=2)
    for _ in range(5):
        wire.observe({"type": "ticker", "sid": 2, "msg": {"market_ticker": "X"}})

    absences = P4.typed_absences(wire)
    gaps = absences["ticker_sequence_gaps"]
    assert gaps["state"] == "NOT_MEASURABLE:empty_sequence_domain"
    assert "2" in gaps["sids"]
    # The whole point: no numeric zero anywhere in this block.
    assert 0 not in gaps.values()

    assert absences["recoveries_from_tape"]["state"] == (
        "NOT_RECONSTRUCTABLE_BY_DESIGN")
    # B3 CLOSED 2026-08-19 (KALSHI-P4-1-REPLAY-REQUAL). The blocker is gone; the
    # verdict is still not claimed, because it has never been RUN over a
    # production tape — and it stays typed rather than becoming a bare False.
    replay_equality = absences["replay_equality"]
    assert replay_equality["state"] == "NOT_QUALIFIED:NOT_YET_COMPUTED"
    assert replay_equality["b3_closed_by"] == "KALSHI-P4-1-REPLAY-REQUAL"
    assert "B3_OPEN" not in replay_equality["state"]
    assert absences["transport_dropped_frames"]["state"] == (
        "NOT_MEASURABLE:no_source_exists")


def test_an_omitted_ladder_contributes_no_spread_and_is_typed_NOT_PROVIDED():
    """Doctrine 10 on the analysis half: a ladder the venue never sent must not
    become a spread sample, and must not be recorded as an empty one."""
    rates = P4.RateRecorder()
    rates.observe({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": "X", "yes_dollars_fp": [["0.4700", "5.00"]]}})

    assert rates.spread_samples_snapshot == []
    assert rates.ladder_presence_census["yes=PRESENT no=NOT_PROVIDED"] == 1
    # NOT_PROVIDED and EMPTY are different observations and must not merge.
    rates.observe({"type": "orderbook_snapshot", "sid": 1, "seq": 2, "msg": {
        "market_ticker": "X", "yes_dollars_fp": [], "no_dollars_fp": []}})
    assert rates.ladder_presence_census["yes=EMPTY no=EMPTY"] == 1


def test_snapshot_and_ticker_spreads_are_never_pooled():
    """They are different depth classes (`full_ladder` vs `top_of_book_only`)."""
    rates = P4.RateRecorder()
    rates.observe({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": "X",
        "yes_dollars_fp": [["0.4700", "5.00"]],
        "no_dollars_fp": [["0.5100", "206.00"]]}})
    rates.observe({"type": "ticker", "sid": 2, "msg": {
        "market_ticker": "X", "yes_bid": "0.10", "yes_ask": "0.90"}})

    assert len(rates.spread_samples_snapshot) == 1
    assert len(rates.spread_samples_ticker) == 1
    # The NO side is the YES ask with NO complement applied (P3 s5.1). A
    # complementing implementation would report 0.4700 - (1 - 0.5100) = -0.02.
    assert str(rates.spread_samples_snapshot[0]) == "0.0400"
    # Which field NAME supplied the quote is itself an observation (P3 s5.4).
    assert rates.venue_field_names["yes_bid/yes_ask"] == 1


def test_an_empty_spread_sample_set_is_a_typed_absence_not_a_zero():
    summary = P4._spread_summary([])
    assert summary["n"] == 0
    assert summary["state"].startswith("NOT_MEASURABLE:")
    assert "min" not in summary and "median" not in summary


def test_percentiles_below_the_sample_floor_are_typed_not_computed():
    """`p99` below 100 samples is the maximum wearing a percentile's name."""
    out = P4._percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
    assert out["p50"] == 3.0
    assert str(out["p95"]).startswith("NOT_MEASURABLE:below_min_samples")
    assert str(out["p99"]).startswith("NOT_MEASURABLE:below_min_samples")


# --- the boundary ---------------------------------------------------------------


def test_the_capture_script_reaches_no_order_or_portfolio_surface():
    """The structural guard covers `app/`; this script lives outside it.

    Over IDENTIFIERS, not raw text — the same choice the real guard makes and
    for the same reason. A grep over the source is defeated by a rename and
    tripped by the boundary docstring, which says the words *because* it is
    promising not to do them. A docstring must be structurally invisible here.
    """
    import ast

    source = (REPO / "scripts" / "kalshi_prod_capture_p4.py").read_text()
    tree = ast.parse(source)
    identifiers: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr.lower())
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            identifiers.add(node.name.lower())

    for banned in ("create_order", "place_order", "submit_order", "cancel_order",
                   "market_positions", "user_orders", "portfolio", "fills"):
        hits = [i for i in identifiers if banned in i]
        assert not hits, f"{banned!r} appears as an identifier: {hits}"
    # Anti-vacuity: the permitted surface must actually be present, or this
    # test passes against a file that does nothing.
    assert "ProductionVerifyingConnector" in source
    assert "KalshiWebsocketTransport" in source
