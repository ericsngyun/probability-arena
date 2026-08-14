"""CALIBRATION-GATE3-001 — pins the Gate 3 calibration of
`initial_per_token_cost_seconds` for CRYPTO-RECONCILER-GUARDED-TIMER-001.

Two things are pinned here, and they are different in kind.

1. **THE REJECTION BEHAVIOUR** (`0`, `-1`, `abc`, `999`), verified live on
   EVO-X2 during the calibration session. These are runnable proofs that the
   constant has a validated domain on BOTH sides: a lower bound that refuses a
   non-positive seed instead of defaulting one, an upper bound that stops the
   pass instead of guessing a sub-1-token batch, and type validation that fires
   before a pass runs at all. Each was mutation-tested — remove the lower
   bound, remove the upper bound, or make the invalid path return `ok`, and a
   named test below fails.

2. **THE CHOSEN VALUE ITSELF (0.15)**, so it cannot drift silently between the
   three artifacts that carry it: `.env.example`, the runbook derivation, and
   the milestone doc. THE SELECTOR IS STRUCTURAL — a fenced block, not a token
   that happens to appear in prose. This branch has already had three
   iterations of a pin landing on prose or on the wrong fenced block (see
   `TestRunbookIsActionable._runnable_filter_block` in
   `tests/test_gate2_writer_telemetry_001.py`), and the lesson taken from those
   is applied here: select by fence, then require the artifacts to AGREE, so
   that editing one of them alone is a failure rather than a silent divergence.

Gate 3 derives the constant. **Gate 6 activates it.** The last test in this
file pins that separation: the value is set nowhere that takes effect.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import CryptoPriceTick, CryptoToken
from app.services.crypto_tape import (
    STATUS_UNSAFE_HOST_COST,
    run_scheduled_reconciliation,
)

REPO = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO / ".env.example"
RUNBOOK = REPO / "docs" / "EVO_X2_RUNBOOK.md"
MILESTONE = REPO / "docs" / "CRYPTO_RECONCILER_GUARDED_TIMER_001.md"

VAR = "CRYPTO_TAPE_RECONCILER_INITIAL_PER_TOKEN_COST_SECONDS"

#: The constant this whole milestone gate exists to produce. Changing it here
#: is not enough to make these tests pass — all three artifacts must change
#: together, which is the point.
CHOSEN = "0.15"

#: The eight per-token samples (ms) the constant was derived from, sorted.
#: These are MEASUREMENTS taken on EVO-X2 and are not re-derivable from this
#: repository; a test that lets them be edited is not a pin.
SAMPLES_MS = ("14.8", "17.8", "18.0", "18.8", "18.8", "19.6", "19.8", "105.4")

CHAIN = "solana"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _mint(session, address: str, *, born_hours_ago: float = 30.0) -> None:
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(hours=born_hours_ago)
    session.add(CryptoToken(
        chain=CHAIN, token_address=address, symbol=address[:6],
        first_seen_at=first_seen, last_seen_at=now,
    ))
    session.add(CryptoPriceTick(
        chain=CHAIN, token_address=address, pair_address=f"pair-{address}",
        observed_at=first_seen, price_usd=1.0, liquidity_usd=10_000.0,
        volume_24h_usd=5_000.0,
    ))
    session.flush()


def _settings(**over) -> Settings:
    base = dict(
        enable_crypto_tape_reconciler=True,
        crypto_tape_reconciler_window_hours=48,
        crypto_tape_reconciler_limit=1000,
    )
    base.update(over)
    return Settings(**base)


# --------------------------------------------------------------------------
# 1. the rejection behaviour, verified live on EVO-X2
# --------------------------------------------------------------------------

class TestTheSeedHasAValidatedDomain:
    """The four inputs exercised on EVO during the calibration session. Each
    is a boundary of the domain, not an arbitrary bad value."""

    @pytest.mark.parametrize("bad", [0, 0.0, -1, -1.0])
    def test_a_non_positive_seed_is_refused_never_defaulted(self, session, bad):
        """LOWER BOUND. MUTATION: delete the
        `resolved_initial_cost <= 0` refusal in `run_scheduled_reconciliation`
        and this fails.

        `0` is the dangerous one and the reason there is no built-in default:
        a zero per-token cost predicts an infinitely cheap token, which sizes
        the first batch at the B11 ceiling with no evidence at all behind it."""
        r = run_scheduled_reconciliation(
            session, settings=_settings(), initial_per_token_cost_seconds=bad,
        )
        assert r["status"] == "invalid_initial_per_token_cost_seconds", (
            f"seed {bad!r} was accepted (status={r['status']!r}) — the lower "
            "bound on the UNCALIBRATED seed is gone"
        )

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_the_refused_seed_path_never_reports_ok_and_writes_nothing(
        self, session, bad
    ):
        """MUTATION: make the invalid path return `ok` and this fails.

        Kept separate from the status assertion above on purpose. A refusal
        that reports success is worse than a crash: the guarded timer's health
        gate classifies statuses, and an `ok` here would mean a pass that
        never validated its seed is counted as a clean pass."""
        for i in range(6):
            _mint(session, f"tok-refuse-{bad}-{i}")
        r = run_scheduled_reconciliation(
            session, settings=_settings(), initial_per_token_cost_seconds=bad,
        )
        assert r["status"] != "ok", (
            "a refused seed reported ok — a pass that never validated its "
            "seed would be counted as clean by the health gate"
        )
        assert r.get("tokens_processed", 0) == 0, (
            "a refused seed still processed tokens — the refusal is not "
            "stopping the pass"
        )
        assert r.get("error"), "a refusal must say why"

    def test_a_non_numeric_seed_fails_validation_before_any_pass_runs(self):
        """`abc` never reaches the reconciler at all: pydantic rejects it
        while `Settings` is being constructed, which is what makes a
        typo in `.env` a startup failure rather than a silently inert flag."""
        with pytest.raises(Exception) as exc:
            _settings(crypto_tape_reconciler_initial_per_token_cost_seconds="abc")
        assert "ValidationError" in type(exc.value).__name__, (
            f"expected a pydantic ValidationError, got {type(exc.value)!r}"
        )

    def test_an_absurd_seed_stops_the_pass_rather_than_guessing(self, session):
        """UPPER BOUND. MUTATION: delete the
        `conservative > time_budget_seconds` guard in
        `next_adaptive_batch_size` and this fails.

        The bound is not a second constant: when even a one-token transaction
        is predicted to blow the write-time budget, the sizer returns 0 and
        the pass takes the typed terminal status. The forbidden alternative is
        a silent floor at one token, which would hold the write lock for the
        whole predicted cost on a host that just said it could not afford it.

        Exercised through `Settings`, not a caller kwarg, because that is the
        path an operator's `.env` actually takes."""
        for i in range(6):
            _mint(session, f"tok-unsafe-{i}")
        r = run_scheduled_reconciliation(
            session,
            settings=_settings(
                crypto_tape_reconciler_initial_per_token_cost_seconds=999.0,
                crypto_tape_reconciler_time_budget_seconds=2.0,
            ),
            sleeper=lambda _s: None,
        )
        assert r["status"] == STATUS_UNSAFE_HOST_COST, (
            f"a 999 s/token seed was accepted (status={r['status']!r}) — the "
            "upper bound is gone and a single-token batch would now be issued"
        )
        assert r["stop_reason"] == "unsafe_host_cost"
        assert r["tokens_processed"] == 0
        assert r["error"]

    def test_the_two_bounds_are_distinct_outcomes(self, session):
        """A single collapsed refusal would lose the operator-visible
        difference between "you configured nonsense" and "this host cannot
        afford the work"."""
        for i in range(6):
            _mint(session, f"tok-distinct-{i}")
        low = run_scheduled_reconciliation(
            session, settings=_settings(), initial_per_token_cost_seconds=0.0,
        )
        high = run_scheduled_reconciliation(
            session,
            settings=_settings(
                crypto_tape_reconciler_initial_per_token_cost_seconds=999.0,
                crypto_tape_reconciler_time_budget_seconds=2.0,
            ),
            sleeper=lambda _s: None,
        )
        assert low["status"] != high["status"]


# --------------------------------------------------------------------------
# 2. the chosen value, pinned structurally across all three artifacts
# --------------------------------------------------------------------------

def _fenced_blocks(text: str) -> list[str]:
    """Only the segments INSIDE a fence. `split("```")` alternates
    outside/inside, so the odd indices are the code blocks. Selecting on the
    whole text would let surrounding prose masquerade as a block — the exact
    failure class this file's docstring records."""
    parts = text.split("```")
    assert len(parts) % 2 == 1, "unbalanced ``` fences"
    return parts[1::2]


def _runbook_gate3_section() -> str:
    text = RUNBOOK.read_text()
    assert "#### Gate 3 — the calibration session and the chosen constant" in text, (
        "the Gate 3 derivation subsection is gone from the runbook — the "
        "constant below now has no operator-facing justification"
    )
    return (text
            .split("#### Gate 3 — the calibration session and the chosen constant")[1]
            .split("#### Growth, rotation and reading this file")[0])


def _derivation_block(section: str, *, where: str) -> str:
    """THE BLOCK THAT STATES THE CONSTANT, selected by its fence and
    cross-checked by the literal parameter name. A fence is a structural
    property of the artifact; a name in prose is not."""
    blocks = [b for b in _fenced_blocks(section)
              if b.lstrip().startswith("text")
              and "initial_per_token_cost_seconds =" in b]
    assert len(blocks) == 1, (
        f"expected exactly one fenced derivation block in {where}, found "
        f"{len(blocks)} — the pin has lost its anchor"
    )
    return blocks[0]


def _stated_value(block: str) -> str:
    m = re.search(r"initial_per_token_cost_seconds\s*=\s*([0-9]*\.?[0-9]+)", block)
    assert m, f"no `initial_per_token_cost_seconds = <number>` in:\n{block}"
    return m.group(1)


def _env_example_value() -> tuple[str, bool]:
    """Returns (value, is_commented_out) for the .env.example assignment."""
    pattern = re.compile(
        rf"^(?P<hash>#\s*)?{VAR}\s*=\s*(?P<value>\S*)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(ENV_EXAMPLE.read_text()))
    assert len(matches) == 1, (
        f"expected exactly one {VAR} assignment in .env.example, found "
        f"{len(matches)}"
    )
    m = matches[0]
    return m.group("value"), bool(m.group("hash"))


class TestTheChosenValueCannotDriftSilently:

    def test_env_example_carries_the_calibrated_value(self):
        value, _ = _env_example_value()
        assert value == CHOSEN, (
            f".env.example carries {value!r}, not the calibrated {CHOSEN!r}. "
            "The constant governs a production writer's write-lock hold; it "
            "does not get edited without re-deriving it."
        )

    def test_the_runbook_derivation_names_the_same_number(self):
        block = _derivation_block(_runbook_gate3_section(), where="the runbook")
        assert _stated_value(block) == CHOSEN

    def test_the_milestone_doc_derivation_names_the_same_number(self):
        block = _derivation_block(MILESTONE.read_text(), where="the milestone doc")
        assert _stated_value(block) == CHOSEN

    def test_all_three_artifacts_agree(self):
        """THE PIN THAT MAKES THE PINS ABOVE HOLD. Three artifacts carry this
        number; pinning each against a constant in this file still lets a
        four-way edit pass. Requiring them to agree with EACH OTHER means a
        change to any one of them, alone, is a failure."""
        env_value, _ = _env_example_value()
        runbook_value = _stated_value(
            _derivation_block(_runbook_gate3_section(), where="the runbook"))
        milestone_value = _stated_value(
            _derivation_block(MILESTONE.read_text(), where="the milestone doc"))
        assert env_value == runbook_value == milestone_value, (
            f".env.example={env_value!r} runbook={runbook_value!r} "
            f"milestone={milestone_value!r} — the operator's file and the "
            "evidence for it have diverged"
        )

    def test_the_eight_measurements_are_recorded_in_both_docs(self):
        """The samples are MEASUREMENTS taken on EVO-X2 and cannot be
        re-derived from this repository. A derivation that keeps the
        conclusion but loses its inputs is not an evidentiary record."""
        for name, block in (
            ("runbook", _derivation_block(_runbook_gate3_section(),
                                          where="the runbook")),
            ("milestone doc", _derivation_block(MILESTONE.read_text(),
                                                where="the milestone doc")),
        ):
            for sample in SAMPLES_MS:
                assert sample in block, (
                    f"per-token sample {sample} ms is missing from the "
                    f"{name} derivation block"
                )

    @pytest.mark.parametrize("phrase", [
        # the cold sample is n=1 ...
        "n = 1",
        # ... and this is the CLAIM that caveat makes. Pinned separately
        # because the count alone survives being restated in a table while
        # the qualification it exists to carry is deleted — a mutation that
        # removed the caveat prose and left `(n = 1)` in a fence passed an
        # earlier version of this test.
        "not bounded by this data",
        # the cold sample must not be re-read as an outlier and dropped
        "outlier",
        # the margin is a judgement, not a measurement
        "1.42",
        # the denominator actually used, as the filter subsection requires
        "batch_size",
        # rows_committed is a three-table row count, not a token count
        "rows_committed",
        # pass 4's deadline overshoot, which max_duration_seconds cannot stop
        "41035",
        # passes 6-8: the backlog has drained to steady state
        "590",
        # the SLO the whole derivation is against
        "2.0 s",
        # the ceiling that currently clamps the calibrated first batch back
        # to today's fixed behaviour — the reason the loosening is latent
        "B11",
    ])
    def test_the_caveats_survive_in_both_docs(self, phrase):
        """These are load-bearing qualifications, not decoration. Each was
        explicitly required to appear in the record rather than be smoothed
        away, and prose is exactly what erodes between milestones."""
        assert phrase in _runbook_gate3_section(), (
            f"{phrase!r} is gone from the runbook Gate 3 derivation"
        )
        assert phrase in MILESTONE.read_text(), (
            f"{phrase!r} is gone from the milestone doc"
        )

    def test_the_cold_start_is_named_as_the_governing_observation(self):
        """The derivation's whole load-bearing claim. If a later editor
        re-reads the eight samples as "median 18.8" and drops the cold case as
        an outlier, the constant becomes ~5x too aggressive at exactly the
        moment it is used."""
        section = _runbook_gate3_section()
        assert "105.4" in section
        assert "cold" in section.lower()


# --------------------------------------------------------------------------
# 3. Gate 3 derives; Gate 6 activates
# --------------------------------------------------------------------------

class TestNothingIsActivated:

    def test_the_env_example_line_is_still_commented_out(self):
        """Gate 3 records the value; Gate 6 turns it on. An uncommented line
        here would be inherited by every fresh `cp .env.example .env`."""
        _, commented = _env_example_value()
        assert commented, (
            f"{VAR} is no longer commented out in .env.example — that is "
            "Gate 6's decision, not Gate 3's"
        )

    def test_the_shipped_settings_default_is_still_unset(self):
        """The seed has no built-in default by design: a fixed token count is
        not a safety invariant, and an invented default would silently make
        one up on an unmeasured host."""
        s = Settings()
        assert s.crypto_tape_reconciler_initial_per_token_cost_seconds is None
        assert s.crypto_tape_reconciler_time_budget_seconds is None

    def test_the_docs_say_activation_is_a_separate_gate(self):
        assert "Gate 6" in _runbook_gate3_section()
        assert "Gate 6" in MILESTONE.read_text()
