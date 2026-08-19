"""KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001 (P4) — the production capture.

**Strictly observational.** Market-data channels only. No order, cancel,
portfolio mutation, private fill/order channel, capital or execution-module
dependency exists here; the only frames that reach the socket are the
`subscribe` the shipped collector builds and, on a real orderbook fault, its own
`get_snapshot`. The collector runs **exactly as shipped** — this file adds a
delegating tap that cannot drop, reorder, retry or synthesise a frame, and a
connector wrapper that reads the TLS peer's identity and then hands the
connection straight through.

## The one thing this file exists to do that a DEMO probe never had to

**Prove the tape is production before a single frame is accepted as production
data.** A tape mislabelled `production` would poison the qualification it exists
to establish, and the label is written by us, not by the venue — `environment`
is a `COLLECTOR_FACT` in the record envelope
(`KALSHI-TAPE-MEASUREMENT-CONTRACT-001` §4.1). So the label is earned by an
ordered evidence chain, every link of which HALTS rather than degrades:

| # | link | what it establishes | failure |
|---|---|---|---|
| E1 | host constant | the collector's `WS_HOSTS[production]` is the host the AsyncAPI spec publishes, and is not the demo constant | HALT |
| E2 | DNS | the production and demo hosts are separate names, and both resolve; the addresses are recorded, never asserted (a CDN may legitimately share them) | recorded |
| E3 | TLS, out of band | the certificate presented for the production WS host covers it and covers **no** demo name | HALT |
| E4 | credential, production identity store | an RSA-PSS-signed GET to the **production** REST `/trade-api/v2/api_keys` returns the installed key id with scopes exactly `["read"]`. A demo key is not in a production account and a production host will not authenticate it | HALT |
| E5 | TLS, on the capture socket itself | the same certificate identity, read from the socket the frames actually arrive on — so E3 cannot be satisfied by one connection while frames arrive on another | HALT, before any frame is read |
| E6 | universe | the subscribed tickers come from a production REST census, and are absent from the demo REST host | recorded |

E5 is the load-bearing one. E3 proves *a* production host exists; E5 proves
*this socket* is it, and it is checked inside the connector, before the
collector's first `recv()`.

**What this chain does NOT establish**, stated so nobody infers it: that the
production venue's behaviour matches the demo venue's, that the frames are
representative of any other hour, or that the host clock offset is
characterised. Those are measurements, and they are in the artifact.

## What it measures, and what it refuses to measure

Governed by `KALSHI-TAPE-MEASUREMENT-CONTRACT-001` (P3). Quantities the
contract types as unmeasurable stay unmeasurable in the artifact:

* `ticker` is **unsequenced**. Its sequence census is emitted as the string
  `NOT_MEASURABLE:empty_sequence_domain`, never as `0` — a zero there is an
  arithmetic artefact of an empty domain, not an observation (P3 §3.2).
* `recoveries` and `generation_advances` are `NOT_RECONSTRUCTABLE_BY_DESIGN`
  from the tape; the live numbers are recorded here because this is the only
  place they exist (P3 §8.2).
* the venue-to-receive offset is **contaminated by an uncharacterised host
  clock offset** and is reported under that name, never as a latency (P3 §8.5).
* the per-sid census this file keeps is **generation-blind** and is labelled as
  such; the authoritative, generation-aware numbers are the collector's own
  `SubscriptionState.stats` (P3 §3.2).
* spread is read only off frames the contract says carry one, and a ladder that
  was never transmitted is `NOT_PROVIDED`, not a level count of zero
  (P3 §5.1, §6.2, doctrine 10).

**Replay equality is NOT computed here and must not be.** B3 of the contract is
open: `archive.replay()` skips a non-orderbook frame's `seq` and manufactures a
gap that never happened. Capture is authorized; the replay-equality verdict is
not.

    python scripts/kalshi_prod_capture_p4.py evidence --json
    python scripts/kalshi_prod_capture_p4.py capture \\
        --archive-root ROOT --tickers-file F --label L --out OUT --max-seconds N
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import ssl
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# Imported at module scope, NOT inside `capture()`. These are only needed to
# BUILD the artifact, which happens after the session has already run -- so a
# late ImportError would destroy the record of a completed production capture
# rather than refusing before one started. The import must fail at second 0.
from kalshi_collector_p0_wire_probe import WireRecorder, _plain  # noqa: E402
from kalshi_cp6_cp9_functional_probe import capture_state  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.realtime.auth import ReadOnlyRequestSigner  # noqa: E402
from app.realtime.collector import (  # noqa: E402
    CollectorConfig,
    _Session,
    load_observer_signer,
)
from app.realtime.collector_metrics import CollectorMetrics  # noqa: E402
from app.realtime.credential_audit import audit_scopes  # noqa: E402
from app.realtime.kalshi import (  # noqa: E402
    ENV_DEMO,
    ENV_PRODUCTION,
    REST_HOSTS,
    WS_HOSTS,
)
from app.realtime.ws_transport import KalshiWebsocketTransport  # noqa: E402

ENVIRONMENT = ENV_PRODUCTION

# The host the venue's own machine-readable AsyncAPI spec publishes. Written
# down here as an INDEPENDENT constant rather than read from `WS_HOSTS`: this
# assertion exists to catch `WS_HOSTS` itself being wrong, and a check that
# reads its subject cannot fail.
SPEC_PRODUCTION_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
SPEC_PRODUCTION_REST = "https://api.elections.kalshi.com/trade-api/v2"

# Any of these appearing in a certificate the production capture is talking to
# means we are on the sandbox. Substrings, so a wildcard SAN cannot hide one.
DEMO_NAME_MARKERS = ("demo.kalshi.co", "demo-api.kalshi.co", ".demo.")

BOUNDARY_NOTE = (
    "OBSERVE_ONLY: authenticated read-only Kalshi production market-data "
    "observation. Subscriptions only, over a closed allowlist; no order, "
    "position, wallet, key-management or write-scoped surface is reachable "
    "from this script's import closure.")


class ProductionEvidenceError(RuntimeError):
    """The tape cannot be labelled production. A HALT, never a downgrade."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host_of(url: str) -> str:
    return url.split("://", 1)[1].split("/", 1)[0]


# ================================================================================
# E1-E3 + E6 — endpoint evidence, gathered WITHOUT a credential
# ================================================================================


def tls_evidence(hostname: str, port: int = 443) -> dict:
    """One TLS handshake, out of band, purely to read the peer's identity.

    Stdlib only and `create_default_context()` — certificate verification and
    hostname checking are ON, so a handshake that returns at all has already
    proven the peer holds a chain a public trust store accepts for this name.
    The parsed certificate is then recorded so the claim is auditable rather
    than implied by the absence of an exception.
    """
    record: dict = {"hostname": hostname, "port": port, "at": _now()}
    try:
        record["resolved_addresses"] = sorted(
            {info[4][0] for info in socket.getaddrinfo(hostname, port,
                                                       proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        record["resolved_addresses"] = None
        record["dns_error"] = type(exc).__name__
        return record

    context = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=20) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as tls:
                cert = tls.getpeercert()
                record["tls_version"] = tls.version()
                record["cipher"] = tls.cipher()[0] if tls.cipher() else None
                record["peer_address"] = tls.getpeername()[0]
    except (OSError, ssl.SSLError) as exc:
        record["tls_error"] = type(exc).__name__
        # The REASON, not only the type. No credential is in scope in this
        # function — no signed header, no URL carrying one — so the message
        # carries nothing that must not be logged, and without it a HALT
        # cannot distinguish "this host is not who it claims to be" from
        # "this machine has no CA bundle installed".
        record["tls_error_detail"] = str(exc)
        record["local_trust_store"] = {
            "cafile": ssl.get_default_verify_paths().cafile,
            "capath": ssl.get_default_verify_paths().capath,
        }
        return record

    subject = {k: v for entry in (cert.get("subject") or ())
               for k, v in entry}
    issuer = {k: v for entry in (cert.get("issuer") or ()) for k, v in entry}
    record.update({
        "certificate_subject": subject,
        "certificate_issuer": issuer,
        "subject_alt_names": sorted({v for k, v in (cert.get("subjectAltName") or ())
                                     if k == "DNS"}),
        "not_before": cert.get("notBefore"),
        "not_after": cert.get("notAfter"),
        "serial_number": cert.get("serialNumber"),
        "verified_by_default_trust_store": True,
        "hostname_checked": True,
    })
    return record


def _san_covers(names, hostname: str) -> bool:
    """Does this SAN set cover the hostname, wildcards included?"""
    for name in names or ():
        if name == hostname:
            return True
        if name.startswith("*.") and hostname.count(".") == name.count("."):
            if hostname.endswith(name[1:]):
                return True
    return False


def endpoint_evidence() -> dict:
    """E1-E3. Returns the evidence and the findings; NEVER decides silently."""
    collector_ws = WS_HOSTS[ENV_PRODUCTION]
    demo_ws = WS_HOSTS[ENV_DEMO]
    prod_host = _host_of(collector_ws)
    demo_host = _host_of(demo_ws)

    findings: list = []
    if collector_ws != SPEC_PRODUCTION_WS:
        findings.append(
            f"E1 the collector would dial {collector_ws!r}, which is not the "
            f"host the AsyncAPI spec publishes ({SPEC_PRODUCTION_WS!r})")
    if collector_ws == demo_ws:
        findings.append("E1 the production and demo WS constants are equal")
    if REST_HOSTS[ENV_PRODUCTION] != SPEC_PRODUCTION_REST:
        findings.append(
            f"E1 the production REST host is {REST_HOSTS[ENV_PRODUCTION]!r}, "
            f"not {SPEC_PRODUCTION_REST!r}")

    ws_tls = tls_evidence(prod_host)
    rest_tls = tls_evidence(_host_of(REST_HOSTS[ENV_PRODUCTION]))
    demo_tls = tls_evidence(demo_host)

    for label, record, host in (("production WS", ws_tls, prod_host),
                                ("production REST", rest_tls,
                                 _host_of(REST_HOSTS[ENV_PRODUCTION]))):
        if "subject_alt_names" not in record:
            findings.append(
                f"E3 no certificate could be read for the {label} host "
                f"{host!r} ({record.get('tls_error') or record.get('dns_error')})")
            continue
        if not _san_covers(record["subject_alt_names"], host):
            findings.append(
                f"E3 the {label} certificate does not cover {host!r}: "
                f"{record['subject_alt_names']}")
        contaminated = [n for n in record["subject_alt_names"]
                        if any(m in n for m in DEMO_NAME_MARKERS)]
        if contaminated:
            findings.append(
                f"E3 the {label} certificate also covers demo names "
                f"{contaminated} — this host is not exclusively production")

    return {
        "gate": "endpoint_evidence",
        "at": _now(),
        "collector_would_dial": collector_ws,
        "spec_production_ws": SPEC_PRODUCTION_WS,
        "demo_ws_constant": demo_ws,
        "production_rest_host": REST_HOSTS[ENV_PRODUCTION],
        "demo_rest_host": REST_HOSTS[ENV_DEMO],
        "hosts_are_distinct_names": prod_host != demo_host,
        "production_ws_tls": ws_tls,
        "production_rest_tls": rest_tls,
        # Recorded for contrast, and because "the demo host is a different
        # certificate" is part of what makes the production one meaningful.
        "demo_ws_tls_for_contrast": demo_tls,
        "findings": findings,
        "passed": not findings,
    }


# ================================================================================
# E4 — the credential must belong to the PRODUCTION identity store
# ================================================================================


def prove_read_only_production() -> tuple:
    """One signed GET to the PRODUCTION key-metadata route. Halts otherwise.

    Modelled on `kalshi_collector_p0_wire_probe.prove_read_only`, with the
    environment fixed to production rather than inherited. `audit_scopes` halts
    unless the installed key id appears **in this account's** key list with
    scopes exactly `["read"]` — which is why this doubles as production
    evidence: a demo key is not in a production account, and the production
    host will not authenticate a signature made by a key it does not hold.

    No key material is read, copied, printed or returned. The audit carries a
    key-id FINGERPRINT and nothing else.
    """
    import httpx

    settings = get_settings()
    key_id = (settings.kalshi_observer_api_key_id or "").strip()
    credential_path = (settings.kalshi_observer_credential_path or "").strip()
    if not key_id or not credential_path:
        raise ProductionEvidenceError(
            "refused: KALSHI_OBSERVER_API_KEY_ID and/or "
            "KALSHI_OBSERVER_CREDENTIAL_PATH are not set on this host")

    bootstrap = ReadOnlyRequestSigner.for_scope_audit(
        key_id=key_id, credential_path=credential_path, environment=ENVIRONMENT)
    base = REST_HOSTS[ENVIRONMENT].rsplit("/trade-api/v2", 1)[0]
    seen: dict = {}

    def fetch(path, headers):
        with httpx.Client(timeout=20.0) as client:
            response = client.get(base + path, headers=dict(headers))
            seen["status_code"] = response.status_code
            # The URL the signature was actually presented to, recorded so the
            # audit's "production" label is checkable and not asserted.
            seen["request_host"] = response.request.url.host
            seen["http_version"] = response.http_version
            response.raise_for_status()
            return response.json()

    try:
        audit = audit_scopes(signer=bootstrap, key_id=key_id,
                             environment=ENVIRONMENT, fetch=fetch,
                             timestamp_ms=int(time.time() * 1000))
    except Exception as exc:
        # A HALT stays a HALT. What is added here is only the RECORD of what
        # was learned on the way to it, because "the audit failed" and "the
        # audit reached the production account and found the key order-capable"
        # are different findings and an operator must be able to tell them
        # apart. `audit_scopes` remains the only verdict path; nothing here
        # re-reads the metadata or reconstructs a decision from it.
        raise ProductionEvidenceError(json.dumps({
            "gate": "credential_production_identity",
            "passed": False,
            "halt": f"{type(exc).__name__}: {exc}",
            "metadata_route_host": seen.get("request_host"),
            "metadata_route_status": seen.get("status_code"),
            "metadata_route_http_version": seen.get("http_version"),
            "key_id_fingerprint": None,
            "credential_path_basename": Path(credential_path).name,
            "key_material_read_by_this_script": False,
            "what_the_halt_establishes": (
                "the reached-host and status fields say whether the signature "
                "was accepted by the production identity store BEFORE the "
                "scope check refused the key"),
        }, sort_keys=True)) from None
    signer = load_observer_signer(environment=ENVIRONMENT,
                                  reported_scopes=list(audit.scopes))
    evidence = {
        "gate": "credential_production_identity",
        "environment": audit.environment,
        "key_id_fingerprint": audit.key_id_fingerprint,
        "scopes": list(audit.scopes),
        "proven_read_only": audit.proven_read_only,
        "verified_at": audit.verified_at,
        "detail": audit.detail,
        "metadata_route_host": seen.get("request_host"),
        "metadata_route_status": seen.get("status_code"),
        "metadata_route_http_version": seen.get("http_version"),
        "credential_path_basename": Path(credential_path).name,
        "key_material_read_by_this_script": False,
        "passed": (audit.proven_read_only
                   and seen.get("request_host") == _host_of(SPEC_PRODUCTION_REST)),
    }
    if not evidence["passed"]:
        raise ProductionEvidenceError(
            "the key-metadata audit did not resolve against the production "
            f"REST host (host={seen.get('request_host')!r})")
    return signer, evidence


# ================================================================================
# E5 — the certificate on the socket the frames arrive on
# ================================================================================


class ProductionVerifyingConnector:
    """Wraps `websockets.connect` and refuses a non-production peer.

    The check runs **between** the handshake completing and the connection
    being handed to the collector, so no frame can be read — let alone
    archived — from a socket whose peer identity has not been established.
    A refusal closes the connection and raises; it never downgrades to a
    warning, because a warning on a mislabelled tape is indistinguishable from
    a clean run six months later.
    """

    def __init__(self, expected_host: str) -> None:
        self._expected_host = expected_host
        self.connections: list = []

    async def __call__(self, uri, **kwargs):
        from websockets.asyncio.client import connect as _connect

        if uri != WS_HOSTS[ENV_PRODUCTION]:
            raise ProductionEvidenceError(
                f"refused: the transport asked to dial {uri!r}, which is not "
                f"the production host constant")
        conn = await _connect(uri, **kwargs)
        record = self._socket_evidence(conn, uri)
        self.connections.append(record)
        if not record["passed"]:
            await conn.close()
            raise ProductionEvidenceError(
                "E5 refused: " + "; ".join(record["findings"]))
        return conn

    def _socket_evidence(self, conn, uri: str) -> dict:
        findings: list = []
        # Every read below is defensive: these are library internals, and a
        # fabricated value would be worse than a null (P3 §8.4).
        transport = getattr(conn, "transport", None)

        def extra(name):
            try:
                return transport.get_extra_info(name)
            except Exception:
                return None

        cert = extra("peercert")
        cipher = extra("cipher")
        peername = extra("peername")
        response = getattr(conn, "response", None)
        headers = getattr(response, "headers", None)

        sans = None
        if isinstance(cert, dict):
            sans = sorted({v for k, v in (cert.get("subjectAltName") or ())
                           if k == "DNS"})
            if not _san_covers(sans, self._expected_host):
                findings.append(
                    f"the peer certificate on the capture socket does not "
                    f"cover {self._expected_host!r}: {sans}")
            contaminated = [n for n in sans
                            if any(m in n for m in DEMO_NAME_MARKERS)]
            if contaminated:
                findings.append(
                    f"the peer certificate on the capture socket covers demo "
                    f"names {contaminated}")
        else:
            # `getpeercert()` returns {} when the socket is not verified. A
            # missing certificate is not evidence of a good one.
            findings.append(
                "no peer certificate could be read from the capture socket; "
                "production identity is therefore UNESTABLISHED on this socket")

        return {
            "uri": uri,
            "at": _now(),
            "expected_host": self._expected_host,
            "peer_certificate_subject_alt_names": sans,
            "peer_certificate_issuer": (
                {k: v for entry in (cert.get("issuer") or ()) for k, v in entry}
                if isinstance(cert, dict) else None),
            "peer_certificate_not_after": (cert.get("notAfter")
                                           if isinstance(cert, dict) else None),
            "tls_cipher": cipher[0] if cipher else None,
            "peer_address": peername[0] if peername else None,
            "handshake_response_status": getattr(response, "status_code", None),
            "handshake_response_headers": (
                {k.lower(): v for k, v in headers.raw_items()}
                if headers is not None and hasattr(headers, "raw_items") else None),
            "findings": findings,
            "passed": not findings,
        }


# ================================================================================
# the tap — delegating, and it measures ARRIVAL, which DEMO could not
# ================================================================================


class RateRecorder:
    """Per-frame arrival, kept compact enough to run for an hour.

    One tuple per frame: monotonic nanoseconds, event type, sid, whether a
    `seq` was present, and the market ticker. Wall-clock is taken once at the
    start; everything else is a monotonic delta, because `received_monotonic_ns`
    is a duration instrument and nothing else (P3 §4.1).
    """

    def __init__(self) -> None:
        self.t0_monotonic_ns: int | None = None
        self.t0_wall: str | None = None
        self.rows: list = []
        self.per_market: Counter = Counter()
        self.per_market_orderbook: Counter = Counter()
        self.venue_offset_ms_contaminated: list = []
        # Snapshot spreads and ticker spreads are kept APART. They are
        # different observations: a snapshot's spread is `full_ladder` depth
        # and a ticker's is `top_of_book_only` (P3 s5.2, s6.3). Pooling them
        # would produce one distribution describing two quantities, which is
        # the same defect as `depth` once being hardcoded to `full_ladder`.
        self.spread_samples_snapshot: list = []
        self.spread_samples_ticker: list = []
        self.ladder_presence_census: Counter = Counter()
        self.venue_field_names: Counter = Counter()

    def observe(self, message: object) -> None:
        now_ns = time.monotonic_ns()
        if self.t0_monotonic_ns is None:
            self.t0_monotonic_ns = now_ns
            self.t0_wall = _now()
        if type(message) is not dict:
            self.rows.append((now_ns - self.t0_monotonic_ns, "__not_a_dict__",
                              None, False, None))
            return
        raw_type = message.get("type")
        event_type = raw_type if type(raw_type) is str else "__no_type__"
        sid = message.get("sid")
        sid = sid if isinstance(sid, int) and not isinstance(sid, bool) else None
        seq_present = isinstance(message.get("seq"), int) and not isinstance(
            message.get("seq"), bool)
        msg = message.get("msg")
        msg = msg if isinstance(msg, dict) else {}
        ticker = msg.get("market_ticker")
        ticker = ticker if isinstance(ticker, str) else None

        self.rows.append((now_ns - self.t0_monotonic_ns, event_type, sid,
                          seq_present, ticker))
        if ticker:
            self.per_market[ticker] += 1
            if event_type in ("orderbook_snapshot", "orderbook_delta"):
                self.per_market_orderbook[ticker] += 1

        # Venue-to-receive offset. NOT a latency: it carries an uncharacterised
        # host clock offset and is named for what it is (P3 §8.5).
        venue_ms = msg.get("ts_ms")
        if isinstance(venue_ms, int) and not isinstance(venue_ms, bool):
            self.venue_offset_ms_contaminated.append(
                time.time() * 1000.0 - float(venue_ms))

        if event_type == "orderbook_snapshot":
            self._observe_snapshot(msg)
        elif event_type == "ticker":
            self._observe_ticker(msg)

    def _observe_snapshot(self, msg: dict) -> None:
        # Doctrine 10 / P3 §5.1: an omitted ladder is NOT_PROVIDED, an
        # explicitly empty one is EMPTY, and the two are never merged.
        def state(key):
            if key not in msg or msg[key] is None:
                return "NOT_PROVIDED"
            return "EMPTY" if not msg[key] else "PRESENT"

        yes_state, no_state = state("yes_dollars_fp"), state("no_dollars_fp")
        self.ladder_presence_census[f"yes={yes_state} no={no_state}"] += 1
        if yes_state != "PRESENT" or no_state != "PRESENT":
            return
        try:
            # Both ladders arrive on the YES price scale and the NO side is the
            # YES ask with NO complement applied (P3 §5.1). Reversing that is
            # the two-cent error the contract records.
            #
            # `Decimal`, not `float`: the contract refuses float in the
            # canonical encoder because a value written and re-read as Decimal
            # re-serialises differently. A statistic computed off the same
            # values has no reason to be looser than the tape that carries them.
            best_bid = max(Decimal(str(level[0]))
                           for level in msg["yes_dollars_fp"])
            best_ask = min(Decimal(str(level[0]))
                           for level in msg["no_dollars_fp"])
        except (TypeError, ValueError, IndexError, InvalidOperation):
            return
        self.spread_samples_snapshot.append(best_ask - best_bid)

    def _observe_ticker(self, msg: dict) -> None:
        # Which field NAME supplied the value is itself an observation
        # (P3 §5.4), so the two spellings are counted rather than collapsed.
        for bid_key, ask_key in (("yes_bid_dollars", "yes_ask_dollars"),
                                 ("yes_bid", "yes_ask")):
            if msg.get(bid_key) is None or msg.get(ask_key) is None:
                continue
            try:
                spread = (Decimal(str(msg[ask_key])) - Decimal(str(msg[bid_key])))
            except (TypeError, ValueError, InvalidOperation):
                return
            self.spread_samples_ticker.append(spread)
            self.venue_field_names[f"{bid_key}/{ask_key}"] += 1
            return


class RateTap:
    """The P0 tap plus arrival timing. Cannot drop, reorder or synthesise."""

    def __init__(self, inner, wire: WireRecorder, rates: RateRecorder,
                 journal: list) -> None:
        self._inner = inner
        self._wire = wire
        self._rates = rates
        self._journal = journal

    @property
    def counters(self):
        return self._inner.counters

    @property
    def queue_depth(self):
        return self._inner.queue_depth

    @property
    def backpressure_active(self):
        return self._inner.backpressure_active

    @property
    def last_close(self):
        return self._inner.last_close

    @property
    def connected(self) -> bool:
        return self._inner.connected

    async def connect(self) -> None:
        await self._inner.connect()
        self._journal.append({"event": "connected", "at": _now()})

    async def send(self, message) -> None:
        self._wire.on_command(message)
        await self._inner.send(message)

    async def close(self) -> None:
        await self._inner.close()
        self._journal.append({"event": "closed", "at": _now()})

    def __aiter__(self):
        return self._tap()

    async def _tap(self):
        async for message in self._inner:
            self._wire.observe(message)
            self._rates.observe(message)
            yield message


# ================================================================================
# analysis — every quantity carries its epistemic class
# ================================================================================


def _spread_summary(samples) -> dict:
    """A spread distribution, or a typed absence. Never a zero-filled one."""
    if not samples:
        return {"n": 0,
                "state": "NOT_MEASURABLE:no_frame_of_this_kind_carried_both_sides"}
    ordered = sorted(samples)
    return {
        "n": len(ordered),
        "min": str(ordered[0]),
        "median": str(ordered[len(ordered) // 2]),
        "max": str(ordered[-1]),
        "locked_or_crossed_samples": sum(1 for s in ordered if s <= 0),
    }


def _percentiles(values, points=(50, 90, 95, 99)) -> dict:
    """Percentiles, or a typed refusal below the sample floor (P3 §8.5)."""
    floors = {50: 3, 90: 20, 95: 20, 99: 100}
    ordered = sorted(values)
    out: dict = {}
    for point in points:
        if len(ordered) < floors[point]:
            out[f"p{point}"] = (
                f"NOT_MEASURABLE:below_min_samples("
                f"n={len(ordered)}<{floors[point]})")
            continue
        index = min(len(ordered) - 1,
                    max(0, int(round((point / 100.0) * len(ordered))) - 1))
        out[f"p{point}"] = ordered[index]
    return out


def analyse(rates: RateRecorder, wire: WireRecorder) -> dict:
    rows = rates.rows
    if not rows:
        return {"frames": 0,
                "note": "no frame arrived; every rate below is an empty domain"}

    span_s = (rows[-1][0] - rows[0][0]) / 1e9
    interarrival_ms = [(rows[i][0] - rows[i - 1][0]) / 1e6
                       for i in range(1, len(rows))]

    # Per-second frame counts over the WHOLE span, zeros included: dropping the
    # quiet seconds is how a bursty stream is made to look steady.
    buckets = Counter(int(row[0] // 1_000_000_000) for row in rows)
    last_bucket = int(rows[-1][0] // 1_000_000_000)
    per_second = [buckets.get(i, 0) for i in range(last_bucket + 1)]
    mean_rate = statistics.fmean(per_second) if per_second else 0.0
    variance = statistics.pvariance(per_second) if len(per_second) > 1 else 0.0

    by_type = Counter(row[1] for row in rows)
    by_sid = Counter(row[2] for row in rows)

    # The DEMO comparison quantity. `KALSHI-TAPE-MANIFEST-001` measured DEMO as
    # four hyperactive markets, a 98.3x cliff, then a quasi-flat plateau.
    ranked = rates.per_market.most_common()
    counts = [c for _t, c in ranked]
    cliff = None
    if len(counts) > 1:
        ratios = [(counts[i] / counts[i + 1], i + 1)
                  for i in range(len(counts) - 1) if counts[i + 1] > 0]
        if ratios:
            ratio, position = max(ratios)
            cliff = {"largest_adjacent_ratio": round(ratio, 3),
                     "after_rank": position}

    return {
        "frames": len(rows),
        "span_seconds": round(span_s, 3),
        "frames_per_second_mean": round(len(rows) / span_s, 4) if span_s else None,
        "frames_per_second_peak_1s": max(per_second) if per_second else None,
        "frames_per_second_p50_1s": (statistics.median(per_second)
                                     if per_second else None),
        "silent_seconds": sum(1 for v in per_second if v == 0),
        "seconds_observed": len(per_second),
        # Index of dispersion. 1.0 is Poisson; >1 is bursty. Reported with its
        # own mean so a reader can see the regime it was computed in.
        "burstiness_index_of_dispersion": (round(variance / mean_rate, 4)
                                           if mean_rate > 0 else
                                           "NOT_MEASURABLE:zero_mean_rate"),
        "interarrival_ms": {
            **_percentiles(interarrival_ms),
            "max": round(max(interarrival_ms), 3) if interarrival_ms else None,
            "n": len(interarrival_ms),
        },
        "frames_by_type": dict(by_type),
        "frames_by_sid": {str(k): v for k, v in by_sid.items()},
        "activity_distribution": {
            "markets_with_at_least_one_frame": len(ranked),
            "top_10": ranked[:10],
            "bottom_10": ranked[-10:],
            "median_frames_per_market": (statistics.median(counts)
                                         if counts else None),
            "max_to_median_ratio": (round(counts[0] / statistics.median(counts), 3)
                                    if counts and statistics.median(counts) else None),
            "largest_cliff": cliff,
        },
        "orderbook_frames_per_market": rates.per_market_orderbook.most_common(),
        "ladder_presence_census": dict(rates.ladder_presence_census),
        "spread_dollars_by_depth_class": {
            "full_ladder_from_snapshots": _spread_summary(
                rates.spread_samples_snapshot),
            "top_of_book_only_from_ticker": _spread_summary(
                rates.spread_samples_ticker),
            "epistemic_note": (
                "TWO distributions, never pooled: a snapshot spread is "
                "`full_ladder` depth and a ticker spread is `top_of_book_only` "
                "(P3 s6.3). Each is computed ONLY from frames the contract says "
                "carry a spread -- a snapshot missing a ladder contributes "
                "nothing and is never read as a zero spread (P3 s6.2). Spread "
                "is never 0 by absence; a 0 here would be a genuinely locked "
                "market."),
        },
        "ticker_quote_field_names_observed": dict(rates.venue_field_names),
        "venue_to_receive_offset_contaminated_ms": {
            "n": len(rates.venue_offset_ms_contaminated),
            **_percentiles(rates.venue_offset_ms_contaminated),
            "host_clock_offset_characterised": False,
            "epistemic_note": (
                "NOT A LATENCY. Equals true_transit + (our_offset - their_"
                "offset) and the host clock offset is uncharacterised "
                "(P3 s8.5). Negatives are retained, never dropped."),
        },
        "per_sid_sequence_census_GENERATION_BLIND": {
            "note": (
                "generation-blind, and therefore NOT the authoritative fault "
                "count: the venue re-issues the same sids on every resubscribe "
                "so sid alone cannot separate epochs (P3 s3.2). The "
                "authoritative numbers are the collector's own "
                "SubscriptionState.stats in `live_terminal_state`."),
            "census": wire.to_dict()["per_sid_census"],
        },
    }


def typed_absences(wire: WireRecorder) -> dict:
    """The quantities the contract forbids reporting as numbers."""
    ticker_sids = sorted(
        str(sid) for sid, entry in wire.sids.items()
        if entry["types"].get("ticker") and entry["seq_absent"] == entry["frames"])
    return {
        "ticker_sequence_gaps": {
            "state": "NOT_MEASURABLE:empty_sequence_domain",
            "sids": ticker_sids,
            "why": (
                "the ticker channel carries no `seq`, so there is no sequence "
                "domain to have a gap in. Zero here would be an arithmetic "
                "artefact, not an observation (P3 s3.2)."),
        },
        "ticker_completeness": {
            "state": "NOT_MEASURABLE:no_loss_detector_exists",
            "why": "no seq, no gap detector, no repair route (P3 s3, L1).",
        },
        "recoveries_from_tape": {
            "state": "NOT_RECONSTRUCTABLE_BY_DESIGN",
            "why": ("`recoveries` counts an outbound collector action; the tape "
                    "records inbound venue messages only (P3 s8.2a). The LIVE "
                    "numbers are in `live_terminal_state`."),
        },
        "generation_advances_on_unsequenced_sid": {
            "state": "NOT_RECONSTRUCTABLE_BY_DESIGN",
            "why": ("dispatch advances an epoch only on a frame carrying a seq, "
                    "and no ticker frame does (P3 s8.2b). The per-record "
                    "generation stamps are all present and conserved."),
        },
        "transport_dropped_frames": {
            "state": "NOT_MEASURABLE:no_source_exists",
            "why": ("the installed websockets library has no drop path and no "
                    "drop counter; a zero would be fabricated (P3 s8.4)."),
        },
        "replay_equality": {
            "state": "NOT_QUALIFIED:B3_OPEN",
            "why": ("`archive.replay()` skips a non-orderbook frame's seq and "
                    "manufactures a gap that never happened. Capture is "
                    "authorized; the replay-equality verdict is not."),
        },
    }


# ================================================================================
# the run
# ================================================================================


def capture(*, tickers, channels, max_seconds, max_events, max_reconnects,
            archive_root, samples_per_type, label, out_path,
            read_timeout_s) -> int:
    started_wall = _now()

    # --- the evidence chain, in order, before any socket carries a frame -------
    endpoint = endpoint_evidence()
    if not endpoint["passed"]:
        raise ProductionEvidenceError(
            "endpoint evidence failed: " + "; ".join(endpoint["findings"]))
    signer, credential = prove_read_only_production()
    connector = ProductionVerifyingConnector(_host_of(WS_HOSTS[ENV_PRODUCTION]))

    wire = WireRecorder(samples_per_type=samples_per_type)
    rates = RateRecorder()
    journal: list = []
    transports: list = []

    def transport_factory():
        inner = KalshiWebsocketTransport(environment=ENVIRONMENT, signer=signer,
                                         read_timeout_s=read_timeout_s,
                                         _connector=connector)
        transports.append(inner)
        return RateTap(inner, wire, rates, journal)

    config = CollectorConfig(
        environment=ENVIRONMENT,
        archive_root=Path(archive_root),
        market_tickers=tuple(tickers),
        channels=tuple(channels),
        max_seconds=max_seconds,
        max_events=max_events,
        max_reconnects=max_reconnects,
        dry_run=False,
        validate_sequence=True,
    )
    metrics = CollectorMetrics(environment=ENVIRONMENT,
                               markets_subscribed=len(tickers))
    holder: dict = {}

    async def _go():
        session = _Session(config, transport_factory=transport_factory,
                           metrics=metrics)
        holder["session"] = session
        return await session.run()

    result = asyncio.run(_go())
    session = holder["session"]
    finished_wall = _now()

    payload = {
        "milestone": "KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001",
        "phase": "CAPTURE",
        "boundary_note": BOUNDARY_NOTE,
        "environment": ENVIRONMENT,
        "run_label": label,
        "started_at": started_wall,
        "finished_at": finished_wall,
        "production_evidence_chain": {
            "E1_E3_endpoint": endpoint,
            "E4_credential": credential,
            "E5_capture_socket": connector.connections,
            "verified_before_first_frame": True,
            "what_this_does_not_establish": [
                "that production behaviour matches demo behaviour",
                "that this hour is representative of any other hour",
                "that the host clock offset is characterised",
            ],
        },
        "config": {
            "market_tickers_count": len(tickers),
            "market_tickers": list(tickers),
            "channels": list(channels),
            "max_seconds": max_seconds,
            "max_events": max_events,
            "max_reconnects": max_reconnects,
            "archive_root": str(archive_root),
            "read_timeout_s": read_timeout_s,
        },
        "session_result": result.to_dict(),
        "subscription_epoch_final": session.subscription_epoch,
        "connection_generation_final": session.connection_generation,
        "metrics": {
            "events_received": metrics.events_received,
            "events_archived": metrics.events_archived,
            "events_rejected": metrics.events_rejected,
            "frames_malformed": metrics.frames_malformed,
            "append_calls": metrics.append_calls,
            "rotations_started": metrics.rotations_started,
            "segments_closed": metrics.segments_closed,
            "disconnects": metrics.disconnects,
            "reconnects": metrics.reconnects,
            "sequence_gaps": metrics.sequence_gaps,
            "sequence_regressions": metrics.sequence_regressions,
            "sequence_duplicates": metrics.sequence_duplicates,
            "subscription_generation": metrics.subscription_generation,
            "observe_errors": metrics.observe_errors,
            "event_bytes_total": metrics.event_bytes_total,
        },
        "wire_counters_per_connection": [t.counters.snapshot() for t in transports],
        "load": analyse(rates, wire),
        "typed_absences": typed_absences(wire),
        "wire": wire.to_dict(),
        "connection_journal": journal,
        "live_terminal_state": capture_state(session),
    }
    Path(out_path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")

    print(json.dumps({
        "run_label": label,
        "status": result.status,
        "events_received": result.events_received,
        "events_archived": result.events_archived,
        "events_rejected": result.events_rejected,
        "frames_malformed": result.frames_malformed,
        "sequence_faults": result.sequence_faults,
        "reconnects": result.reconnects,
        "segments_committed": result.segments_committed,
        "frames_per_second_mean": payload["load"].get("frames_per_second_mean"),
        "by_type": dict(wire.by_type),
        "out": str(out_path),
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evidence",
                        help="E1-E4 only. Opens no websocket, captures nothing.")
    ev.add_argument("--json", action="store_true")
    ev.add_argument("--skip-credential", action="store_true",
                    help="E1-E3 only; do not touch the credential at all")

    cap = sub.add_parser("capture", help="the bounded production capture")
    cap.add_argument("--archive-root", required=True)
    cap.add_argument("--tickers", default="")
    cap.add_argument("--tickers-file", default="")
    cap.add_argument("--channels", default="orderbook_delta,ticker,trade")
    cap.add_argument("--max-seconds", type=int, default=600)
    cap.add_argument("--max-events", type=int, default=1_000_000)
    cap.add_argument("--max-reconnects", type=int, default=4)
    cap.add_argument("--samples-per-type", type=int, default=8)
    cap.add_argument("--read-timeout-s", type=float, default=None)
    cap.add_argument("--label", required=True)
    cap.add_argument("--out", required=True)

    args = parser.parse_args()

    if args.command == "evidence":
        report = {"endpoint": endpoint_evidence()}
        if not args.skip_credential:
            try:
                _signer, credential = prove_read_only_production()
                report["credential"] = credential
            except Exception as exc:
                report["credential"] = {"passed": False,
                                        "error": f"{type(exc).__name__}: {exc}"}
        report["passed"] = all(
            section.get("passed") for section in report.values()
            if isinstance(section, dict))
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0 if report["passed"] else 1

    if args.tickers_file:
        raw = Path(args.tickers_file).read_text()
        tickers = [t for t in (x.strip() for x in raw.replace("\n", ",").split(","))
                   if t]
    else:
        tickers = [t for t in args.tickers.split(",") if t]
    if not tickers:
        raise SystemExit("refused: no market tickers; a session with no sample "
                         "frame measures nothing")

    return capture(
        tickers=tickers,
        channels=[c for c in args.channels.split(",") if c],
        max_seconds=args.max_seconds,
        max_events=args.max_events,
        max_reconnects=args.max_reconnects,
        archive_root=args.archive_root,
        samples_per_type=args.samples_per_type,
        label=args.label,
        out_path=args.out,
        read_timeout_s=(args.read_timeout_s if args.read_timeout_s is not None
                        else float(args.max_seconds + 120)))


if __name__ == "__main__":      # pragma: no cover - operator entry point
    sys.exit(main())
