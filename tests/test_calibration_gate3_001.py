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


# --------------------------------------------------------------------------
# 4. the AS-SHIPPED arithmetic, pinned against the code that produces it
# --------------------------------------------------------------------------

#: The figures the two derivation blocks state, and where each comes from in
#: the shipped code. Every one of these is EVALUATED below rather than
#: asserted as a literal, because the defect being fixed was a doc that argued
#: from arithmetic the code does not perform.
COLD_MS_PER_TOKEN = 0.1054
WARM_MS_PER_TOKEN = 0.0198
SLO_SECONDS = 2.0


def _shipped() -> dict:
    """What the shipped code ACTUALLY does with the calibrated seed. Imported
    live so that a change to `bias_multiplier`, to `next_adaptive_batch_size`
    or to the `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` default breaks the doc pin
    rather than silently making the docs wrong again."""
    from app.services.crypto_tape import (
        AdaptiveBatchCostEstimate,
        next_adaptive_batch_size,
    )
    seed = float(CHOSEN)
    est = AdaptiveBatchCostEstimate(seed)
    ceiling = Settings().crypto_tape_reconciler_batch_size
    unclamped = next_adaptive_batch_size(SLO_SECONDS, est)
    clamped = next_adaptive_batch_size(SLO_SECONDS, est, max_batch_size=ceiling)
    return {
        "conservative": est.conservative_estimate_seconds,
        "unclamped": unclamped,
        "ceiling": ceiling,
        "clamped": clamped,
        "unclamped_cold_s": unclamped * COLD_MS_PER_TOKEN,
        "clamped_cold_s": clamped * COLD_MS_PER_TOKEN,
        "unclamped_cold_pct": unclamped * COLD_MS_PER_TOKEN / SLO_SECONDS * 100,
        "clamped_cold_pct": clamped * COLD_MS_PER_TOKEN / SLO_SECONDS * 100,
        "clamped_warm_s": clamped * WARM_MS_PER_TOKEN,
        "margin": seed / COLD_MS_PER_TOKEN,
    }


def _flat(text: str) -> str:
    """Markdown emphasis and line wrapping are not semantics. A precondition
    that reads `\\`X\\` above **5**` across a line break is the same sentence as
    `X above 5`, and a pin that only matches one of those spellings is a pin
    on the formatter, not on the claim."""
    stripped = re.sub(r"(?m)^\s*>\s?", "", text)          # blockquote markers
    return re.sub(r"\s+", " ", stripped.replace("`", "").replace("*", ""))


def _both_derivation_blocks() -> list[tuple[str, str]]:
    return [
        ("the runbook",
         _derivation_block(_runbook_gate3_section(), where="the runbook")),
        ("the milestone doc",
         _derivation_block(MILESTONE.read_text(), where="the milestone doc")),
    ]


class TestTheAsShippedArithmeticIsWhatTheReaderMeetsFirst:
    """§4.3/§4.4 once stated 13 tokens / 1.37 s / 68% of the SLO and concluded
    "it loosens", and §4.5 corrected both further down. The conclusion was
    wrong about the shipped effect, which is no change at all, and the
    superseded figure was the one an operator would have acted on.

    THE SELECTOR IS THE SAME FENCE the value pin above uses, for the same
    reason: this branch has had FOUR iterations of a pin landing on prose or
    on the wrong fenced block. Nothing here is asserted as a literal that the
    doc could simply be edited to match — every figure is recomputed from the
    shipped code and compared against what the fenced block says."""

    def test_the_block_states_the_batch_size_the_code_actually_returns(self):
        """MUTATION: change `8` to `13` in either fenced block, or change
        `bias_multiplier` away from 1.5, and this fails."""
        s = _shipped()
        for where, block in _both_derivation_blocks():
            assert f"-> {s['unclamped']} tokens" in block, (
                f"{where}'s derivation block does not state the "
                f"{s['unclamped']}-token first batch that "
                f"`next_adaptive_batch_size({SLO_SECONDS}, {CHOSEN})` returns"
            )
            assert f"{s['conservative']:.3f} s/token" in block, (
                f"{where} does not state the biased estimate the code "
                f"computes ({s['conservative']:.3f} s/token)"
            )

    def test_the_block_states_the_clamped_first_batch_and_the_ceiling(self):
        """MUTATION: drop the `min(8, 5)` line, or change the shipped
        `CRYPTO_TAPE_RECONCILER_BATCH_SIZE` default, and this fails.

        The clamp is the whole shipped effect. A block that stops at the
        unclamped 8 describes behaviour the reconciler does not have."""
        s = _shipped()
        for where, block in _both_derivation_blocks():
            assert f"min({s['unclamped']}, {s['ceiling']})" in block, (
                f"{where} does not show the B11 clamp "
                f"min({s['unclamped']}, {s['ceiling']})"
            )
            assert f"-> {s['clamped']} tokens" in block, (
                f"{where} does not state the {s['clamped']}-token batch the "
                "code actually issues"
            )
            assert f"BATCH_SIZE = {s['ceiling']}" in block, (
                f"{where}'s block does not name the ceiling constant's value"
            )

    def test_the_block_states_the_holds_and_percentages_those_batches_produce(self):
        """MUTATION: restore `1.37 s` / `68%`, or perturb any percentage, and
        this fails. The percentages are the figures the margin caveat is
        anchored to, so they cannot be allowed to drift independently."""
        s = _shipped()
        for where, block in _both_derivation_blocks():
            for label, value in (
                ("unclamped hold", f"{s['unclamped_cold_s']:.3f} s"),
                ("clamped hold", f"{s['clamped_cold_s']:.3f} s"),
                ("unclamped pct", f"{s['unclamped_cold_pct']:.1f}%"),
                ("clamped pct", f"{s['clamped_cold_pct']:.1f}%"),
                ("clamped warm hold", f"{s['clamped_warm_s']:.3f} s"),
            ):
                assert value in block, (
                    f"{where}'s derivation block is missing the {label} "
                    f"{value} implied by the cold cost {COLD_MS_PER_TOKEN} "
                    "s/token"
                )

    def test_the_superseded_figures_are_not_left_standing_anywhere(self):
        """The stale-claim-beside-its-correction pattern, pinned negatively.
        `13` survives ONLY as budget arithmetic that is explicitly disclaimed;
        the hold and percentage it implied must be gone, not appended to.

        MUTATION: put `1.37 s (68% of the SLO)` back into either doc and this
        fails."""
        for name, path in (("runbook", RUNBOOK), ("milestone doc", MILESTONE)):
            text = path.read_text()
            for stale in ("1.37 s", "1.37 seconds", "68% of the", "0.26 s"):
                assert stale not in text, (
                    f"the superseded figure {stale!r} is back in the {name} — "
                    "it is the number an operator would act on, and the code "
                    "never produces it"
                )
            for head, _, tail in (text.partition("≈ 13"),):
                if tail:
                    assert "not a batch size" in tail[:200], (
                        f"the {name} states `≈ 13` without immediately saying "
                        "the code never issues it"
                    )

    def test_the_conclusion_is_that_the_shipped_effect_is_no_change(self):
        """The heading is the conclusion an operator skims. "It loosens; it
        does not tighten" was a plainly-titled conclusion that was wrong about
        the shipped effect.

        MUTATION: retitle either section back and this fails."""
        s = _shipped()
        for name, path in (("runbook", RUNBOOK), ("milestone doc", MILESTONE)):
            text = path.read_text()
            headings = [ln for ln in text.splitlines()
                        if ln.lstrip().startswith("#")
                        and "loosening is latent" in ln]
            assert headings, (
                f"the {name} has no heading stating that the effect on "
                "today's constants is no change and the loosening is latent"
            )
            assert all("NO CHANGE" in h for h in headings), (
                f"the {name}'s conclusion heading no longer says the shipped "
                f"effect is no change: {headings!r}"
            )
            assert f"first batch is {s['clamped']} tokens" in text, (
                f"the {name} does not state the shipped first batch in prose "
                "next to that heading"
            )

    def test_the_margin_caveat_is_anchored_to_the_as_shipped_batch(self):
        """MUTATION: re-anchor the caveat on "under 70% of the SLO" without
        the as-shipped percentages and this fails.

        The caveat exists to say what the margin buys. Anchored to a batch
        size the code never issues, it overstates the headroom by a factor of
        ~2.6."""
        s = _shipped()
        for name, path in (("runbook", RUNBOOK), ("milestone doc", MILESTONE)):
            text = path.read_text()
            head, _, tail = text.partition("margin is a judgement")
            assert tail, f"the margin caveat is gone from the {name}"
            caveat = tail[:900]
            assert f"{s['margin']:.3f}x" in caveat, (
                f"the {name}'s margin caveat does not state the exact margin "
                f"{s['margin']:.3f}x = {CHOSEN} / {COLD_MS_PER_TOKEN}"
            )
            assert f"{s['clamped_cold_pct']:.1f}%" in caveat, (
                f"the {name}'s margin caveat is not anchored to the "
                "as-shipped 5-token hold"
            )
            assert "superseded" in caveat, (
                f"the {name}'s margin caveat does not record that the old "
                "70%-of-SLO anchor is superseded"
            )


# --------------------------------------------------------------------------
# 5. the two preconditions Gate 4 asked to be made explicit
# --------------------------------------------------------------------------

class TestTheTransferredCalibrationRiskIsAHardPrecondition:
    """`0.15` is inert today ONLY because `min(8, 5) = 5`. The risk did not
    resolve, it transferred to whichever milestone raises the B11 ceiling.
    Left implicit in a clamp bullet, it is invisible at the moment it
    matters."""

    def test_raising_the_ceiling_requires_re_deriving_the_seed(self):
        """MUTATION: delete the precondition from either doc and this fails.

        Selected on "HARD PRECONDITION for Gate 6" and not on "HARD
        PRECONDITION" alone: the Gate 3 section carries a second, unrelated
        hard precondition (no partial batch), and a selector that matched
        either would let this one be deleted while the test still passed."""
        for name, text in (
            ("runbook", _runbook_gate3_section()),
            ("milestone doc", MILESTONE.read_text()),
        ):
            head, _, tail = text.partition("HARD PRECONDITION for Gate 6")
            assert tail, (
                f"the {name} does not state a HARD PRECONDITION on raising "
                "the B11 ceiling"
            )
            window = _flat(tail[:1200])
            assert "CRYPTO_TAPE_RECONCILER_BATCH_SIZE above 5" in window, (
                f"the {name}'s Gate 6 precondition does not name the ceiling "
                "and the value it may not be raised past"
            )
            assert "re-derive" in window or "re-derived" in window, (
                f"the {name}'s precondition does not require the seed to be "
                "re-derived"
            )

    def test_the_enable_checklist_carries_it_too_not_only_the_derivation(self):
        """An operator enabling the timer reads the precondition list, not the
        derivation. MUTATION: remove the pointer from precondition 3 and this
        fails."""
        text = RUNBOOK.read_text()
        head, _, tail = text.partition("### Recurring-timer preconditions")
        assert tail, "the recurring-timer precondition list is gone"
        checklist = _flat(tail.split("### Where the per-pass record now lands")[0])
        assert "CRYPTO_TAPE_RECONCILER_BATCH_SIZE above 5" in checklist, (
            "the enable-time checklist does not say that raising the batch "
            "ceiling invalidates the calibrated seed"
        )
        assert "re-deriving the seed" in checklist


class TestThePartialBatchPreconditionIsRecorded:
    """`write_hold_ms_max / batch_size` is a per-token cost ONLY because no
    batch was partial. A max-over-batches numerator over a fixed denominator
    UNDER-shoots — the unsafe direction — if any batch is short."""

    @pytest.mark.parametrize("required", [
        # the check itself, in executable form
        "batches_committed x batch_size == tokens_considered",
        # the failure direction, named
        "UNDER-estimate",
        # the ordinary way a short batch arrives
        "deadline-stopped",
    ])
    def test_both_docs_record_the_check_and_its_failure_direction(self, required):
        """MUTATION: drop the failure direction and keep the check, and this
        fails — a check whose direction is unstated reads as bookkeeping."""
        for name, text in (
            ("runbook", RUNBOOK.read_text()),
            ("milestone doc", MILESTONE.read_text()),
        ):
            assert required in text, (
                f"{required!r} is missing from the {name}'s re-calibration "
                "precondition"
            )

    def test_it_is_stated_as_a_precondition_not_as_a_session_note(self):
        """The reviewer's point exactly: recorded as a note about the Gate 3
        session, it does not bind the NEXT session, which is the one that can
        get it wrong."""
        for name, text in (
            ("runbook", RUNBOOK.read_text()),
            ("milestone doc", MILESTONE.read_text()),
        ):
            head, _, tail = text.partition(
                "batches_committed x batch_size == tokens_considered")
            window = head[-600:] + tail[:600]
            assert "PRECONDITION" in window.upper(), (
                f"the {name} states the identity without calling it a "
                "precondition for future re-calibration"
            )


# --------------------------------------------------------------------------
# 6. the named follow-ups — recorded, deliberately NOT built
# --------------------------------------------------------------------------

class TestTheNamedFollowUpsSurvive:

    def test_the_lock_tally_time_window_gap_is_named_with_its_measurement(self):
        """The scoping fixed WHICH population, not OVER WHAT INTERVAL. A
        follow-up without its measurement is a wish; the measurement is what
        makes it actionable and what shows the `> 6` gate ages out on its own.

        MUTATION: delete the measurement and leave the prose, and this
        fails."""
        for name, path in (("runbook", RUNBOOK), ("milestone doc", MILESTONE)):
            text = path.read_text()
            head, _, tail = text.partition("11 in-scope events")
            assert tail, (
                f"the {name} does not carry the measurement showing the "
                "cumulative count crosses `> 6` on benign traffic"
            )
            window = head[-700:] + tail[:400]
            assert "90 days" in window and "0.5%" in window, (
                f"the {name} states the 11 events without the traffic and "
                "contention rate that produce them"
            )
            assert "> 6" in window
            assert "--since" in window or "lock_events_last_24h" in window, (
                f"the {name} names the gap without naming the fix"
            )

    def test_the_follow_up_is_named_as_not_built_here(self):
        text = MILESTONE.read_text()
        assert "NOT built by this gate" in text or "not built here" in text.lower()

    def test_run_source_forgery_is_recorded_as_inert_only_for_now(self):
        """Inert because nothing reads it back — a property of today's
        consumers, not of the field. MUTATION: drop the conditional and this
        fails."""
        text = MILESTONE.read_text()
        head, _, tail = text.partition("run_source")
        assert tail, "the run_source note is gone from the milestone doc"
        assert "no consumer reads it back" in text, (
            "the milestone doc no longer says WHY the forgery is inert"
        )
        assert "first reader" in text, (
            "the milestone doc does not say who inherits the enforcement "
            "question"
        )

    def test_nothing_here_reads_run_source_back_from_the_sink(self):
        """THE EXECUTABLE HALF of the claim above. The note is only true while
        the grep is true, so the grep runs.

        MUTATION: add a consumer that reads `run_source` off a parsed event
        outside the emit path and this fails, which is exactly the moment the
        doc note stops being accurate."""
        emit_path = {
            REPO / "app" / "telemetry" / "writer_pass.py",
            REPO / "app" / "telemetry" / "schema.py",
        }
        offenders = []
        for root in ("app", "scripts"):
            for py in (REPO / root).rglob("*.py"):
                if py in emit_path:
                    continue
                for i, line in enumerate(py.read_text().splitlines(), 1):
                    if "run_source" not in line:
                        continue
                    if line.lstrip().startswith("#"):
                        continue
                    offenders.append(f"{py.relative_to(REPO)}:{i}: {line.strip()}")
        assert not offenders, (
            "something now reads `run_source` outside the emit path — the "
            "milestone doc's 'no consumer reads it back' note is stale and "
            "the forgery is no longer inert:\n" + "\n".join(offenders)
        )
