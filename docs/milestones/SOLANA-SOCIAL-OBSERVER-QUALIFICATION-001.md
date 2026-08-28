# SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001

**Status: FUNNEL AND GUARDS BUILT. NOT A LIVE RUN — live social ingestion is
not yet authorized.** Branch `solana-readonly-chain-adapter-001`, held off
`main` while S06 is armed against a pinned commit.

One question, and deliberately only one:

> Can we prospectively acquire live social artifacts and move them through the
> full evidence / identity / clock / on-chain funnel **without silently
> inventing provenance, timing, or token identity**?

Not whether any of it predicts anything.

## The funnel

```text
RECEIVED_SOCIAL → EVIDENCE_EXTRACTED → AUTHORITY_RESOLVED → CHAIN_VERIFIED
→ CANONICALLY_VERIFIED → LIVE_DELIVERY → CLOCK_COMPUTABLE
→ DOWNSTREAM_CHAIN_OBSERVATION → QUOTE_OBSERVATION → SOCIAL_FILL_JOINABLE
```

**Every artifact terminates in the next stage or a typed refusal.** An outcome
that records neither raises at construction — an artifact that simply stops
being mentioned would be indistinguishable from one never received, which is
the silent-wrongness shape this project keeps closing.

The report gives counts reached at each stage, where artifacts stopped, refusal
reasons, per-stage losses, and **the largest single loss** — because the shape
is the deliverable. If 30 chain-verified yields 3 authoritative sources, the
bottleneck is *provenance*, not RPC. If canonical-verified is 20 and joinable is
2, it is the clock or downstream observation. Either answer names the next
milestone.

## What it cannot compute

No return, markout, price response, win rate, source score, token ranking or
trading decision. `ArtifactOutcome` has no field for one, and an AST guard
fails the build if the code references any. Injecting a `price_response` list
or a `total_latency_us` key both fail.

Both latencies are reported and **never summed**: `delivery_us` is marked
contaminated (a platform clock we cannot audit), `pipeline_us` is not. An
absent latency is omitted, never zeroed.

## Composition: the consumer cannot bypass the adapter

Asserting the adapter's own surface is not enough. Tests now assert that
nothing reachable from the observer opens a transport, that only the adapter
holds an endpoint, that `chain_identity` **receives** a reader rather than
constructing one, that every whitelisted RPC method has exactly one typed
caller, and that the observer cannot reach `app.microstructure` at all.

## Three guard defects found while building these guards

1. **A prose literal condemned a correct module.** The funnel's own `note` —
   *"no return, markout, price response … is computed here"* — tripped the
   forbidden-concept scan. `astguard` excluded docstrings but not other prose
   literals. Now literals ≥40 chars containing a space are treated as prose, on
   the same principle: a module saying "no price is computed here" is asserting
   the property, not violating it. **Seventh instance of this pattern.**

2. **`socket` is not automatically a transport.** `app.seam.clock` imports it
   for `gethostname()`, to derive the host-identity half of the comparability
   key. Banning the import would condemn a module doing exactly the right
   thing, so the guard checks **calls**, and a separate test pins clock to
   `socket.gethostname` only.

3. **The network guard did not bite, and a mutation proved it.** It read `.id`
   off the call's base, which is a `Name` only for single-level calls like
   `socket.connect`. For `urllib.request.urlopen` the base is itself an
   `Attribute`, so `.id` was absent and the call escaped entirely — an injected
   `urlopen` **passed**. The guard now walks the attribute chain to its root.
   Both `urllib.request.urlopen` and `socket.create_connection` now fail it.

The third is the one worth remembering: **a guard that has never been mutated
is a guard whose coverage is unknown.** It looked correct, read correctly, and
caught nothing.

## Not done

* **No live social ingestion.** Sources, credentials and rate/cost limits are a
  separate authorization; nothing here connects to a platform.
* **No source universe frozen yet.** Selection must be on plumbing diversity —
  direct mint publications, no-mint posts, quotes, migrations, conflicting
  addresses, duplicated announcements, delivery modes — and explicitly **not**
  on historical forward returns.
* No subscriptions, no paid provider tier.

The next step after a live run would be `SOCIAL-ALPHA-FEASIBILITY-001`, a
bounded prospective count of joinable events — still with no forward returns —
to answer whether the corpus can support `SOCIAL-LEAD-LAG-001` at all.
