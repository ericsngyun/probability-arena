# QDK-001 — CLOB microstructure state engine and execution model (Kalshi-facing)

STATUS: RESEARCH ONLY. No production code, no trading, no live execution, no order
placement of any kind is designed, enabled or implied by this document. Everything here
is a specification on paper plus a literature verification ledger.

SCOPE: **Kalshi CLOB only.** Solana memecoins trade on AMMs and bonding curves. They have
no book, no queue and no maker/taker distinction in the sense used here, and are covered
by a separate track. Section 8 lists exactly which constructs below do NOT transfer.

VERIFICATION CONVENTION used throughout:

- **VERIFIED** — read from the primary source named, with the numbers quoted from it.
- **INFERRED** — a defensible conclusion of ours, or read from a secondary source. Not
  authoritative.
- **UNVERIFIED** — asserted somewhere but not confirmed against wire or paper. Treated as
  an open question, never as a design input.

---

## 0. How to read this document

## 1. Scope boundary — what a CLOB gives you that an AMM does not

## 2. The CLOB state vector — typed feature schema

### 2.1 Design rules for the schema
### 2.2 Typed absence
### 2.3 The schema

## 3. Order-flow imbalance

### 3.1 OFI (Cont–Kukanov–Stoikov) — verification and estimator
### 3.2 MLOFI — what deeper levels add
### 3.3 Queue imbalance as a one-tick-ahead predictor
### 3.4 What this means for Kalshi specifically

## 4. Microprice

### 4.1 The construction
### 4.2 Failure modes in thin and wide books
### 4.3 Recommendation for Kalshi

## 5. Hawkes processes for order flow — assessment

### 5.1 The model
### 5.2 Stability and estimation cost
### 5.3 Verdict

## 6. Execution modelling

### 6.1 The EV identity
### 6.2 C_execution from the actual book
### 6.3 P(fill | ...) — the fill-probability model
### 6.4 E[return post-fill | filled] — the markout model
### 6.5 The negative fill-probability/return relationship

## 7. The maker/taker decision rule

## 8. Does NOT transfer to AMMs

## 9. Realism pass — what our collector can actually feed

### 9.1 What Kalshi exposes
### 9.2 Feature-by-feature mapping
### 9.3 Features we must NOT put in the schema

## 10. Citation ledger

## 11. Open questions and next steps
