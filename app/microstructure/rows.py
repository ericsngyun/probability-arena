"""MARKET-MICROSTRUCTURE-ROW-BUILDER-001 — tape + panels -> 1 Hz research rows.

Owns the time discipline, and only the time discipline. `features` and
`labels` do not import each other; this module is the single place they meet,
and it meets them in **two separate passes**:

  pass 1  forward replay -> M0/M1 at each sample. Cannot see the future,
          because the replay has not reached it.
  pass 2  attach labels from the completed published-mid grid.

That ordering is the structural reason a future price cannot enter a feature.

**Panel membership is binding.** `panel(t_k)` governs the half-open interval
`[t_k, t_{k+1})`. A market outside the governing panel produces no row even
though its frames are on the tape — the subscribed set (<= 24) is a capacity
quantity, the research panel (K = 12) is the exposure quantity, and only the
second emits rows.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.microstructure import features as F
from app.microstructure import labels as L
from app.microstructure.panel import DatasetRole, SESSION_OK
from app.realtime.book import SubscriptionRouter, SubscriptionState

SAMPLE_INTERVAL_S = 1
ORDERBOOK_TYPES = ("orderbook_delta", "orderbook_snapshot")

ROW_SCHEMA_VERSION = "microstructure-row-v1"
LABEL_SCHEMA_VERSION = "microstructure-label-v1"

SKIP_NOT_IN_PANEL = "not_in_governing_panel"
SKIP_BOOK_UNPUBLISHABLE = "book_not_publishable"
SKIP_BEFORE_FIRST_PANEL = "before_first_panel_decision"


def segment_dirs_in_order(env_dir: Path) -> list[Path]:
    """Segments in the order they were opened.

    Ordered by each manifest's `opened_at` rather than by path text. Sorting
    the FILE paths is subtly wrong -- `segment=X.r0001/events.jsonl.gz` sorts
    before `segment=X/events.jsonl.gz` because `.` < `/` -- which would replay
    a later segment first and reject every delta for want of a base.
    """
    dirs = [d for d in env_dir.glob("**/segment=*") if (d / "manifest.json").exists()]

    def opened(d):
        return json.loads((d / "manifest.json").read_text())["opened_at"]

    return sorted(dirs, key=opened)


def read_records(env_dir: Path):
    for d in segment_dirs_in_order(env_dir):
        f = d / "events.jsonl.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def _ms(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


def _dispatch_record(rec: dict) -> dict:
    """Archive record -> the envelope `SubscriptionRouter.dispatch` expects.

    `sid`, `seq` and the generation are taken from the ARCHIVE record's own
    columns rather than from whatever `normalized_event` happens to echo. Live
    tape does echo them, so relying on the echo works right up until a fixture
    or a schema revision does not carry it -- and then every delta is refused
    for belonging to "subscription None".
    """
    ne = rec.get("normalized_event") or {}
    return {**ne, "raw": rec.get("raw_event"), "seq": rec.get("seq"),
            "sid": rec.get("subscription_id"),
            "market_ticker": rec.get("market_ticker"),
            "subscription_generation": rec.get("subscription_generation")}


@dataclass
class PanelSchedule:
    """`panel(t_k)` governs `[t_k, t_{k+1})`."""
    ticks: list           # [(tick_ms, frozenset(panel), tick_id)]

    def governing(self, sample_ms: float):
        found = None
        for tick_ms, panel, tick_id in self.ticks:
            if tick_ms <= sample_ms:
                found = (tick_ms, panel, tick_id)
            else:
                break
        return found


def build_rows(*, env_dir: Path, panel_schedule: PanelSchedule, markets: dict,
               session_id: str, capture_commit: str, dataset_role: str,
               session_status: str = SESSION_OK,
               preregistration_version: str,
               trade_lag_ms: int = F.TRADE_LAG_MS) -> dict:
    """Build every research row the session supports, plus an audit report."""
    if dataset_role not in DatasetRole.ALL:
        raise ValueError(f"dataset_role must be typed, got {dataset_role!r}")

    records = list(read_records(env_dir))
    if not records:
        raise ValueError("no records on tape")
    stamped = [(r, _ms(r["received_at_utc"])) for r in records
               if r.get("received_at_utc")]
    session_open_ms, session_end_ms = stamped[0][1], stamped[-1][1]

    tickers = tuple(sorted(markets))
    sub = SubscriptionState(1, market_tickers=tickers, generation=1)
    router = SubscriptionRouter(sub)
    acc = {t: F.FlowAccumulator() for t in tickers}

    # 1 Hz grid, anchored on the session's first frame.
    first_tick_ms = panel_schedule.ticks[0][0] if panel_schedule.ticks else None
    samples = []
    s = session_open_ms
    while s <= session_end_ms:
        samples.append(s)
        s += SAMPLE_INTERVAL_S * 1000

    rows, skips, mid_grid = [], {}, {t: {} for t in tickers}
    dispatch_errors = 0
    si, ri = 0, 0

    # ---- pass 1: forward replay; features only -----------------------------
    for sample_ms in samples:
        while ri < len(stamped) and stamped[ri][1] <= sample_ms:
            rec, ms = stamped[ri]
            mt = rec.get("message_type")
            if mt in ORDERBOOK_TYPES:
                try:
                    router.dispatch(_dispatch_record(rec))
                except Exception:
                    dispatch_errors += 1
                msg = (rec.get("raw_event") or {}).get("msg") or {}
                tk = rec.get("market_ticker")
                if tk in acc and mt == "orderbook_delta":
                    try:
                        acc[tk].add_delta(
                            ms, float(msg.get("delta_fp", 0)) * F.CONTRACT_UNITS_PER_CONTRACT,
                            msg.get("side", ""),
                            int(round(float(msg.get("price_dollars", 0)) * F.PRICE_UNITS_PER_PROB)))
                    except (TypeError, ValueError):
                        pass
            elif mt == "trade":
                msg = (rec.get("raw_event") or {}).get("msg") or {}
                tk = rec.get("market_ticker")
                if tk in acc:
                    try:
                        cnt = float(msg.get("count_fp", 0))
                        signed = cnt if msg.get("taker_side") == "yes" else -cnt
                        acc[tk].add_trade(ms, signed)
                    except (TypeError, ValueError):
                        pass
            ri += 1

        gov = panel_schedule.governing(sample_ms)
        for tk in tickers:
            book = router.book_for(tk)
            state = book.publication_state
            bid_u = book.best_yes_bid_units if state.publishable else None
            ask_u = book.best_yes_ask_units if state.publishable else None
            acc[tk].add_best(sample_ms, bid_u, ask_u)

            # THE MID GRID IS OBSERVABILITY, NOT SELECTION.
            # Recorded for every subscribed market with a publishable book,
            # whether or not it is in the governing panel. Restricting it to
            # panel members made a 300 s label depend on the market still
            # being selected 300 s later -- and since rotation is also 300 s,
            # label availability became correlated with panel persistence,
            # which is correlated with sustained activity. That is a
            # selection-dependent target, not a coverage inconvenience.
            # A future mid is a price the venue published; whether we chose to
            # emit a research row for that market at that instant has nothing
            # to do with whether its price moved.
            if bid_u is not None and ask_u is not None:
                mid_obs = ((bid_u + ask_u) / 2) / F.PRICE_UNITS_PER_PROB
                acc[tk].add_mid(sample_ms, mid_obs)
                mid_grid[tk][round(sample_ms)] = mid_obs

            if gov is None:
                skips[SKIP_BEFORE_FIRST_PANEL] = skips.get(SKIP_BEFORE_FIRST_PANEL, 0) + 1
                continue
            _tick_ms, panel, tick_id = gov
            if tk not in panel:
                skips[SKIP_NOT_IN_PANEL] = skips.get(SKIP_NOT_IN_PANEL, 0) + 1
                continue
            if not state.publishable:
                skips[SKIP_BOOK_UNPUBLISHABLE] = skips.get(SKIP_BOOK_UNPUBLISHABLE, 0) + 1
                continue

            view = F.BookView(market_ticker=tk, bids=dict(book.yes),
                              asks=dict(book.no),
                              last_modified_ms=None, sample_time_ms=sample_ms)
            meta = markets[tk]
            tte = (meta.occurrence_datetime.timestamp() * 1000 - sample_ms) / 1000.0
            m0 = F.compute_m0(view, seconds_to_close=tte)
            m1 = F.compute_m1_flow(acc[tk], sample_ms, trade_lag_ms=trade_lag_ms)

            rows.append({
                "session_id": session_id, "panel_tick_id": tick_id,
                "ticker": tk, "series": meta.series,
                "event_id": meta.occurrence_datetime.isoformat(),
                "subscription_generation": state.subscription_generation,
                "sample_time": datetime.fromtimestamp(
                    sample_ms / 1000, tz=timezone.utc).isoformat(),
                "sample_time_ms": round(sample_ms),
                "TTE_seconds": round(tte, 3),
                "capture_commit": capture_commit,
                "feature_schema_version": ROW_SCHEMA_VERSION,
                "label_schema_version": LABEL_SCHEMA_VERSION,
                "preregistration_version": preregistration_version,
                "dataset_role": dataset_role,
                "session_status": session_status,
                "m0": {k: m0[k] for k in F.M0_FEATURES},
                "controls": {k: m0[k] for k in F.M0_CONTROLS},
                "m1_flow": m1,
            })
        si += 1

    # ---- pass 2: labels, from the completed published-mid grid -------------
    for r in rows:
        r["labels"] = {
            str(h): lab.to_dict() for h, lab in L.compute_labels(
                t_ms=r["sample_time_ms"], mid_at_t=r["m0"]["mid"],
                mid_grid=mid_grid[r["ticker"]],
                session_end_ms=session_end_ms).items()}

    report = {
        "milestone": "MARKET-MICROSTRUCTURE-ROW-BUILDER-001",
        "session_id": session_id, "dataset_role": dataset_role,
        "row_schema_version": ROW_SCHEMA_VERSION,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "trade_lag_ms": trade_lag_ms,
        "records_on_tape": len(records),
        "sample_instants": len(samples),
        "row_opportunities": len(samples) * len(tickers),
        "rows_emitted": len(rows),
        "skips": skips,
        "dispatch_errors": dispatch_errors,
        "session_open": datetime.fromtimestamp(session_open_ms / 1000, tz=timezone.utc).isoformat(),
        "session_end": datetime.fromtimestamp(session_end_ms / 1000, tz=timezone.utc).isoformat(),
        "label_coverage": {str(k): v for k, v in L.coverage(rows).items()},
        "m0_completeness": _completeness(rows, "m0", F.M0_FEATURES),
        "m1_completeness": _completeness(rows, "m1_flow", F.M1_FLOW_FEATURES),
        "unique_market_session_clusters": len({(r["session_id"], r["ticker"])
                                               for r in rows}),
        "panel_ticks_represented": len({r["panel_tick_id"] for r in rows}),
        "tte_bins_represented": sorted({_bin(r["TTE_seconds"]) for r in rows}),
    }
    return {"rows": rows, "report": report}


def _bin(tte):
    from app.microstructure.panel import tte_bin
    return tte_bin(tte)


def _completeness(rows, block, names) -> dict:
    if not rows:
        return {}
    return {n: round(sum(1 for r in rows if r[block].get(n) is not None)
                     / len(rows), 4) for n in names}
