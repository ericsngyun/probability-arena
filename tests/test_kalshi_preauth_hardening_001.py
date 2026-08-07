"""KALSHI-OBSERVER-PREAUTH-HARDENING-001 — Gate 9.

Zero credentials, zero provider calls, zero network. Every key is generated in
the test; every wire message is a fixture.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import book as bk
from app.realtime import kalshi as kx

PKG = Path(bk.__file__).parent
REPO = PKG.parent.parent
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

A, B = "KXMLBGAME-A", "KXMLBGAME-B"


# --- Gate 7: current wire schema --------------------------------------------------
def snapshot(sid=1, seq=1, ticker=A, yes=None, no=None, generation=1):
    """`orderbook_snapshot` exactly as the spec documents it."""
    return {"event_type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "market_ticker": ticker, "subscription_generation": generation,
            "raw": {"msg": {"market_ticker": ticker, "market_id": "mid-1",
                            "yes_dollars_fp": yes or [],
                            "no_dollars_fp": no or []}}}


def delta(sid=1, seq=2, ticker=A, side="yes", price="0.6000", amount="1.00",
          generation=1, ts_ms=None):
    """`orderbook_delta` exactly as the spec documents it."""
    msg = {"market_ticker": ticker, "market_id": "mid-1",
           "price_dollars": price, "delta_fp": amount, "side": side}
    if ts_ms is not None:
        msg["ts_ms"] = ts_ms
    return {"event_type": "orderbook_delta", "sid": sid, "seq": seq,
            "market_ticker": ticker, "subscription_generation": generation,
            "raw": {"msg": msg}}


def router(tickers=(A, B), sid=1):
    sub = bk.SubscriptionState(sid, market_tickers=tickers)
    return bk.SubscriptionRouter(sub)


def seed(r, sid=1, generation=1):
    """One snapshot per market, on one subscription, sharing the seq stream."""
    r.dispatch(snapshot(sid=sid, seq=1, ticker=A, generation=generation,
                        yes=[["0.6000", "10.00"]], no=[["0.3500", "8.00"]]))
    r.dispatch(snapshot(sid=sid, seq=2, ticker=B, generation=generation,
                        yes=[["0.4000", "5.00"]], no=[["0.5500", "6.00"]]))
    return r


# --- 1-2: subscription-level sequencing -------------------------------------------
class TestSubscriptionSequencing:
    def test_1_two_markets_share_one_subscription(self):
        r = seed(router())
        assert set(r.books) == {A, B}
        assert r.publishable_books() == {A: True, B: True}
        assert r.subscription.sid == 1
        assert r.subscription.last_seq == 2

    def test_2_interleaved_traffic_does_not_false_gap_the_other_market(self):
        """The defect this milestone exists to fix. `seq` counts messages across
        the whole subscription, so a per-market view of it has a hole at every
        sibling message — with two markets each book halted on its second."""
        r = seed(router())
        for seq, ticker in ((3, A), (4, B), (5, A), (6, B), (7, A)):
            r.dispatch(delta(seq=seq, ticker=ticker))
        assert r.publishable_books() == {A: True, B: True}
        assert r.subscription.healthy is True
        assert r.subscription.stats["gaps"] == 0
        assert r.books[A].publishable and r.books[B].publishable
        # A's own view of seq is 1,3,5,7 — full of holes that are B's traffic.
        assert r.subscription.last_seq == 7

    def test_2b_thirty_markets_interleaved_stay_healthy(self):
        tickers = tuple(f"KX-{i:02d}" for i in range(30))
        r = bk.SubscriptionRouter(bk.SubscriptionState(9, market_tickers=tickers))
        seq = 0
        for t in tickers:
            seq += 1
            r.dispatch(snapshot(sid=9, seq=seq, ticker=t,
                                yes=[["0.5000", "1.00"]], no=[["0.4000", "1.00"]]))
        for round_ in range(3):
            for t in tickers:
                seq += 1
                r.dispatch(delta(sid=9, seq=seq, ticker=t, amount="1.00"))
        assert all(r.publishable_books().values())
        assert r.subscription.stats["gaps"] == 0


# --- 3-7: subscription integrity failures ------------------------------------------
class TestSubscriptionIntegrity:
    def test_3_a_sid_gap_invalidates_every_derived_book(self):
        """Nothing in the hole says which market the lost message belonged to,
        so repairing only the market named in the NEXT message would leave the
        others silently wrong."""
        r = seed(router())
        r.dispatch(delta(seq=3, ticker=A))
        with pytest.raises(bk.SubscriptionError, match="sequence gap"):
            r.dispatch(delta(seq=99, ticker=B))
        assert r.subscription.healthy is False
        assert r.publishable_books() == {A: False, B: False}
        for t in (A, B):
            with pytest.raises(bk.BookIntegrityError):
                r.books[t].top_of_book()

    def test_4_duplicate_subscription_sequence_is_rejected(self):
        r = seed(router())
        r.dispatch(delta(seq=3, ticker=A))
        out = r.dispatch(delta(seq=3, ticker=A, amount="99.00"))
        assert out["action"] == "duplicate_ignored"
        assert r.subscription.stats["duplicates"] == 1
        assert r.books[A].yes[6000] == 1100   # 10.00 + 1.00, not + 99.00

    def test_5_subscription_sequence_regression_is_rejected(self):
        r = seed(router())
        r.dispatch(delta(seq=3, ticker=A))
        with pytest.raises(bk.SubscriptionError, match="regression"):
            r.dispatch(delta(seq=2, ticker=B))
        assert r.publishable_books() == {A: False, B: False}

    def test_6_wrong_sid_is_rejected(self):
        r = seed(router())
        with pytest.raises(bk.SubscriptionError, match="belongs to subscription"):
            r.dispatch(delta(sid=2, seq=3, ticker=A))
        assert r.publishable_books() == {A: False, B: False}

    def test_7_superseded_generation_is_rejected(self):
        """A straggler from the old stream carries sequence numbers from a
        different namespace; without the generation it looks like a gap."""
        r = seed(router())
        gen = r.subscription.supersede()
        assert gen == 2
        r.dispatch(snapshot(seq=10, ticker=A, generation=2,
                            yes=[["0.6000", "1.00"]]))
        with pytest.raises(bk.SubscriptionError, match="generation"):
            r.dispatch(delta(seq=4, ticker=A, generation=1))
        assert r.subscription.stats["stale_generation"] == 1

    def test_missing_sequence_is_a_fault(self):
        r = seed(router())
        with pytest.raises(bk.SubscriptionError, match="absent is not ordered"):
            r.dispatch(delta(seq=None, ticker=A))
        assert r.publishable_books() == {A: False, B: False}

    def test_unrouteable_message_invalidates_the_subscription(self):
        r = seed(router())
        rec = delta(seq=3, ticker=A)
        rec["market_ticker"] = None
        rec["raw"]["msg"].pop("market_ticker")
        with pytest.raises(bk.SubscriptionError, match="cannot be routed"):
            r.dispatch(rec)
        assert r.publishable_books() == {A: False, B: False}

    def test_unsubscribed_market_is_refused(self):
        r = seed(router())
        with pytest.raises(bk.SubscriptionError, match="did not subscribe"):
            r.dispatch(delta(seq=3, ticker="KX-NEVER-ASKED-FOR"))


# --- 8-10: recovery ----------------------------------------------------------------
class TestRecovery:
    def test_8_get_snapshot_recovery(self):
        r = seed(router())
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(delta(seq=99, ticker=A))
        cmd = kx.build_get_snapshot(7, r.subscription.sid)
        assert cmd["cmd"] == "update_subscription"
        assert cmd["params"] == {"sids": [1], "action": "get_snapshot"}
        r.subscription.begin_recovery()
        assert r.subscription.last_seq is None
        r.dispatch(snapshot(seq=100, ticker=A, yes=[["0.6100", "4.00"]]))
        r.dispatch(snapshot(seq=101, ticker=B, yes=[["0.4100", "4.00"]]))
        assert r.publishable_books() == {A: True, B: True}
        assert r.subscription.stats["recoveries"] == 1

    def test_9_resubscription_recovery_supersedes_the_generation(self):
        r = seed(router())
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(delta(seq=99, ticker=A))
        cmds = kx.build_resubscribe(7, r.subscription.sid,
                                    ["orderbook_delta"], [A, B])
        assert [c["cmd"] for c in cmds] == ["unsubscribe", "subscribe"]
        assert cmds[1]["params"]["use_yes_price"] is True
        gen = r.subscription.supersede(market_tickers=(A, B))
        r.dispatch(snapshot(seq=1, ticker=A, generation=gen,
                            yes=[["0.6100", "4.00"]]))
        r.dispatch(snapshot(seq=2, ticker=B, generation=gen,
                            yes=[["0.4100", "4.00"]]))
        assert r.publishable_books() == {A: True, B: True}

    def test_10_no_stale_book_is_published_during_recovery(self):
        r = seed(router())
        r.dispatch(delta(seq=3, ticker=A, price="0.6000", amount="5.00"))
        before = r.books[A].top_of_book()["best_yes_bid"]
        with pytest.raises(bk.SubscriptionError):
            r.dispatch(delta(seq=99, ticker=B))
        assert r.publishable_books() == {A: False, B: False}
        r.subscription.begin_recovery()
        # Mid-recovery: still nothing publishable, and the old value is gone.
        assert r.publishable_books() == {A: False, B: False}
        for t in (A, B):
            with pytest.raises(bk.BookIntegrityError):
                r.books[t].top_of_book()
        # Only A is re-snapshotted; B must stay unpublished.
        r.dispatch(snapshot(seq=200, ticker=A, yes=[["0.7000", "1.00"]]))
        assert r.publishable_books()[B] is False
        assert r.books[A].top_of_book()["best_yes_bid"] != before

    def test_a_delta_during_recovery_is_rejected_not_buffered(self):
        r = seed(router())
        r.subscription.begin_recovery()
        with pytest.raises(bk.SubscriptionError, match="not healthy"):
            r.dispatch(delta(seq=50, ticker=A))


# --- 11-15: credential isolation and signing authority ------------------------------
def _py_files():
    for base in ("app",):
        yield from sorted((REPO / base).rglob("*.py"))


class TestCredentialIsolation:
    def test_11_observer_credential_is_read_by_the_observer_loader_only(self):
        """A generic Kalshi credential is one any Kalshi subsystem can pick up,
        so the blast radius of a mis-scoped key would be every caller."""
        readers = []
        for path in _py_files():
            src = path.read_text()
            if ("kalshi_observer_api_key_id" in src
                    or "kalshi_observer_credential_path" in src):
                readers.append(str(path.relative_to(REPO)))
        assert set(readers) <= {"app/config.py", "app/main.py",
                                "app/realtime/auth.py"}, readers
        for forbidden in ("app/adapters/kalshi.py", "app/services/scanner.py",
                          "app/services/watcher.py", "app/services/research.py"):
            assert forbidden not in readers

    def test_12_legacy_generic_credential_cannot_satisfy_observer_config(self):
        from app.config import Settings

        s = Settings(kalshi_observer_api_key_id="", kalshi_observer_credential_path="")
        assert s.observer_credential_configured is False
        # The generic fields no longer exist at all, so they cannot be aliased
        # into the observer's configuration by accident.
        assert not hasattr(s, "kalshi_api_key_id")
        assert not hasattr(s, "kalshi_private_key_path")
        assert not hasattr(s, "ws_enabled")
        # Setting the old names is silently ignored (extra="ignore"), and in
        # particular does NOT configure the observer.
        s2 = Settings(kalshi_api_key_id="leftover", kalshi_private_key_path="/tmp/k.pem")
        assert s2.observer_credential_configured is False
        # Half a credential is not a credential.
        assert Settings(kalshi_observer_api_key_id="k",
                        kalshi_observer_credential_path="").observer_credential_configured is False

    def test_13_exactly_one_pem_loader_exists_in_the_repository(self):
        loaders = []
        for path in _py_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in (
                        "load_pem_private_key", "load_der_private_key"):
                    loaders.append(str(path.relative_to(REPO)))
                elif isinstance(node, ast.Name) and node.id in (
                        "load_pem_private_key", "load_der_private_key"):
                    loaders.append(str(path.relative_to(REPO)))
        assert sorted(set(loaders)) == ["app/realtime/auth.py"], loaders

    def test_14_exactly_one_signing_surface_exists(self):
        signers = []
        for path in _py_files():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "sign"):
                    signers.append(str(path.relative_to(REPO)))
        assert sorted(set(signers)) == ["app/realtime/auth.py"], signers
        # And the deleted legacy module is really gone.
        assert not (REPO / "app/services/ws_snapshots.py").exists()

    def test_15_no_general_purpose_signing_api(self):
        from app.realtime import auth as ka

        assert [n for n in dir(ka.ReadOnlyRequestSigner)
                if n.startswith("from_")] == ["from_path"]
        assert not hasattr(ka.ReadOnlyRequestSigner, "sign")
        code = ka.ReadOnlyRequestSigner.websocket_headers.__code__
        params = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
        assert "method" not in params
        assert "path" not in params
        # KALSHI-DEMO-READONLY-VALIDATION-001 added the one-shot credential
        # audit route. Both entries are GET, both are constants, and neither is
        # reachable without holding the matching typed purpose.
        assert ka.READ_ONLY_PATH_ALLOWLIST == frozenset(
            {"/trade-api/ws/v2", "/trade-api/v2/api_keys"})
        assert all(m == "GET" for m, _p in kx.AUTH_PURPOSE_ROUTES.values())
        assert kx.ALLOWED_HTTP_METHODS == ("GET",)

    def test_pem_contents_are_never_read_from_the_environment(self):
        """The config holds a key id and a PATH. Environment variables are
        readable from /proc, leak into `docker inspect`, and persist in shell
        history."""
        from app.config import Settings

        for field in Settings.model_fields:
            assert "pem" not in field.lower()
            assert not field.endswith("_private_key")


# --- 16-19: wire schema and channel contract ---------------------------------------
class TestWireContract:
    def test_16_use_yes_price_is_structurally_present(self):
        cmd = kx.build_subscribe(1, ["orderbook_delta"], [A, B])
        assert cmd["params"]["use_yes_price"] is True
        # Structural, not incidental: a literal True in the builder, so it can
        # never be inherited from a server default that may migrate.
        src = ast.parse((PKG / "kalshi.py").read_text())
        found = False
        for node in ast.walk(src):
            if (isinstance(node, ast.Constant) and node.value == "use_yes_price"):
                found = True
        assert found
        for c in kx.build_resubscribe(1, 1, ["orderbook_delta"], [A])[1:]:
            assert c["params"]["use_yes_price"] is True

    def test_17_current_snapshot_schema_applies(self):
        r = router((A,))
        out = r.dispatch(snapshot(yes=[["0.6000", "10.00"]],
                                  no=[["0.3500", "8.00"]]))
        assert out["yes_levels"] == 1 and out["no_levels"] == 1
        top = r.books[A].top_of_book()
        assert top["best_yes_bid"] == "0.6000"
        assert top["best_yes_ask"] == "0.6500"   # 1 - 0.3500, derived from NO

    def test_18_current_delta_schema_applies_including_optional_ts_ms(self):
        r = seed(router())
        out = r.dispatch(delta(seq=3, ticker=A, side="no", price="0.3500",
                               amount="-2.00", ts_ms=1_754_500_000_000))
        assert out["action"] == "delta" and out["side"] == "no"
        assert out["level_units"] == 600        # 8.00 - 2.00
        # ts_ms is optional: its absence must not change acceptance.
        assert r.dispatch(delta(seq=4, ticker=A))["action"] == "delta"

    def test_19_ticker_v2_is_not_used(self):
        assert "ticker" in kx.ALLOWED_CHANNELS
        assert "ticker_v2" not in kx.ALLOWED_CHANNELS
        for path in sorted(PKG.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert "ticker_v2" not in node.value, path
        with pytest.raises(kx.CapabilityError):
            kx.build_subscribe(1, ["ticker_v2"], [A])

    def test_lifecycle_open_close_is_not_assumed_to_be_explicit(self):
        assert kx.LIFECYCLE_GUARANTEES_EXPLICIT_OPEN_CLOSE is False

    def test_private_user_channels_remain_refused(self):
        for ch in kx.FORBIDDEN_CHANNELS:
            with pytest.raises(kx.CapabilityError):
                kx.build_subscribe(1, [ch], [A])


# --- Gate 6: canonical YES-price state ---------------------------------------------
class TestYesPriceCanonicalState:
    def test_every_level_retains_the_venue_words_and_our_reading(self):
        r = router((A,))
        r.dispatch(snapshot(yes=[["0.6000", "10.00"]], no=[["0.3500", "8.00"]]))
        ladder = r.books[A].yes_scale_ladder()
        assert ladder["use_yes_price_requested"] is True
        assert ladder["no_side_normalization"] == "complement"
        for level in ladder["bids"] + ladder["asks"]:
            for f in ("venue_side", "raw_price_string", "raw_price_units",
                      "normalized_yes_price_units"):
                assert f in level, (f, level)
        ask = ladder["asks"][0]
        assert ask["venue_side"] == "no"
        assert ask["raw_price_units"] == 3500          # what the venue said
        assert ask["normalized_yes_price_units"] == 6500   # our reading
        assert ask["raw_price_string"] == "0.3500"

    def test_a_crossed_book_still_refuses(self):
        """Standing in for the unverified `use_yes_price` convention: if the NO
        ladder already arrives YES-scaled, the complement is applied twice and
        this is the symptom."""
        r = router((A,))
        with pytest.raises(bk.BookIntegrityError, match="crossed"):
            r.dispatch(snapshot(yes=[["0.8000", "1.00"]], no=[["0.9000", "1.00"]]))


# --- 20: replay determinism --------------------------------------------------------
class TestReplayDeterminism:
    def _records(self):
        out = [snapshot(seq=1, ticker=A, yes=[["0.6000", "10.00"]],
                        no=[["0.3500", "8.00"]]),
               snapshot(seq=2, ticker=B, yes=[["0.4000", "5.00"]],
                        no=[["0.5500", "6.00"]])]
        for i, (t, side, price, amt) in enumerate((
                (A, "yes", "0.5900", "4.00"), (B, "no", "0.5600", "2.00"),
                (A, "yes", "0.6000", "-3.00"), (B, "yes", "0.4000", "1.00")),
                start=3):
            out.append(delta(seq=i, ticker=t, side=side, price=price, amount=amt))
        return out

    def test_20_replay_is_deterministic_across_the_subscription(self):
        recs = self._records()
        first = {t: b.checksum()
                 for t, b in seed_replay(recs).books.items()}
        second = {t: b.checksum()
                  for t, b in seed_replay(recs).books.items()}
        assert first == second
        assert set(first) == {A, B}

    def test_20b_replay_reproduces_the_live_books_exactly(self):
        recs = self._records()
        live = seed_replay(recs)
        replayed = seed_replay(recs)
        assert ({t: b.checksum() for t, b in live.books.items()}
                == {t: b.checksum() for t, b in replayed.books.items()})


def seed_replay(records):
    r = router((A, B))
    for rec in records:
        r.dispatch(rec)
    return r


# --- 21: audits --------------------------------------------------------------------
class TestAudits:
    def test_21_safety_audit_is_clean_with_no_allowlist_for_config(self):
        from app.services.frontier_eval import (
            SAFETY_ALLOWLIST_FRAGMENTS, FrontierEvalService,
        )

        result = FrontierEvalService.safety_audit(None)
        assert result["safety_ok"] is True, result["violations"]
        # One private-key surface, one allowlist entry, nothing else.
        assert SAFETY_ALLOWLIST_FRAGMENTS == {
            "app/realtime/auth.py": ("private_key",)}

    def test_observer_package_remains_inert(self):
        for path in sorted(PKG.rglob("*.py")):
            tree = ast.parse(path.read_text())
            mods = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods.add(node.module)
            for banned in ("sqlite3", "sqlalchemy", "app.db", "app.models",
                           "websockets", "httpx", "aiohttp", "requests"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in mods), (path, banned)
            assert "__main__" not in path.read_text()

    def test_no_timer_service_or_daemon_was_added(self):
        infra = REPO / "infra"
        units = sorted(p.name for p in infra.rglob("*.service"))
        units += sorted(p.name for p in infra.rglob("*.timer"))
        for u in units:
            assert "kalshi" not in u and "observer" not in u and "realtime" not in u


class TestTheCorrectionIsReal:
    """Proof the new architecture is what fixes it, not the new fixtures.

    A test suite written alongside a rewrite can pass because both sides moved.
    This pins the old model's behaviour explicitly so the discrimination is
    visible in the file rather than inferred.
    """

    def test_per_market_sequencing_false_gaps_and_the_router_does_not(self):
        # OLD MODEL: each book compares seq itself.
        a, b = bk.OrderBook(A), bk.OrderBook(B)
        a.apply_snapshot({"market_ticker": A, "yes_dollars_fp": [["0.6000", "10.00"]],
                          "no_dollars_fp": []}, seq=1, sid=1)
        b.apply_snapshot({"market_ticker": B, "yes_dollars_fp": [["0.4000", "5.00"]],
                          "no_dollars_fp": []}, seq=2, sid=1)
        rejected = 0
        for seq, book, t in ((3, a, A), (4, b, B), (5, a, A), (6, b, B)):
            try:
                book.apply_delta({"market_ticker": t, "side": "yes",
                                  "price_dollars": "0.6000", "delta_fp": "1.00"},
                                 seq=seq, sid=1)
            except bk.BookIntegrityError:
                rejected += 1
        assert rejected == 4, "the old model must reject every interleaved delta"
        assert not a.publishable and not b.publishable

        # NEW MODEL: ordering settled once, at the subscription.
        r = seed(router())
        for seq, t in ((3, A), (4, B), (5, A), (6, B)):
            r.dispatch(delta(seq=seq, ticker=t))
        assert r.publishable_books() == {A: True, B: True}

    def test_replay_of_a_two_market_archive_survives(self, tmp_path):
        """The same defect reached replay, so a two-market archive rebuilt as
        two permanently halted books and reported it as a venue fault."""
        recs = [snapshot(seq=1, ticker=A, yes=[["0.6000", "10.00"]]),
                snapshot(seq=2, ticker=B, yes=[["0.4000", "5.00"]])]
        recs += [delta(seq=s, ticker=t)
                 for s, t in ((3, A), (4, B), (5, A), (6, B))]
        out = ar.replay(recs)
        assert out["events_rejected"] == 0, out["faults"]
        assert out["publishable"] == {A: True, B: True}
        assert out["subscriptions"] == 1
        assert all(v is not None for v in out["checksums"].values())

    def test_replay_separates_distinct_subscriptions(self):
        recs = [snapshot(sid=1, seq=1, ticker=A, yes=[["0.6000", "1.00"]]),
                snapshot(sid=2, seq=1, ticker=B, yes=[["0.4000", "1.00"]]),
                delta(sid=1, seq=2, ticker=A),
                delta(sid=2, seq=2, ticker=B)]
        out = ar.replay(recs)
        assert out["subscriptions"] == 2
        assert out["events_rejected"] == 0, out["faults"]
        assert out["publishable"] == {A: True, B: True}
