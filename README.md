# FlightDelayClaimResolver

An [Intelligent Contract](https://docs.genlayer.com) on [GenLayer](https://www.genlayer.com/) that adjudicates flight-delay compensation claims. Validators independently fetch live flight-status data from a configurable set of real, authoritative aviation data providers, reach consensus on the facts (via GenLayer's Optimistic Democracy / Equivalence Principle), and an LLM judges eligibility against those facts — all under strict, deterministic evidence validation.

> **Deployed contract:** `0xEF0A558F7411D1B18F9b6Cb76b8967eE55a2FE50`
> **Network:** / Studionet_
> Explorer (studionet): `https://explorer-studio.genlayer.com/address/0xEF0A558F7411D1B18F9b6Cb76b8967eE55a2FE50`
---

## What it does

1. A claimant (or anyone on their behalf) **files a claim** for a specific flight, booking, and passenger — `open_case`.
2. Once the flight has plausibly concluded, anyone can **trigger an assessment** — `assess_case`. Under the hood:
   - The **leader** validator fetches live flight-status data from the first active, configured provider that succeeds.
   - The **other validators** independently re-fetch and re-derive the same result and vote to agree/disagree (GenLayer's Equivalence Principle).
   - Once validators agree on the underlying facts (flight status, delay in minutes), an **LLM** judges eligibility against those facts under a strict, injection-resistant prompt, and validators cross-check that judgment too.
3. The claim resolves to one of three terminal-ish states: **ELIGIBLE**, **NOT_ELIGIBLE**, or **UNRESOLVED** (ambiguous — retryable, capped at 3 attempts, with an owner-only recovery path for stuck cases).

The contract only **adjudicates eligibility** — it does not hold funds or move any payment itself. A downstream escrow/payment contract can read `disposition` and `severity` from a resolved case and act on it.

## Why this needs GenLayer

Determining "is this claim eligible" requires:
- Fetching **live, external, authoritative data** (flight status) — no built-in oracle for this exists on ordinary chains.
- **Judgment**, not just a lookup: interpreting ambiguous cases (e.g. a cancelled flight with no stated cause) safely defaults to requiring human review rather than guessing.
- **Neutral consensus**: no single centralized backend or airline should be the sole arbiter of whether *their own* delay qualifies for compensation.

This is a canonical fit for GenLayer's [milestone/dispute adjudication](https://docs.genlayer.com/understand-genlayer-protocol/typical-use-cases) use case.

---

## Architecture

```
open_case()                      -- deterministic: validate + store claim
        |
        v  (>= 36h after scheduled departure)
assess_case()
        |
        v
  snapshot active providers + case data (deterministic, pre-nondet)
        |
        v
  ┌─────────────────────────────────────────────┐
  │  gl.vm.run_nondet_unsafe(leader_fn, ...)     │
  │                                               │
  │  for provider in providers (priority order):  │
  │     fetch flight status  ──▶  parse & validate │
  │     (first success wins; on failure, try next) │
  │                                               │
  │  independently verified evidence               │
  │        |                                       │
  │        v                                       │
  │  LLM eligibility judgment (strict JSON schema) │
  └─────────────────────────────────────────────┘
        |
        v
  validators independently re-run the same pipeline
  and must reach the identical (decision, evidence) tuple
        |
        v
  ELIGIBLE / NOT_ELIGIBLE / UNRESOLVED  (stored + hashed evidence)
```

### Provider registry (configurable data sources)

Rather than a single hardcoded endpoint, the contract owner manages a small **registry** of real flight-status providers:

- Ships with one real, well-documented provider pre-registered: **AviationStack** (`https://api.aviationstack.com/v1/flights`) — **inactive until an API key is set**.
- The owner can register up to 5 providers, each with its own priority. If the leader's first-choice provider is down or returns unusable data, it falls through to the next — deterministically, so every validator tries providers in the exact same order.
- Adding a genuinely different vendor later only requires implementing one parser function and registering its schema id — no redesign needed.

### Evidence & consensus integrity

- Flight identity (number + scheduled date) is verified **deterministically in Python**, before any LLM is involved — the LLM is never trusted to judge identity.
- Delay is measured at **arrival** (falling back to departure delay if unavailable), matching how most delay-compensation regimes measure delay.
- Every resolved decision is bound to a **SHA-256 evidence hash** that includes the case id, attempt number, chain id, contract address, provider id, and the canonicalized upstream evidence — so a proof cannot be replayed across cases, attempts, chains, or contract instances.
- A cancelled flight with no stated cause resolves to `UNRESOLVED` rather than the LLM guessing — the contract has no reliable signal for *why* a flight was cancelled, so it fails safe toward human review.

---

## Getting started (post-deploy)

The contract deploys with **zero constructor arguments** and its default provider **inactive** — it will never silently call a placeholder or unconfigured endpoint. You must explicitly configure a real API key before any claim can resolve to anything other than `UNRESOLVED`.

```bash
# 1. Get a real AviationStack API key: https://aviationstack.com
# 2. Activate the default provider with it
genlayer write \
  --contract 0xEF0A558F7411D1B18F9b6Cb76b8967eE55a2FE50 \
  --function set_provider_api_key \
  --args aviationstack "<YOUR_AVIATIONSTACK_KEY>"
```

`set_provider_api_key` automatically flips `active` to `true` once a non-empty key is set. Only the contract **owner** (the deployer) can call any provider-management or ownership method.

> ⚠️ **The API key becomes public.** GenLayer contract storage is fully readable on-chain (`gen_getContractState`), and this SDK has no mechanism for validator-side secret injection for web requests. Use a **low-privilege, rate/budget-capped key dedicated to this contract**, and rotate it with `set_provider_api_key` if it's ever abused. This is a fundamental tradeoff of bridging an authenticated off-chain API from an on-chain contract, not a bug in this implementation.

### Adding a second/backup provider

```bash
genlayer write --contract <address> --function add_provider \
  --args "<provider_id>" "<display name>" "AVIATIONSTACK_V1" "https://<host>/v1/flights" "<api_key>" 1
```
`provider_id`: lowercase, 2–40 chars, `[a-z0-9_-]`. `priority`: lower number = tried first. Max 5 providers.

---

## Usage

### File a claim

```bash
genlayer write --contract <address> --function open_case --args \
  "claim-2024-0001" "BA100" "Jane Doe" "ABC123" "2024-06-01" "LHR" "JFK"
```

### Assess it (once the flight has had time to conclude)

`assess_case` will revert with `"flight has not had sufficient time to reach a final status"` until **36 hours after the start of the scheduled UTC departure day** — this exists specifically so premature attempts don't burn through the retry budget before real data even exists.

```bash
genlayer write --contract <address> --function assess_case --args "claim-2024-0001" 1
```

### Read the outcome

```bash
genlayer call --contract <address> --function read_case --args "claim-2024-0001"
```

Returns a dict including `status`, `disposition`, `severity`, `delay_minutes`, `evidence_hash`, `provider_used`, and `earliest_assessment_at`.

### If unresolved

```bash
# wait 1h cooldown, then:
genlayer write --contract <address> --function retry_unresolved --args "claim-2024-0001" 1
genlayer write --contract <address> --function assess_case --args "claim-2024-0001" 2
```

Up to 3 total attempts. If every attempt is exhausted while genuinely stuck (e.g. a persistent provider outage), the owner can call `reset_case_attempts` — this **can never** touch a case that already resolved to `ELIGIBLE` or `NOT_ELIGIBLE`.

---

## Method reference

### Write methods

| Method | Access | Description |
|---|---|---|
| `open_case(case_id, flight_number, passenger_name, booking_reference, scheduled_departure_date, origin_airport, destination_airport)` | anyone | File a new claim. One claim per unique (flight, passenger+booking) combination. |
| `assess_case(case_id, expected_attempt)` | anyone | Trigger evidence-gathering + LLM eligibility assessment for the current attempt. |
| `retry_unresolved(case_id, expected_attempt)` | anyone | Re-open an `UNRESOLVED` case for another attempt (cooldown + attempt cap apply). |
| `expire_pending(case_id, expected_attempt)` | anyone | Move a long-neglected `PENDING` case (72h+) to `UNRESOLVED` so it enters the retry cycle. |
| `reset_case_attempts(case_id)` | owner | Emergency recovery for a stuck `PENDING`/`UNRESOLVED` case. Cannot touch resolved cases. |
| `add_provider(provider_id, display_name, schema_id, base_url, api_key, priority)` | owner | Register a new data source (max 5). |
| `remove_provider(provider_id)` | owner | Remove a data source. |
| `set_provider_api_key(provider_id, api_key)` | owner | Set/rotate a provider's key; auto-activates/deactivates based on whether it's non-empty. |
| `set_provider_active(provider_id, active)` | owner | Enable/disable a provider without removing it. |
| `set_provider_priority(provider_id, priority)` | owner | Change fallback order (lower = tried first). |
| `transfer_ownership(new_owner)` | owner | Hand off administrative control. |

### View methods

| Method | Returns |
|---|---|
| `read_case(case_id)` | Full case state: status, disposition, severity, delay, evidence hash, provider used, timing. |
| `read_attempt(case_id, attempt)` | The recorded outcome of one specific attempt. |
| `read_case_by_subject(flight_number, subject_hash)` | Looks up an existing case id for a (flight, passenger+booking) pair, for duplicate-filing checks. |
| `read_providers()` | Registered providers (never includes the raw API key — see security note above). |
| `read_contract_metadata()` | Version, owner, supported schemas, and all timing/threshold constants. |

---

## Claim lifecycle

```
PENDING ──assess_case──▶ ELIGIBLE        (terminal)
   │                  ╲─▶ NOT_ELIGIBLE   (terminal)
   │                  ╲─▶ UNRESOLVED ──retry_unresolved──▶ PENDING (attempt+1, up to 3)
   │                                  ╲─▶ (attempts exhausted) ──reset_case_attempts(owner)──▶ PENDING
   └──expire_pending (72h, unattempted)──▶ UNRESOLVED
```

- **ELIGIBLE** ⇒ `disposition = COMPENSATION_APPROVED`, `severity` = `MINOR` (≥120 min delay) or `MAJOR` (≥240 min delay).
- **NOT_ELIGIBLE** ⇒ `disposition = CLAIM_DENIED`.
- **UNRESOLVED** ⇒ `disposition = REVIEW_REQUIRED` — ambiguous evidence, a data-source outage, or an LLM disagreement; safe to retry.

Eligibility rule (evaluated over independently-verified evidence): flight status is `LANDED` or `DIVERTED` **and** delay ≥ 120 minutes ⇒ eligible; delay clearly below threshold, or a cancellation with no evidence it was airline-caused, ⇒ not eligible; anything ambiguous ⇒ unresolved.

---

## Security notes

- **Deterministic identity, non-deterministic judgment only where needed.** Flight-number/date matching and the delay-vs-threshold comparison are enforced in plain Python before/around the LLM call; the LLM is scoped to genuinely ambiguous judgment calls (e.g. cancellation attribution) and is explicitly instructed to treat all case-submitted text as untrusted data, never as instructions.
- **Evidence binding.** Every resolved decision's evidence hash is bound to the case id, attempt number, chain id, contract address, and provider used — not replayable elsewhere.
- **Owner powers are deliberately narrow.** The owner manages data sources and can rescue a *stuck, unresolved* case — it can never overturn a case that consensus already resolved to `ELIGIBLE`/`NOT_ELIGIBLE`.
- **API keys are public information once stored on-chain.** See the warning above — use a low-privilege, dedicated, rotatable key.
- **This is not a court.** Per GenLayer's own guidance, this contract provides an evidence-based settlement primitive, not a legally binding judgment — pair it with the appropriate off-chain agreement/jurisdiction for production use.

---

## Testing

The contract has been validated with:
- `genvm-lint check` — AST safety checks + full SDK-based semantic validation.
- `pyright` (via `genvm-lint typecheck`) — no type errors.
- `genlayer-test` direct-mode functional tests (11 scenarios: eligible/not-eligible/unresolved paths, validator consensus agreement, provider fallback, time-gating, access control, replay protection, retry exhaustion + owner recovery, and resolved-case immutability). See `test_flight_delay_claim_resolver.py`.

```bash
pip install genvm-linter genlayer-test
genvm-lint check flight_delay_claim_resolver.py
pytest test_flight_delay_claim_resolver.py -v
```

## Known limitations

- AviationStack's accessible historical depth depends on the subscription tier — a production deployment should use a plan whose lookback window covers `max_claim_age_days` (default ~3 years) worth of claims.
- The `NOT_CANCELLED_BY_PASSENGER` dimension can rarely be affirmatively proven from flight-status data alone (most providers don't expose cancellation cause) — cancelled-flight claims will typically resolve to `UNRESOLVED` pending human review rather than an automatic determination either way. This is intentional, conservative behavior, not a bug.
- No payment/escrow logic is included by design; wire a downstream contract to `disposition`/`severity` for payouts.
