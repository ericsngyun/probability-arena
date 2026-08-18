# The Kalshi production WebSocket endpoint — recorded, not resolved

**Milestone:** `KALSHI-PROD-QUAL-PRECAPTURE`, deliverable 3, for
`KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001`.
**Date:** 2026-08-17.
**Status of the host itself: still UNVERIFIED.** Nothing in this document was
established by connecting. No production socket has been opened, because there
is no production credential — `KALSHI_PRIVATE_KEY_PATH=` is set-but-empty on
EVO and the only key present is the DEMO observer credential.

This is a doctrine-8 situation: **an endpoint is a claim about the venue**, and
a name is not evidence of its semantics. So the two hosts in play are recorded
side by side, the disagreement is flagged, and **no default is changed**.

---

## The two hosts

| | value | where |
|---|---|---|
| **what the collector would actually use** | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `app/realtime/kalshi.py`, `WS_HOSTS[ENV_PRODUCTION]` |
| **what EVO's `.env` records** | `wss://api.elections.kalshi.com/trade-api/ws/v2` | `KALSHI_WS_URL` in `.env` / `.env.example`; `app/config.py:38` default |
| **what Eric states is official** | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | the milestone task |

## The finding that makes this non-operative

**`settings.kalshi_ws_url` has no reader anywhere in the repository.**

Measured by grep over the whole tree: `kalshi_ws_url` occurs exactly once in
`app/` — its own definition at `app/config.py:38` — and `KALSHI_WS_URL` occurs
exactly once outside it, in `.env.example:20`. Nothing consumes either.

`KalshiWebsocketTransport.connect()` reads `WS_HOSTS[self._environment]`
(`app/realtime/ws_transport.py:527`), never the setting. So:

* the host the production capture would dial is
  `wss://external-api-ws.kalshi.com/trade-api/ws/v2`;
* the `.env` value is **vestigial configuration**, not an override, and
  changing it would change nothing;
* the disagreement is real but **cannot mis-route a capture**, because the
  losing value is unread.

That is stated as a measurement and pinned by a test
(`test_the_env_setting_still_has_no_reader`). If a reader is ever added, that
test fails — which is the point, because on that day the disagreement stops
being cosmetic.

## What the official documentation says

Consulted 2026-08-17 and reachable. Two independent official sources agree, and
they also explain why both hosts exist:

* **`https://docs.kalshi.com/asyncapi.yaml`** — the machine-readable AsyncAPI
  spec lists exactly one server: host `external-api-ws.kalshi.com`, pathname
  `/trade-api/ws/v2`, protocol `wss`, described as *"Production Trade API
  WebSocket server (encrypted connection only)"*.
* **`https://docs.kalshi.com/getting_started/quick_start_websockets`** — gives
  production as `wss://external-api-ws.kalshi.com/trade-api/ws/v2` and demo as
  `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`, and adds verbatim:
  *"The existing shared WebSocket hosts, `wss://api.elections.kalshi.com/trade-api/ws/v2`
  for production and `wss://demo-api.kalshi.co/trade-api/ws/v2` for demo,
  remain supported."*

**So the two values do not contradict each other.** `external-api-ws.kalshi.com`
is the current dedicated host and the one the spec publishes;
`api.elections.kalshi.com` is the legacy shared host, documented as still
supported. The repository's code default is the current one; the `.env` value
is the legacy one.

Both docs pages were fetched through a summarizing reader rather than read as
raw HTML, so the quotations above are the reader's rendering of the page. The
AsyncAPI file is the stronger of the two — it is the venue's own machine
artifact — and it names only the dedicated host.

## Recommendation — and what would justify a change

**Recommendation: change nothing today.**

1. **Do not change `WS_HOSTS[ENV_PRODUCTION]`.** It already matches the
   official spec and Eric's stated endpoint. There is nothing to change.
2. **Do not "fix" `.env` / `.env.example` / `app/config.py:38` as part of this
   milestone.** The value is unread, so editing it is a cosmetic change to a
   file on a shared production host, and touching EVO's `.env` for cosmetics
   is not worth the blast radius. It is also *not wrong* — it names a host
   Kalshi documents as supported.
3. **Prefer deleting the setting to correcting it**, if it is touched at all.
   A configuration knob that overrides nothing is worse than no knob: the next
   reader will assume setting it works. That is a small, separate change with
   its own review — not something to slip into a capture milestone.
4. **The host stays UNVERIFIED until a handshake succeeds.** The comment at
   `app/realtime/kalshi.py:52-55` is still correct and must not be edited to
   claim otherwise on the strength of documentation. §11 B1 of
   `KALSHI-TAPE-MEASUREMENT-CONTRACT-001` is closable only by an operator with
   a credential, and the first successful production handshake is the evidence
   that closes it. The pre-capture preflight reports
   `verified_on_the_wire: false` for exactly this reason.

## What could not be determined

* **Whether the dedicated host and the legacy host behave identically** — sid
  assignment, sequencing, error framing, close behaviour. Documentation says
  both are supported; it does not say they are the same server. Only a capture
  can settle that, and if the two are ever both used, the tape must record
  which one produced it.
* **Whether a read-scoped production credential authenticates against the
  dedicated host at all.** Untestable without the credential.
* **Whether `.env` on EVO differs from `.env.example`.** Not inspected: this
  worktree is isolated and the task forbids disturbing EVO. The value quoted
  above is the one the task supplied and the one `.env.example` carries.
