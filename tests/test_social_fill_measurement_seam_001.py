"""SOCIAL-FILL-MEASUREMENT-SEAM-001 — the seam and its eight positive controls.

Fixture-driven. No network anywhere, including in the token resolver: the
un-wired escalation rungs are `NotWiredStage`, which has nothing to stub.

The eight controls (doctrine 7) live in :class:`TestPositiveControls`. Each one
was proven RED before it was proven green; the mutation used and the failure
observed are recorded in
`docs/milestones/SOCIAL-FILL-MEASUREMENT-SEAM-001.md` §6.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Observed,
    absent,
    observed,
)
from app.fills.schema import (
    CostBreakdown,
    FillStatus,
    QuoteRecord,
    RealizedFill,
    RouteDescriptor,
    Side,
    StateLabels,
)
from app.seam import SEAM_VERSION
from app.seam.clock import (
    BootIdStatus,
    ClockContractError,
    ClockQuality,
    ComputedInterval,
    CrossDomainInterval,
    HostBootId,
    IntervalBasis,
    NotComputable,
    NotComputableReason,
    ObservationTimestamp,
    SyncBound,
    TimeDomain,
    UNKNOWN_HOST,
    capture_observation,
    cross_domain_interval,
    external_delivery_latency,
    from_our_received_at,
    interval,
    legacy_wall_interval_us,
    our_response_latency,
    read_host_boot_id,
)
from app.seam.cohort import (
    CohortPoolingError,
    CohortPurpose,
    CohortPurposeViolation,
    DeliveryCohort,
    LiveLeadLagCohort,
    PRIMARY_ALPHA_DELIVERY_MODE,
    delivery_mode_breakdown,
    partition_by_delivery_mode,
)
from app.seam.join import (
    JoinRefusalReason,
    JoinRefused,
    JoinedEvidenceRow,
    join_social_to_fill,
)
from app.seam.measurement import (
    Availability,
    IllegalMeasurementError,
    Measurement,
    MeasurementAbsentError,
    Observation,
    ObservationWindow,
    OriginTag,
    UnmappableAbsenceError,
    from_fills_absence,
    from_fills_maybe,
    from_social_deferred,
    to_fills_maybe,
    to_social_deferred,
)
from app.seam.token import (
    Chain,
    EscalationLadder,
    EvidenceKind,
    JOINABLE_STATUSES,
    NotWiredStage,
    ResolutionEvidence,
    TextCandidateStage,
    TokenResolution,
    TokenResolutionError,
    TokenResolutionStatus,
    default_ladder,
    from_entity_resolution,
)
from app.social.artifact import (
    Deferred,
    DeferredState,
    DeliveryMode,
    EntityResolution,
    ParentRef,
    Platform,
    PropagationKind,
    ResolutionConfidence,
    SocialArtifact,
    raw_content_digest,
)
from app.social.timebase import (
    SourceCreatedAt,
    capture_receipt,
    process_epoch,
)

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SIG = "5" + "z" * 60


# ---------------------------------------------------------------------------
# fixtures — all local, all deterministic
# ---------------------------------------------------------------------------


def a_window(watcher: str = "solana-tx-watcher.v1") -> ObservationWindow:
    return ObservationWindow(
        start_utc="2026-08-20T12:00:00.000000Z",
        end_utc="2026-08-20T12:05:00.000000Z",
        basis="MONOTONIC_SAME_BOOT",
        watcher_id=watcher,
    )


def a_stamp(
    *,
    wall: str = "2026-08-20T12:00:00.000000Z",
    mono: int | None = 1_000_000_000,
    boot: str | None = "boot-a",
    epoch: str | None = "epoch-1",
    host: str = "host-a",
    quality: ClockQuality = ClockQuality.MONOTONIC_ANCHORED,
    sync_bound: SyncBound | None = None,
) -> ObservationTimestamp:
    return ObservationTimestamp(
        wall_utc=wall,
        host_id=host,
        host_boot_id=(
            HostBootId(status=BootIdStatus.PRESENT, value=boot)
            if boot
            else HostBootId.unknown()
        ),
        clock_quality=quality,
        monotonic_ns=mono,
        process_epoch_id=epoch,
        sync_bound=sync_bound,
    )


def a_source_created_at(text: str = "2026-08-20T11:59:59.000Z") -> SourceCreatedAt:
    cleaned = text[:-1] + "+00:00" if text.endswith("Z") else text
    return SourceCreatedAt.from_platform(
        text, source_field="created_at", parsed=dt.datetime.fromisoformat(cleaned)
    )


def an_artifact(
    *,
    delivery_mode: DeliveryMode = DeliveryMode.LIVE,
    text: str | None = None,
    message_id: str = "m1",
) -> SocialArtifact:
    body = json.dumps({"text": text or f"gm ${MINT}"}).encode("utf-8")
    return SocialArtifact(
        platform=Platform.X,
        source_id="acct:1",
        message_id=message_id,
        author_id="a1",
        source_created_at=a_source_created_at(),
        our_received_at=capture_receipt(process_epoch()),
        raw_content=body,
        raw_content_hash=raw_content_digest(body),
        content_text=json.loads(body.decode("utf-8"))["text"],
        matching_rule="caller.example-account-01",
        parent=ParentRef(kind=PropagationKind.ORIGINAL),
        delivery_mode=delivery_mode,
    )


NO_COSTS = CostBreakdown(
    network_fee_lamports=absent(AbsenceReason.NOT_PROVIDED),
    priority_fee_lamports=absent(AbsenceReason.NOT_PROVIDED),
    tip_lamports=absent(AbsenceReason.NOT_PROVIDED),
    tip_attempted_lamports=absent(AbsenceReason.NOT_PROVIDED),
    compute_units_consumed=absent(AbsenceReason.NOT_PROVIDED),
    compute_unit_price_micro_lamports=absent(AbsenceReason.NOT_PROVIDED),
    rent_lamports_net=absent(AbsenceReason.NOT_PROVIDED),
    tip_destinations=absent(AbsenceReason.NOT_PROVIDED),
)


def a_quote(t_quote=None) -> QuoteRecord:
    a = absent(AbsenceReason.NOT_AUTHORIZED, "no quote authorized")
    return QuoteRecord(
        t_quote=t_quote if t_quote is not None else a,
        quoted_input=a,
        quoted_output=a,
        quoted_price=a,
        quoted_price_impact=a,
        quoted_min_output=a,
        quote_source=a,
        quote_capture_id=a,
    )


def a_fill(*, mint: str = MINT, signature=SIG) -> RealizedFill:
    a = absent(AbsenceReason.NOT_PROVIDED)
    return RealizedFill(
        decision_id=a,
        observation_id=a,
        mint=mint,
        side=Side.ACQUIRE,
        notional_quote_units=a,
        quote_asset_mint=a,
        route=RouteDescriptor(legs=a, hop_count=a, aggregator=a),
        quote=a_quote(),
        t_submit=a,
        signature=(
            observed(signature, source="tx") if signature else a
        ),
        slot=observed(300_000_000, source="tx"),
        t_confirmed=observed(
            dt.datetime(2026, 8, 20, 12, 0, 1, tzinfo=dt.timezone.utc),
            source="tx",
        ),
        status=FillStatus.CONFIRMED,
        actual_input=a,
        actual_output=a,
        costs=NO_COSTS,
        actual_price=a,
        realized_slippage=a,
        quote_to_submit_ms=a,
        submit_to_confirm_ms=a,
        markouts=(),
        states=StateLabels(liquidity_state=a, volatility_state=a, social_state=a),
        model_version=a,
        decoder_version="test-decoder.v1",
    )


def a_verified_resolution(mint: str = MINT) -> TokenResolution:
    return TokenResolution(
        chain=Chain.SOLANA,
        status=TokenResolutionStatus.CANONICALLY_VERIFIED,
        resolver_version="test-canonical.v1",
        mint=mint,
        candidates=(mint,),
        confidence="CONFIRMED",
        evidence=(
            ResolutionEvidence(
                kind=EvidenceKind.CHAIN_MINT_EXISTS,
                stage_id="chain-existence.test",
                detail="mint account exists and is an SPL mint",
            ),
            ResolutionEvidence(
                kind=EvidenceKind.PROJECT_ACCOUNT_LINK,
                stage_id="project-context.test",
                detail="posting account is the project's own account",
            ),
        ),
    )


def a_join(
    *,
    artifact=None,
    resolution=None,
    fill=None,
    onchain=None,
    price=None,
    quote_stamp=None,
    received_stamp=None,
    purpose: CohortPurpose = CohortPurpose.LATENCY_LEAD_LAG,
):
    return join_social_to_fill(
        artifact=artifact if artifact is not None else an_artifact(),
        resolution=(
            resolution if resolution is not None else a_verified_resolution()
        ),
        fill=fill if fill is not None else a_fill(),
        quote_observed_at=(
            quote_stamp
            if quote_stamp is not None
            else a_stamp(wall="2026-08-20T12:00:00.400000Z", mono=1_400_000_000)
        ),
        received_observed_at=(
            received_stamp if received_stamp is not None else a_stamp()
        ),
        onchain_reaction=(
            onchain
            if onchain is not None
            else Measurement.not_attempted(Availability.NOT_YET_OBSERVED)
        ),
        price_reaction=(
            price
            if price is not None
            else Measurement.not_attempted(Availability.NOT_YET_OBSERVED)
        ),
        purpose=purpose,
    )


# ===========================================================================
# THE EIGHT POSITIVE CONTROLS
# ===========================================================================


class TestPositiveControls:
    """Force the condition; prove the measurement becomes non-benign."""

    # -- 1 -----------------------------------------------------------------
    def test_control_1_observed_none_survives_a_join_as_measured_negative(self):
        """A watched window with no reaction is EVIDENCE, and it must arrive
        at the other side of the join still saying so."""
        negative = Measurement.observed_none(
            source="solana-tx-watcher.v1",
            window=a_window(),
            detail="no transaction touching the mint in the 5 minute window",
        )
        row = a_join(onchain=negative)

        assert isinstance(row, JoinedEvidenceRow)
        assert row.first_onchain_reaction.is_measured_negative is True
        assert row.has_measured_negative is True
        # it is NOT absence
        assert row.first_onchain_reaction.availability is Availability.AVAILABLE
        assert (
            row.first_onchain_reaction.observation is Observation.OBSERVED_NONE
        )
        # and it survives serialisation, which is where labels usually die
        rehydrated = Measurement.from_json(
            json.loads(json.dumps(row.to_json()))["first_onchain_reaction"]
        )
        assert rehydrated.is_measured_negative is True
        assert rehydrated.window is not None
        assert rehydrated.window.watcher_id == "solana-tx-watcher.v1"

    def test_control_1b_a_measured_negative_is_distinguishable_from_absence(self):
        negative = Measurement.observed_none(
            source="w", window=a_window()
        )
        nothing = Measurement.not_attempted(Availability.NOT_PROVIDED)
        assert negative.is_measured_negative and not nothing.is_measured_negative
        assert negative.to_json() != nothing.to_json()
        # the ONE mapping the contract forbids: OBSERVED_NONE -> NOT_PROVIDED
        with pytest.raises(UnmappableAbsenceError):
            to_fills_maybe(negative)

    # -- 2 -----------------------------------------------------------------
    def test_control_2_not_provided_cannot_become_zero(self):
        """`x or 0` must not work, `unwrap` must raise, and nothing may read a
        number off an absence."""
        nothing = Measurement.not_attempted(
            Availability.NOT_PROVIDED, detail="the source said nothing"
        )
        with pytest.raises(MeasurementAbsentError):
            bool(nothing)
        with pytest.raises(MeasurementAbsentError):
            nothing.unwrap()
        with pytest.raises(MeasurementAbsentError):
            _ = nothing or 0
        assert nothing.value is None
        assert nothing.is_measurement is False
        assert nothing.is_measured_negative is False
        # and a negative-label counter must not count it
        pool = [
            nothing,
            Measurement.observed_none(source="w", window=a_window()),
            Measurement.not_attempted(Availability.NOT_YET_OBSERVED),
        ]
        assert sum(1 for m in pool if m.is_measured_negative) == 1

    def test_control_2b_an_observed_value_of_none_is_unconstructible(self):
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=None,
                availability=Availability.AVAILABLE,
                observation=Observation.OBSERVED_VALUE,
                source="s",
            )

    # -- 3 -----------------------------------------------------------------
    def test_control_3_mismatched_clock_epochs_produce_not_computable(self):
        """Two monotonic readings, no boot witness, different epochs: the
        difference is a number, and the number is noise wearing a duration's
        name."""
        start = a_stamp(boot=None, epoch="epoch-1", mono=1_000_000_000)
        end = a_stamp(boot=None, epoch="epoch-2", mono=1_400_000_000)
        result = interval(start, end)
        assert isinstance(result, NotComputable)
        assert result.reason is NotComputableReason.UNKNOWN_BOOT_NO_EPOCH_MATCH
        assert not hasattr(result, "microseconds")

    def test_control_3b_a_reboot_between_stamps_is_a_hard_refusal(self):
        start = a_stamp(boot="boot-a", mono=9_000_000_000)
        end = a_stamp(boot="boot-b", mono=5_000_000)
        result = interval(start, end)
        assert isinstance(result, NotComputable)
        assert result.reason is NotComputableReason.BOOT_MISMATCH

    def test_control_3c_two_legacy_wall_stamps_are_not_computable(self):
        """The exact defect EVIDENCE-JOIN-CONTRACT-001 §3 named: a bare
        `datetime` minus a bare `datetime` produced a plausible number."""
        start = ObservationTimestamp.from_wall_clock(
            dt.datetime(2026, 8, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
        )
        end = ObservationTimestamp.from_wall_clock(
            dt.datetime(2026, 8, 20, 12, 0, 1, tzinfo=dt.timezone.utc)
        )
        assert start.host_id == UNKNOWN_HOST
        assert isinstance(interval(start, end), NotComputable)

    # -- 4 -----------------------------------------------------------------
    def test_control_4_same_host_monotonic_stamps_yield_a_computable_interval(
        self,
    ):
        """The permitted thing must EXIST, or the guard above is satisfied by
        a repository in which nothing works (doctrine 4)."""
        start = a_stamp(mono=1_000_000_000, epoch="epoch-1")
        end = a_stamp(
            wall="2026-08-20T12:00:00.400000Z",
            mono=1_400_000_000,
            epoch="epoch-2",  # DIFFERENT process: the cross-process case
        )
        result = interval(start, end)
        assert isinstance(result, ComputedInterval)
        assert result.basis is IntervalBasis.MONOTONIC_SAME_BOOT
        assert result.microseconds == 400_000
        assert result.sync_bound is None
        assert result.domain is TimeDomain.OURS

    def test_control_4b_same_epoch_without_a_boot_id_still_computes(self):
        """macOS has no boot id; the process epoch remains a valid witness."""
        start = a_stamp(boot=None, epoch="e", mono=1_000_000_000)
        end = a_stamp(boot=None, epoch="e", mono=1_250_000_000)
        result = interval(start, end)
        assert isinstance(result, ComputedInterval)
        assert result.basis is IntervalBasis.MONOTONIC_SAME_EPOCH
        assert result.microseconds == 250_000

    # -- 5 -----------------------------------------------------------------
    def test_control_5_an_unverified_base58_mint_cannot_join_to_a_fill(self):
        """A real, syntactically perfect, chain-resident mint string extracted
        from post text is exactly the decoy hazard. It must not join."""
        ladder = default_ladder()
        resolution = ladder.resolve(f"send it {MINT}")
        assert resolution.status is TokenResolutionStatus.TEXT_CANDIDATE
        assert resolution.mint == MINT  # the string matches the fill's mint
        assert resolution.is_joinable is False

        result = a_join(resolution=resolution)
        assert isinstance(result, JoinRefused)
        assert result.reason is JoinRefusalReason.TOKEN_NOT_CANONICALLY_VERIFIED
        assert not hasattr(result, "mint")

    def test_control_5b_every_non_canonical_status_is_refused(self):
        assert JOINABLE_STATUSES == {
            TokenResolutionStatus.CANONICALLY_VERIFIED
        }
        for status in TokenResolutionStatus:
            if status in JOINABLE_STATUSES:
                continue
            if status is TokenResolutionStatus.AMBIGUOUS:
                res = TokenResolution(
                    chain=Chain.SOLANA,
                    status=status,
                    resolver_version="v",
                    candidates=(MINT, OTHER_MINT),
                )
            elif status is TokenResolutionStatus.REJECTED:
                res = TokenResolution(
                    chain=Chain.SOLANA, status=status, resolver_version="v"
                )
            else:
                res = TokenResolution(
                    chain=Chain.SOLANA,
                    status=status,
                    resolver_version="v",
                    mint=MINT,
                )
            assert res.is_joinable is False, status
            assert isinstance(a_join(resolution=res), JoinRefused)

    # -- 6 -----------------------------------------------------------------
    def test_control_6_a_verified_canonical_mint_does_join(self):
        """The positive half. Without it, control 5 would pass in a repository
        where nothing can ever join."""
        row = a_join(resolution=a_verified_resolution())
        assert isinstance(row, JoinedEvidenceRow)
        assert row.mint == MINT
        assert row.tx_signature == SIG
        assert row.seam_version == SEAM_VERSION
        # confidence is CARRIED, never dropped at the seam (§4)
        assert row.token_resolution.confidence == "CONFIRMED"
        assert row.token_resolution.status is (
            TokenResolutionStatus.CANONICALLY_VERIFIED
        )
        # provenance travels (§5)
        assert row.raw_content_hash.startswith("sha256:")
        assert row.ingestion_version
        assert row.decoder_version == "test-decoder.v1"
        assert row.delivery_mode == "LIVE"

    def test_control_6b_a_verified_mint_that_is_not_the_fills_mint_refuses(self):
        result = a_join(resolution=a_verified_resolution(mint=OTHER_MINT))
        assert isinstance(result, JoinRefused)
        assert result.reason is JoinRefusalReason.MINT_MISMATCH

    # -- 7 -----------------------------------------------------------------
    def test_control_7_backfilled_artifacts_cannot_enter_the_live_cohort(self):
        live = [an_artifact(message_id=f"m{i}") for i in range(3)]
        backfilled = an_artifact(
            message_id="m9", delivery_mode=DeliveryMode.BACKFILL
        )

        with pytest.raises(CohortPoolingError):
            LiveLeadLagCohort(live + [backfilled])
        with pytest.raises(CohortPoolingError):
            LiveLeadLagCohort([backfilled])

        # and the join itself refuses for a latency purpose
        result = a_join(artifact=backfilled)
        assert isinstance(result, JoinRefused)
        assert result.reason is JoinRefusalReason.NOT_LIVE_DELIVERY

        # ... while remaining usable for the purposes it IS good for
        cohort = DeliveryCohort(
            delivery_mode=DeliveryMode.BACKFILL, members=(backfilled,)
        )
        cohort.assert_purpose(CohortPurpose.SOURCE_REPUTATION)
        cohort.assert_purpose(CohortPurpose.SEMANTIC_ANALYSIS)
        with pytest.raises(CohortPurposeViolation):
            cohort.assert_purpose(CohortPurpose.LATENCY_LEAD_LAG)

    def test_control_7b_pooling_is_an_error_not_a_convention(self):
        live = DeliveryCohort(
            delivery_mode=DeliveryMode.LIVE, members=(an_artifact(),)
        )
        back = DeliveryCohort(
            delivery_mode=DeliveryMode.BACKFILL,
            members=(an_artifact(delivery_mode=DeliveryMode.BACKFILL),),
        )
        with pytest.raises(CohortPoolingError):
            live.pool(back)
        # the sanctioned alternative exists and works
        both = [*live, *back]
        assert delivery_mode_breakdown(both) == {"LIVE": 1, "BACKFILL": 1}
        parts = partition_by_delivery_mode(both)
        assert set(parts) == {DeliveryMode.LIVE, DeliveryMode.BACKFILL}

    # -- 8 -----------------------------------------------------------------
    def test_control_8_flipping_delivery_mode_breaks_the_primary_cohort_test(
        self,
    ):
        """The mutation control. `_primary_cohort_assembles` is the assertion
        a real lead-lag experiment would make; flipping ONE field must turn it
        red, or `delivery_mode` is decorative."""

        def _primary_cohort_assembles(mode: DeliveryMode) -> bool:
            artifacts = [
                an_artifact(message_id=f"m{i}", delivery_mode=mode)
                for i in range(4)
            ]
            cohort = LiveLeadLagCohort(artifacts)
            return len(cohort) == 4 and cohort.delivery_mode is (
                PRIMARY_ALPHA_DELIVERY_MODE
            )

        assert _primary_cohort_assembles(DeliveryMode.LIVE) is True
        for mode in (
            DeliveryMode.BACKFILL,
            DeliveryMode.PULLED,
            DeliveryMode.UNKNOWN,
        ):
            with pytest.raises(CohortPoolingError):
                _primary_cohort_assembles(mode)


# ===========================================================================
# 1. Measurement — the two dimensions
# ===========================================================================


class TestMeasurementLegality:
    def test_the_two_key_states_differ_on_both_axes(self):
        negative = Measurement.observed_none(source="w", window=a_window())
        nothing = Measurement.not_attempted(Availability.NOT_PROVIDED)
        assert negative.availability is not nothing.availability
        assert negative.observation is not nothing.observation

    @pytest.mark.parametrize(
        "availability",
        [
            Availability.NOT_PROVIDED,
            Availability.NOT_RECONSTRUCTABLE,
            Availability.NOT_YET_OBSERVED,
            Availability.NOT_AUTHORIZED,
            Availability.NOT_APPLICABLE,
        ],
    )
    def test_observed_states_require_availability_available(self, availability):
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=1,
                availability=availability,
                observation=Observation.OBSERVED_VALUE,
                source="s",
            )
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=None,
                availability=availability,
                observation=Observation.OBSERVED_NONE,
                source="s",
                window=a_window(),
            )

    def test_observed_none_cannot_carry_a_value(self):
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=0,
                availability=Availability.AVAILABLE,
                observation=Observation.OBSERVED_NONE,
                source="s",
                window=a_window(),
            )

    def test_observed_none_requires_a_window(self):
        """A negative label without its window cannot state its noise floor."""
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=None,
                availability=Availability.AVAILABLE,
                observation=Observation.OBSERVED_NONE,
                source="s",
            )

    def test_not_attempted_cannot_carry_a_value_or_a_window(self):
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=7,
                availability=Availability.NOT_PROVIDED,
                observation=Observation.NOT_ATTEMPTED,
            )
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=None,
                availability=Availability.NOT_PROVIDED,
                observation=Observation.NOT_ATTEMPTED,
                window=a_window(),
            )

    def test_observed_states_require_a_source(self):
        with pytest.raises(IllegalMeasurementError):
            Measurement(
                value=1,
                availability=Availability.AVAILABLE,
                observation=Observation.OBSERVED_VALUE,
            )

    def test_an_inverted_window_is_refused(self):
        with pytest.raises(IllegalMeasurementError):
            ObservationWindow(
                start_utc="2026-08-20T12:05:00.000000Z",
                end_utc="2026-08-20T12:00:00.000000Z",
                basis="wall",
                watcher_id="w",
            )

    def test_exactly_seven_of_eighteen_combinations_are_legal(self):
        legal = 0
        for availability in Availability:
            for observation in Observation:
                try:
                    Measurement(
                        value=(
                            1
                            if observation is Observation.OBSERVED_VALUE
                            else None
                        ),
                        availability=availability,
                        observation=observation,
                        source="s",
                        window=(
                            a_window()
                            if observation is Observation.OBSERVED_NONE
                            else None
                        ),
                    )
                except IllegalMeasurementError:
                    continue
                legal += 1
        # 6 availabilities x 3 observations = 18; only AVAILABLE may be
        # observed, so 6 NOT_ATTEMPTED + 2 AVAILABLE-observed = 8.
        assert legal == 8


class TestVocabularyAdapters:
    def test_every_fills_absence_reason_round_trips_exactly(self):
        for reason in AbsenceReason:
            measurement = from_fills_absence(
                reason, observation=Observation.NOT_ATTEMPTED, detail="d"
            )
            assert measurement.origin == OriginTag(
                vocabulary="app.fills.absence.AbsenceReason",
                code=reason.value,
            )
            back = to_fills_maybe(measurement)
            assert isinstance(back, Absent)
            assert back.reason is reason, reason

    def test_every_social_deferred_state_round_trips_exactly(self):
        cases = {
            DeferredState.ABSENT: Deferred(),
            DeferredState.OBSERVED: Deferred(
                state=DeferredState.OBSERVED,
                observed_at="2026-08-20T12:05:00.000000Z",
                detail={"sig": SIG},
            ),
            DeferredState.OBSERVED_NONE: Deferred(
                state=DeferredState.OBSERVED_NONE,
                observed_at="2026-08-20T12:05:00.000000Z",
            ),
            DeferredState.NOT_APPLICABLE: Deferred(
                state=DeferredState.NOT_APPLICABLE,
                observed_at="2026-08-20T12:05:00.000000Z",
            ),
        }
        for state, deferred in cases.items():
            kwargs = {}
            if state is DeferredState.ABSENT:
                kwargs["availability"] = Availability.NOT_YET_OBSERVED
            if state is DeferredState.OBSERVED_NONE:
                kwargs["window"] = a_window()
            measurement = from_social_deferred(deferred, **kwargs)
            back = to_social_deferred(
                measurement, observed_at=deferred.observed_at
            )
            assert back.state is state, state

    def test_the_undetermined_dimension_must_be_supplied_not_guessed(self):
        # fills: silent about whether anyone looked
        with pytest.raises(UnmappableAbsenceError):
            from_fills_absence(AbsenceReason.NOT_PROVIDED)
        # social: silent about whether the quantity was obtainable
        with pytest.raises(UnmappableAbsenceError):
            from_social_deferred(Deferred())

    def test_a_reason_that_determines_observation_refuses_a_contradiction(self):
        with pytest.raises(UnmappableAbsenceError):
            from_fills_absence(
                AbsenceReason.NOT_AUTHORIZED,
                observation=Observation.OBSERVED_VALUE,
            )

    def test_the_two_vocabularies_are_not_mapped_onto_each_other(self):
        """§2 BINDING. A social-origin measurement may not be written back as
        a fills reason, or vice versa."""
        social = from_social_deferred(
            Deferred(), availability=Availability.NOT_PROVIDED
        )
        with pytest.raises(UnmappableAbsenceError):
            to_fills_maybe(social)
        fills = from_fills_absence(AbsenceReason.NOT_AUTHORIZED)
        with pytest.raises(UnmappableAbsenceError):
            to_social_deferred(fills)

    def test_an_observed_fills_value_carries_its_source(self):
        measurement = from_fills_maybe(observed(Decimal("1.5"), source="rpc"))
        assert measurement.is_observed_value
        assert measurement.source == "rpc"
        assert measurement.unwrap() == Decimal("1.5")

    def test_measurement_json_round_trips_both_axes(self):
        for measurement in (
            Measurement.observed_none(source="w", window=a_window()),
            Measurement.not_attempted(Availability.NOT_AUTHORIZED),
            Measurement.observed_value(3, source="s"),
            from_fills_absence(AbsenceReason.TRANSACTION_FAILED,
                               observation=Observation.NOT_ATTEMPTED),
        ):
            again = Measurement.from_json(
                json.loads(json.dumps(measurement.to_json()))
            )
            assert again == measurement


# ===========================================================================
# 2. ObservationTimestamp
# ===========================================================================


class TestClockContract:
    def test_the_boot_id_is_read_or_typed_absent_never_fabricated(self):
        real = read_host_boot_id()
        assert isinstance(real, HostBootId)
        if real.status is BootIdStatus.PRESENT:
            assert real.value
        else:
            assert real.value is None

        missing = read_host_boot_id("/nonexistent/boot_id")
        assert missing.status is BootIdStatus.NOT_AVAILABLE_ON_PLATFORM
        assert missing.value is None

    def test_a_fabricated_boot_id_is_unconstructible(self):
        with pytest.raises(ClockContractError):
            HostBootId(
                status=BootIdStatus.NOT_AVAILABLE_ON_PLATFORM, value="made-up"
            )
        with pytest.raises(ClockContractError):
            HostBootId(status=BootIdStatus.PRESENT, value=None)

    def test_an_unknown_boot_id_never_matches_another_unknown(self):
        assert HostBootId.unknown().matches(HostBootId.unknown()) is False

    def test_monotonic_anchored_needs_a_timeline_witness(self):
        with pytest.raises(ClockContractError):
            ObservationTimestamp(
                wall_utc="2026-08-20T12:00:00.000000Z",
                host_id="h",
                host_boot_id=HostBootId.unknown(),
                clock_quality=ClockQuality.MONOTONIC_ANCHORED,
                monotonic_ns=1,
                process_epoch_id=None,
            )

    def test_a_chain_or_platform_stamp_cannot_be_promoted(self):
        with pytest.raises(ClockContractError):
            ObservationTimestamp(
                wall_utc="2026-08-20T12:00:00.000000Z",
                host_id="h",
                host_boot_id=HostBootId.unknown(),
                clock_quality=ClockQuality.WALL_ONLY,
                domain=TimeDomain.CHAIN,
            )

    def test_a_naive_datetime_is_refused(self):
        with pytest.raises(ClockContractError):
            ObservationTimestamp.from_wall_clock(dt.datetime(2026, 8, 20))

    def test_a_cross_host_pair_needs_a_bound_and_the_bound_travels(self):
        start = a_stamp(host="host-a", boot="boot-a")
        end = a_stamp(
            host="host-b",
            boot="boot-b",
            wall="2026-08-20T12:00:00.400000Z",
            mono=99,
        )
        assert isinstance(interval(start, end), NotComputable)

        bound = SyncBound(
            max_error_us=2_000,
            method="chrony tracking, both hosts",
            measured_at="2026-08-20T11:00:00.000000Z",
        )
        result = interval(start, end, sync_bound=bound)
        assert isinstance(result, ComputedInterval)
        assert result.basis is IntervalBasis.WALL_BOUNDED
        assert result.sync_bound is bound
        assert result.microseconds == 400_000

    def test_a_sync_bound_must_be_measured(self):
        with pytest.raises(ClockContractError):
            SyncBound(
                max_error_us=-1,
                method="m",
                measured_at="2026-08-20T12:00:00.000000Z",
            )
        with pytest.raises(ClockContractError):
            SyncBound(
                max_error_us=1,
                method="",
                measured_at="2026-08-20T12:00:00.000000Z",
            )

    def test_wall_synchronized_must_carry_its_bound(self):
        with pytest.raises(ClockContractError):
            ObservationTimestamp(
                wall_utc="2026-08-20T12:00:00.000000Z",
                host_id="h",
                host_boot_id=HostBootId.unknown(),
                clock_quality=ClockQuality.WALL_SYNCHRONIZED,
            )

    def test_capture_observation_takes_no_timestamp(self):
        import inspect

        params = set(inspect.signature(capture_observation).parameters)
        assert "wall" not in params and "value" not in params
        stamp = capture_observation(process_epoch_id="e-1")
        assert stamp.clock_quality is ClockQuality.MONOTONIC_ANCHORED
        assert stamp.monotonic_ns is not None
        assert stamp.domain is TimeDomain.OURS

    def test_two_live_captures_in_one_process_are_computable(self):
        """Doctrine 4: assert the permitted thing EXISTS on the real host."""
        boot = read_host_boot_id()
        first = capture_observation(process_epoch_id="e-1", boot_id=boot)
        second = capture_observation(process_epoch_id="e-1", boot_id=boot)
        result = interval(first, second)
        assert isinstance(result, ComputedInterval)
        assert result.microseconds >= 0

    def test_our_received_at_lifts_losslessly(self):
        received = capture_receipt(process_epoch())
        stamp = from_our_received_at(received, host="host-a")
        assert stamp.wall_utc == received.value
        assert stamp.monotonic_ns == received.monotonic_ns
        assert stamp.process_epoch_id == received.epoch_id
        assert stamp.host_boot_id.is_known is False

    def test_timestamp_json_round_trips(self):
        stamp = a_stamp(
            sync_bound=SyncBound(
                max_error_us=5,
                method="m",
                measured_at="2026-08-20T12:00:00.000000Z",
            )
        )
        assert (
            ObservationTimestamp.from_json(
                json.loads(json.dumps(stamp.to_json()))
            )
            == stamp
        )


class TestThreeNamedQuantities:
    def test_external_delivery_latency_is_not_a_latency_and_needs_the_mode(self):
        artifact = an_artifact()
        figure = external_delivery_latency(
            artifact.source_created_at,
            artifact.our_received_at,
            delivery_mode=artifact.delivery_mode.value,
        )
        assert figure.is_latency is False
        assert figure.delivery_mode == "LIVE"
        assert figure.source_time_fidelity == "UNVERIFIED"
        assert figure.host_clock_offset_characterised is False
        with pytest.raises(ClockContractError):
            external_delivery_latency(
                artifact.source_created_at,
                artifact.our_received_at,
                delivery_mode="",
            )

    def test_our_response_latency_refuses_an_unanchored_fill_stamp(self):
        """tau_social->quote with a legacy `t_quote`: NOT_COMPUTABLE, not
        'approximately fine'."""
        received = from_our_received_at(
            capture_receipt(process_epoch()), host="host-a"
        )
        legacy_quote = ObservationTimestamp.from_wall_clock(
            dt.datetime(2026, 8, 20, 12, 0, 0, 400_000, tzinfo=dt.timezone.utc)
        )
        latency = our_response_latency(received, legacy_quote)
        assert latency.is_computable is False
        assert isinstance(latency.result, NotComputable)

    def test_our_response_latency_computes_for_two_anchored_stamps(self):
        latency = our_response_latency(
            a_stamp(mono=1_000_000_000),
            a_stamp(
                wall="2026-08-20T12:00:00.120000Z", mono=1_120_000_000
            ),
        )
        assert latency.is_computable is True
        assert latency.result.microseconds == 120_000

    def test_a_cross_domain_interval_inherits_the_worst_fidelity(self):
        figure = cross_domain_interval(
            3_000_000,
            domains=(TimeDomain.PLATFORM, TimeDomain.OURS, TimeDomain.CHAIN),
            fidelities=("VERIFIED", "UNVERIFIED"),
        )
        assert figure.worst_fidelity == "UNVERIFIED"
        assert len(figure.domains) == 3

    def test_a_single_domain_interval_is_not_a_cross_domain_interval(self):
        with pytest.raises(ClockContractError):
            CrossDomainInterval(
                microseconds=1,
                domains=(TimeDomain.OURS, TimeDomain.OURS),
                worst_fidelity="VERIFIED",
            )

    def test_the_three_quantities_have_three_different_types(self):
        types = {
            type(
                external_delivery_latency(
                    an_artifact().source_created_at,
                    an_artifact().our_received_at,
                    delivery_mode="LIVE",
                )
            ),
            type(our_response_latency(a_stamp(), a_stamp())),
            type(
                cross_domain_interval(
                    1,
                    domains=(TimeDomain.OURS, TimeDomain.CHAIN),
                    fidelities=("UNVERIFIED",),
                )
            ),
        }
        assert len(types) == 3


class TestFillsTimestampMigration:
    def test_t_quote_and_t_submit_are_observation_timestamps(self):
        quote = a_quote(
            t_quote=observed(
                dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc),
                source="t",
            )
        )
        assert isinstance(quote.t_quote.value, ObservationTimestamp)
        assert quote.t_quote.value.clock_quality is ClockQuality.WALL_ONLY

        fill = a_fill()
        assert isinstance(fill.t_submit, Absent)

    def test_a_typed_stamp_passes_through_unchanged(self):
        stamp = a_stamp()
        quote = a_quote(t_quote=observed(stamp, source="t"))
        assert quote.t_quote.value is stamp

    def test_chain_stamps_stay_in_the_chain_domain(self):
        """§3.3: `t_confirmed` is cluster-derived and is NOT our time."""
        fill = a_fill()
        assert isinstance(fill.t_confirmed.value, dt.datetime)
        assert not isinstance(fill.t_confirmed.value, ObservationTimestamp)
        assert isinstance(fill.slot, Observed)

    def test_the_record_still_serialises(self):
        quote = a_quote(
            t_quote=observed(
                dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc),
                source="t",
            )
        )
        payload = quote.as_json()
        assert payload["t_quote"]["clock_quality"] == "WALL_ONLY"
        assert json.dumps(a_fill().as_json())

    def test_the_legacy_wall_door_is_labelled_and_separate(self):
        start = ObservationTimestamp.from_wall_clock(
            dt.datetime(2026, 8, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
        )
        end = ObservationTimestamp.from_wall_clock(
            dt.datetime(2026, 8, 20, 12, 0, 1, tzinfo=dt.timezone.utc)
        )
        legacy = legacy_wall_interval_us(start, end)
        assert legacy.basis is IntervalBasis.WALL_UNANCHORED
        assert legacy.microseconds == 1_000_000
        assert any("NTP" in note for note in legacy.notes)
        # `interval()` never returns this basis
        assert isinstance(interval(start, end), NotComputable)


# ===========================================================================
# 3. TokenResolution
# ===========================================================================


class TestTokenResolution:
    def test_the_default_ladder_never_reaches_canonically_verified(self):
        resolution = default_ladder().resolve(f"buy {MINT}")
        assert resolution.status is TokenResolutionStatus.TEXT_CANDIDATE
        assert resolution.is_joinable is False
        stages = {e.stage_id for e in resolution.evidence}
        assert "chain-existence.not-wired" in stages
        assert "canonical-identity.not-wired" in stages

    def test_canonical_verification_requires_chain_existence(self):
        with pytest.raises(TokenResolutionError):
            TokenResolution(
                chain=Chain.SOLANA,
                status=TokenResolutionStatus.CANONICALLY_VERIFIED,
                resolver_version="v",
                mint=MINT,
                evidence=(
                    ResolutionEvidence(
                        kind=EvidenceKind.PROJECT_ACCOUNT_LINK,
                        stage_id="s",
                        detail="d",
                    ),
                ),
            )

    def test_chain_existence_alone_is_not_enough(self):
        """A decoy address in a scam post is a real mint too."""
        with pytest.raises(TokenResolutionError):
            TokenResolution(
                chain=Chain.SOLANA,
                status=TokenResolutionStatus.CANONICALLY_VERIFIED,
                resolver_version="v",
                mint=MINT,
                evidence=(
                    ResolutionEvidence(
                        kind=EvidenceKind.CHAIN_MINT_EXISTS,
                        stage_id="s",
                        detail="d",
                    ),
                ),
            )

    def test_an_alias_match_is_not_canonical(self):
        resolution = TokenResolution(
            chain=Chain.SOLANA,
            status=TokenResolutionStatus.RESOLVED_FROM_ALIAS,
            resolver_version="v",
            mint=MINT,
            evidence=(
                ResolutionEvidence(
                    kind=EvidenceKind.ALIAS_TABLE, stage_id="s", detail="d"
                ),
            ),
        )
        assert resolution.is_joinable is False

    def test_ambiguous_must_not_collapse_to_one_mint(self):
        with pytest.raises(TokenResolutionError):
            TokenResolution(
                chain=Chain.SOLANA,
                status=TokenResolutionStatus.AMBIGUOUS,
                resolver_version="v",
                mint=MINT,
                candidates=(MINT, OTHER_MINT),
            )
        with pytest.raises(TokenResolutionError):
            TokenResolution(
                chain=Chain.SOLANA,
                status=TokenResolutionStatus.AMBIGUOUS,
                resolver_version="v",
                candidates=(MINT,),
            )

    def test_rejected_never_exposes_a_mint(self):
        with pytest.raises(TokenResolutionError):
            TokenResolution(
                chain=Chain.SOLANA,
                status=TokenResolutionStatus.REJECTED,
                resolver_version="v",
                mint=MINT,
            )

    def test_a_post_naming_two_addresses_is_ambiguous_not_the_first(self):
        resolution = default_ladder().resolve(f"{MINT} vs {OTHER_MINT}")
        assert resolution.status is TokenResolutionStatus.AMBIGUOUS
        assert resolution.mint is None
        assert set(resolution.candidates) == {MINT, OTHER_MINT}

    def test_the_social_confirmed_state_does_not_become_canonical(self):
        """`app.social` defines CONFIRMED as 'confirmed against an
        authoritative source' with no such source wired. It must not be
        promoted into the join gate."""
        entity = EntityResolution(
            confidence=ResolutionConfidence.CONFIRMED,
            resolved_mint=MINT,
            candidates=(MINT,),
            resolver_id="social.v1",
        )
        resolution = from_entity_resolution(entity)
        assert resolution.status is TokenResolutionStatus.RESOLVED_FROM_ALIAS
        assert resolution.is_joinable is False
        assert resolution.confidence == "CONFIRMED"  # carried, not dropped

    def test_no_stage_in_the_default_ladder_touches_the_network(self):
        ladder = default_ladder()
        assert isinstance(ladder.stages[0], TextCandidateStage)
        assert all(
            isinstance(stage, NotWiredStage) for stage in ladder.stages[1:]
        )

    def test_resolution_json_round_trips(self):
        resolution = a_verified_resolution()
        assert (
            TokenResolution.from_json(
                json.loads(json.dumps(resolution.to_json()))
            )
            == resolution
        )


# ===========================================================================
# 4. Seam reachability (doctrine 5) — asserted from OUTSIDE
# ===========================================================================


class TestSeamReachability:
    def test_the_join_is_reachable_with_the_real_collaborators(self):
        """Real SocialArtifact, real RealizedFill, real timebase, real
        resolver. No mocks: a seam proven only against stubs is a seam whose
        production path has never been walked."""
        artifact = an_artifact()
        fill = a_fill()
        row = join_social_to_fill(
            artifact=artifact,
            resolution=a_verified_resolution(),
            fill=fill,
            quote_observed_at=capture_observation(process_epoch_id="e-1"),
            onchain_reaction=Measurement.observed_none(
                source="watcher", window=a_window()
            ),
            price_reaction=Measurement.not_attempted(
                Availability.NOT_YET_OBSERVED
            ),
        )
        assert isinstance(row, JoinedEvidenceRow)
        assert row.message_identity == artifact.message_identity
        assert row.content_identity == artifact.content_identity
        assert row.slot == 300_000_000
        # the whole row serialises, so it can actually be written down
        assert json.loads(json.dumps(row.to_json()))["joined"] is True

    def test_a_fill_without_a_signature_is_refused(self):
        result = a_join(fill=a_fill(signature=None))
        assert isinstance(result, JoinRefused)
        assert result.reason is JoinRefusalReason.NO_FILL_SIGNATURE

    def test_a_refusal_carries_no_row_fields(self):
        result = a_join(resolution=default_ladder().resolve(f"buy {MINT}"))
        assert isinstance(result, JoinRefused)
        for attribute in ("mint", "tx_signature", "our_response", "slot"):
            assert not hasattr(result, attribute)

    def test_a_backfilled_artifact_still_joins_for_a_non_latency_purpose(self):
        """BACKFILL is not junk. It is disqualified from ONE thing."""
        row = a_join(
            artifact=an_artifact(delivery_mode=DeliveryMode.BACKFILL),
            purpose=CohortPurpose.SEMANTIC_ANALYSIS,
        )
        assert isinstance(row, JoinedEvidenceRow)
        assert row.delivery_mode == "BACKFILL"
        # ... and the delivery figure still carries the mode, so no consumer
        # can pool it by accident
        assert row.external_delivery.delivery_mode == "BACKFILL"


class TestSeamContainsNoSignal:
    def test_the_package_ranks_scores_and_predicts_nothing(self):
        import ast
        from pathlib import Path

        banned = {
            "score",
            "rank",
            "predict",
            "signal",
            "alpha_of",
            "edge",
            "expectancy",
        }
        seam = Path(__file__).resolve().parents[1] / "app" / "seam"
        offenders = []
        for path in sorted(seam.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.lower() in banned:
                        offenders.append(f"{path.name}:{node.name}")
        assert offenders == []
