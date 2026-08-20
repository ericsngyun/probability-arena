"""REALIZED-FILL-CORPUS-001 — fixture provenance and drift detection.

Doctrine 9: fixtures are executable claims about external reality, and the
provenance block IS the drift detector — it is how we notice the live venue
moving away from the world our tests certify.

The Kalshi CP3 lesson is the one being guarded against: 368 green tests built
on fixtures that put every channel on one shared `sid`, internally consistent
and wrong, with the suite certifying the wrong behaviour rather than failing.

A finalized Solana transaction is immutable, so unlike a WS frame the CONTENT
cannot drift. What drifts is the RPC's REPRESENTATION of it, and that is
exactly what would change our decoder's input while every test stayed green.
Hence two hashes and a detector that can tell them apart.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.fills.provenance import (
    FIXTURE_SCHEMA_VERSION,
    MAINNET_GENESIS_HASH,
    canonical_subset,
    content_hash,
    detect_drift,
    load_fixture_set,
    semantic_hash,
    verify_offline,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "solana_fills"

#: Every hard case from the milestone §5 table that we CLAIM to cover. A case
#: that appears here but in no fixture is an unbacked claim.
REQUIRED_HARD_CASES = {
    "multi_hop_route",
    "naive_log_parse_is_wrong",
    "wrapped_sol",
    "ata_creation_and_closure_rent",
    "failed_transaction",
    "fee_payer_is_trade_party",
    "fee_payer_is_not_trade_party",
    "versioned_transaction_lookup_table",
    "legacy_transaction",
    "mev_tip",
    "reverted_tip_intent",
    "priority_fee_charged_on_requested_limit",
}


@pytest.fixture(scope="module")
def fixtures():
    return load_fixture_set(FIXTURE_DIR)


def test_every_fixture_satisfies_its_own_provenance_claim(fixtures):
    """Offline, no network. Fails on any local mutation of a pinned byte."""
    problems = verify_offline(fixtures)
    assert problems == [], "\n".join(problems)
    assert len(fixtures.entries) == 6


def test_provenance_records_how_and_when_each_fixture_was_retrieved(fixtures):
    """A fixture that cannot identify its empirical basis is synthetic test
    data, not venue truth."""
    for e in fixtures.entries:
        assert e.venue == "solana-mainnet-beta"
        assert e.chain_genesis_hash == MAINNET_GENESIS_HASH
        assert e.rpc_method == "getTransaction"
        assert e.rpc_encoding == "jsonParsed"
        assert e.rpc_commitment == "confirmed"
        assert e.rpc_max_supported_version == 0
        # tier 0 only (doctrine 17): a free public endpoint, never a paid one
        assert e.rpc_endpoint == "https://api.mainnet-beta.solana.com"
        assert e.retrieved_at
        assert "fetch_realized_fill_fixtures.py" in e.retrieved_by
        assert e.schema_version == FIXTURE_SCHEMA_VERSION
        assert len(e.content_sha256) == 64
        assert len(e.semantic_sha256) == 64
        assert e.selection_reason, f"{e.capture_id} has no stated reason"
        assert e.hard_cases, f"{e.capture_id} claims no hard case"
        assert e.slot > 0


def test_the_corpus_covers_every_hard_case_it_claims(fixtures):
    """Doctrine 4's guard shape: assert that the permitted thing EXISTS, or
    the guard is satisfied by a corpus in which nothing is covered."""
    covered = set()
    for e in fixtures.entries:
        covered.update(e.hard_cases)
    missing = REQUIRED_HARD_CASES - covered
    assert not missing, f"hard cases claimed but not covered: {sorted(missing)}"


def test_partial_fill_is_honestly_absent_from_the_corpus(fixtures):
    """A NEGATIVE claim, asserted so it cannot rot into a silent gap.

    A partial fill is defined against a QUOTE (`actual_input < quoted_input`),
    and a third-party transaction carries no quote we can see. So the partial-
    fill branch is exercised by a constructed linkage test, NOT by a real
    fixture, and this test exists so a future reader cannot mistake the
    corpus's silence for coverage."""
    covered = set()
    for e in fixtures.entries:
        covered.update(e.hard_cases)
    assert "partial_fill" not in covered


# ---------------------------------------------------------------------------
# the drift detector must be able to FIRE
# ---------------------------------------------------------------------------


def test_drift_detector_reports_no_drift_on_an_identical_payload(fixtures):
    entry = fixtures.entries[0]
    payload = fixtures.payload(entry)
    report = detect_drift(entry, copy.deepcopy(payload), payload)
    assert report.drifted is False
    assert report.live_semantic_sha256 == entry.semantic_sha256


def test_drift_detector_fires_when_the_semantic_surface_changes(fixtures):
    """POSITIVE CONTROL. Force the condition — mutate a field the decoder
    reads — and require the detector to become non-benign. A detector that
    never fires is indistinguishable from one that is disconnected."""
    entry = fixtures.entries[0]
    payload = fixtures.payload(entry)
    live = copy.deepcopy(payload)
    live["meta"]["fee"] = live["meta"]["fee"] + 1

    report = detect_drift(entry, live, payload)
    assert report.drifted is True
    assert "meta" in report.changed_fields
    assert "meta.fee" in report.changed_fields
    assert report.live_semantic_sha256 != entry.semantic_sha256


def test_drift_detector_ignores_a_field_the_decoder_never_reads(fixtures):
    """The other arm. An RPC that starts returning a new advisory field has
    not drifted in any sense that matters, and reporting it would train
    everyone to ignore the detector."""
    entry = fixtures.entries[0]
    payload = fixtures.payload(entry)
    live = copy.deepcopy(payload)
    live["meta"]["rewards"] = [{"pubkey": "x", "lamports": 1}]
    live["someNewAdvisoryField"] = {"anything": True}

    report = detect_drift(entry, live, payload)
    assert report.drifted is False


def test_a_failed_live_fetch_is_unknown_drift_not_absent_drift(fixtures):
    """Reporting a rate-limited RPC as "no drift" is the exact failure
    doctrine 7 targets: a plausible benign value from a broken path."""
    entry = fixtures.entries[0]
    report = detect_drift(entry, None, fixtures.payload(entry))
    assert report.drifted is False
    assert report.live_semantic_sha256 is None
    assert "UNKNOWN, not absent" in report.detail


def test_verify_offline_catches_a_mutated_byte(tmp_path, fixtures):
    """POSITIVE CONTROL for the content hash: copy the corpus, change one
    number, and require the verifier to fail."""
    import shutil

    dest = tmp_path / "solana_fills"
    shutil.copytree(FIXTURE_DIR, dest)

    target = dest / fixtures.entries[0].relative_path
    payload = json.loads(target.read_text())
    payload["meta"]["fee"] = 999_999
    target.write_text(json.dumps(payload, sort_keys=True, indent=1))

    problems = verify_offline(load_fixture_set(dest))
    assert problems
    assert any("content hash" in p for p in problems)
    assert any("semantic hash" in p for p in problems)


def test_verify_offline_catches_a_swapped_signature(tmp_path):
    """A fixture whose stored transaction is not the one its provenance
    claims. This is the Kalshi CP3 failure shape: the file is well-formed and
    every test built on it would be internally consistent and wrong."""
    import shutil

    dest = tmp_path / "solana_fills"
    shutil.copytree(FIXTURE_DIR, dest)

    manifest = json.loads((dest / "MANIFEST.json").read_text())
    manifest["fixtures"][0]["signature"] = "not-the-signature-in-the-file"
    (dest / "MANIFEST.json").write_text(json.dumps(manifest))

    problems = verify_offline(load_fixture_set(dest))
    assert any("does not match pinned" in p for p in problems)


def test_the_canonical_subset_contains_everything_the_decoder_reads(fixtures):
    """If a field the decoder reads is missing from the canonical subset, the
    drift detector is blind to a change in it — the worst possible failure for
    a detector, because it reports health."""
    subset = canonical_subset(fixtures.payload(fixtures.entries[0]))
    for key in ("slot", "blockTime", "signatures", "header", "accountKeys",
                "instructions", "meta"):
        assert key in subset
    for key in ("err", "fee", "preBalances", "postBalances",
                "preTokenBalances", "postTokenBalances",
                "computeUnitsConsumed", "loadedAddresses",
                "innerInstructions"):
        assert key in subset["meta"], key


def test_hashes_are_stable_across_key_order(fixtures):
    """The semantic hash must not move because a dict was built differently."""
    payload = fixtures.payload(fixtures.entries[0])
    reordered = json.loads(json.dumps(payload, sort_keys=True))
    assert semantic_hash(payload) == semantic_hash(reordered)


def test_content_hash_is_over_the_stored_bytes_not_the_parsed_object(fixtures):
    entry = fixtures.entries[0]
    raw = (FIXTURE_DIR / entry.relative_path).read_bytes()
    assert content_hash(raw) == entry.content_sha256
    assert content_hash(raw + b"\n") != entry.content_sha256


# ---------------------------------------------------------------------------
# gated live check — skipped by default (docs/TESTING_POLICY.md)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("REALIZED_FILL_LIVE_DRIFT"),
    reason="live RPC drift check: set REALIZED_FILL_LIVE_DRIFT=1 to run",
)
def test_live_drift_against_the_public_rpc(fixtures):
    """Re-fetch every pinned transaction and compare the semantic subset.

    Read-only, tier 0, and skipped by default so the suite never depends on a
    network. Run it deliberately to ask "has the RPC's representation of these
    immutable transactions moved?"."""
    import asyncio

    from app.adapters.solana_rpc import SolanaRpcAdapter

    adapter = SolanaRpcAdapter(timeout=30.0)

    async def run():
        reports = []
        for entry in fixtures.entries:
            live = await adapter.get_transaction(entry.signature)
            await asyncio.sleep(0.8)
            reports.append(
                detect_drift(
                    entry,
                    live if isinstance(live, dict) else None,
                    fixtures.payload(entry),
                )
            )
        return reports

    reports = asyncio.run(run())
    drifted = [r for r in reports if r.drifted]
    assert not drifted, "\n".join(
        f"{r.capture_id}: {r.changed_fields}" for r in drifted
    )
