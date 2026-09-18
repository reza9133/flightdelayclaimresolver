# FlightDelayClaimResolver

An [Intelligent Contract](https://docs.genlayer.com) on [GenLayer](https://www.genlayer.com/) that produces an independently-verified **flight-delay attestation**. Validators fetch live flight-status data from a configurable set of real, authoritative aviation data providers, reach consensus on the facts (via GenLayer's Optimistic Democracy / Equivalence Principle), and an LLM judges delay-eligibility criteria against those facts — all under strict, deterministic evidence validation, including a hard check that the claimed route actually matches the flight.

> **Deployed contract:** `0x2a867C5b33ec7CfaFa385A1532bB2B6A4eC0fB31`
> **Network:** Studionet
> **Explorer:** `https://explorer-studio.genlayer.com/address/0x2a867C5b33ec7CfaFa385A1532bB2B6A4eC0fB31`

---

> ## ⚠️ Required first step — do this before anything else
>
> This contract deploys with its data provider **inactive by default** — it will never silently call a placeholder endpoint. Until you run the command below, **every claim will resolve to `UNRESOLVED`**, not because of a bug, but because no live flight-status source is configured yet.
>
> ```bash
> genlayer write \
>   --contract 0x2a867C5b33ec7CfaFa385A1532bB2B6A4eC0fB31 \
>   --function set_provider_api_key \
>   --args aviationstack "<YOUR_AVIATIONSTACK_API_KEY>"
> ```
>
> Get a key at [aviationstack.com](https://aviationstack.com). Only the contract **owner** (the deployer account) can call this. See [Security notes](#security-notes) for why this key should be a low-privilege, dedicated, rotatable one.

---

## ⚠️ Scope: this is an attestation, not a compensation authority

**This contract does not, and structurally cannot, approve compensation.** It independently verifies three things against real aviation data:

1. **Flight identity** — the flight number actually existed on the scheduled date.
2. **Route** — the flight's *actual* departure/arrival airports match what the claimant submitted.
3. **Delay/cancellation facts** — whether the flight's real-world outcome meets the configured delay threshold.

It has **no way to verify that the submitting party is the actual holder of the booking** they cite (`passenger_name` / `booking_reference`) — no publicly accessible, authoritative passenger/PNR lookup exists for a smart contract to query. So a `DELAY_CONFIRMED` outcome means *"this flight, on this date, on this route, was independently verified as delayed/diverted beyond the threshold"* — it is a **flight-fact attestation**, never an entitlement or payment decision. A downstream system (an airline, an escrow contract, a human reviewer) is expected to independently confirm the claimant actually holds that booking before paying anything out. This scope is also baked into the contract itself — see `read_contract_metadata().scope_disclaimer` and `read_case().entitlement_independently_verified` (always `false`).

## What it does

1. A claimant (or anyone on their behalf) **files a claim** for a specific flight, route, booking, and passenger — `open_case`. These submitted details are stored as claim metadata, not verified facts.
2. Once the flight has plausibly concluded, anyone can **trigger an assessment** — `assess_case`. Under the hood:
   - The **leader** validator fetches live flight-status data from the first active, configured provider that succeeds.
   - The **other validators** independently re-fetch and re-derive the same result and vote to agree/disagree (GenLayer's Equivalence Principle).
   - The contract **deterministically cross-checks the claimed route** against the flight's actual departure/arrival airports. A mismatch is a hard, non-ambiguous disqualifier — the LLM is never even invoked in that case.
   - If the route checks out, an **LLM** judges the remaining delay/cancellation criteria against the verified facts under a strict, injection-resistant prompt, and validators cross-check that judgment too.
3. The claim resolves to one of three states: **DELAY_CONFIRMED**, **CRITERIA_NOT_MET**, or **UNRESOLVED** (ambiguous or missing data — retryable, capped at 3 attempts, with an owner-only recovery path for stuck cases).

The contract does not hold funds or move any payment itself. A downstream escrow/payment contract can read `disposition`, `severity`, `route_verified`, and `evidence_hash` from a resolved case — but must independently confirm booking ownership before paying anything out.

## Why this needs GenLayer

Determining "does this flight's outcome meet the delay-attestation criteria" requires:
- Fetching **live, external, authoritative data** (flight status) — no built-in oracle for this exists on ordinary chains.
- **Judgment**, not just a lookup: interpreting ambiguous cases (e.g. a cancelled flight with no stated cause) safely defaults to requiring human review rather than guessing.
- **Neutral consensus**: no single centralized backend or airline should be the sole arbiter of whether *their own* flight's delay meets an objective threshold.

This is a canonical fit for GenLayer's [milestone/dispute adjudication](https://docs.genlayer.com/understand-genlayer-protocol/typical-use-cases) use case — scoped narrowly to the part that GenLayer validators can actually verify.

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
  ┌──────────────────────────────────────────────────────────────┐
  │  gl.vm.run_nondet_unsafe(leader_fn, ...)                      │
  │                                                                │
  │  for provider in providers (priority order):                   │
  │     fetch flight status  ──▶  parse & validate                  │
  │     (first success wins; on failure, try next)                  │
  │                                                                │
  │  independently verified flight identity + route                │
  │        |                                                        │
  │        ├─▶ route MISMATCH or UNKNOWN ──▶ UNRESOLVED              │
  │        │      (deterministic, LLM never called, retryable —     │
  │        │       never a permanent denial from one data point)    │
  │        └─▶ route MATCH                                          │
  │               |                                                 │
  │               ├─▶ LANDED / DIVERTED: delay_minutes vs threshold  │
  │               │      ──▶ DELAY_CONFIRMED or CRITERIA_NOT_MET     │
  │               │      (deterministic, LLM never called)          │
  │               └─▶ CANCELLED: cause is genuinely ambiguous         │
  │                      ──▶ LLM judges airline-attributable vs.     │
  │                          extraordinary circumstances (strict     │
  │                          JSON schema; defaults to UNRESOLVED     │
  │                          absent a stated cause)                 │
  └──────────────────────────────────────────────────────────────┘
        |
        v
  validators independently re-run the same pipeline
  and must reach the identical (decision, evidence) tuple
        |
        v
  DELAY_CONFIRMED / CRITERIA_NOT_MET / UNRESOLVED  (stored + hashed evidence)
```

The LLM is invoked for exactly **one** scenario: a `CANCELLED` flight, where the cause genuinely requires interpretation. Every other branch — identity, route, and the delay-vs-threshold comparison for `LANDED`/`DIVERTED` flights — is a plain, mechanical fact check with no ambiguity, so it's resolved in Python and never touches the model.

### Provider registry (configurable data sources)

Rather than a single hardcoded endpoint, the contract owner manages a small **registry** of real flight-status providers:

- Ships with one real, well-documented provider pre-registered: **AviationStack** (`https://api.aviationstack.com/v1/flights`) — **inactive until an API key is set**.
- The owner can register up to 5 providers, each with its own priority. If the leader's first-choice provider is down or returns unusable data, it falls through to the next — deterministically, so every validator tries providers in the exact same order.
- Adding a genuinely different vendor later only requires implementing one parser function and registering its schema id — no redesign needed.

### Evidence & consensus integrity

- **Flight identity** (number + scheduled date) is verified **deterministically in Python**, before any LLM is involved.
- **Route** (origin/destination airports) is cross-checked **deterministically** against the provider's actual departure/arrival data for that exact flight+date. A mismatch or missing route data resolves to `UNRESOLVED`, never a terminal denial — a route disagreement could stem from a claimant mistake or from a single provider's data being wrong, and the contract has no way to tell which, so it never commits to a permanent, non-appealable "no" on that basis alone. The actual verified route (or its absence) stays visible via `verified_origin_airport`/`verified_destination_airport` for a human reviewer regardless.
- **Delay-vs-threshold** (for `LANDED`/`DIVERTED` flights) is a plain numeric comparison done in Python — there's no genuine ambiguity in "is 145 ≥ 120", so the LLM is never invoked for this, either. Delay is measured at **arrival** (falling back to departure delay if unavailable), matching how most delay-compensation regimes measure delay.
- **Cancellation cause** is the one dimension that can genuinely require interpretation, and the *only* case where the LLM is invoked — it judges whether a cancellation was airline-attributable or an extraordinary circumstance beyond the airline's control, strictly from whatever the evidence states. AviationStack today doesn't expose a cancellation-cause field, so in practice this almost always (correctly) resolves to `UNRESOLVED`; the architecture is ready for a future provider that does supply one.
- Every resolved decision is bound to a **SHA-256 evidence hash** that includes the case id, attempt number, chain id, contract address, provider id, and the canonicalized upstream evidence — so a proof cannot be replayed across cases, attempts, chains, or contract instances.
- The LLM is **never** asked to judge identity, route, or the delay threshold — those bits (`IDENTITY_BIT`, `ROUTE_BIT`, `DELAY_THRESHOLD_BIT`/`DELAY_INSUFFICIENT_BIT`) are set by the contract itself once its own deterministic checks run, never taken on trust from model output. Only `AIRLINE_ATTRIBUTABLE_BIT`/`EXTRAORDINARY_CIRCUMSTANCES_BIT` (cancellations only) come from the model, and even then only when the evidence states a cause directly — never inferred or guessed.

---

## Getting started (post-deploy)

See the **required first step** above. `set_provider_api_key` automatically flips `active` to `true` once a non-empty key is set. Only the contract **owner** (the deployer) can call any provider-management or ownership method.

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

Returns a dict including `status`, `disposition`, `severity`, `delay_minutes`, `evidence_hash`, `provider_used`, `verified_origin_airport` / `verified_destination_airport`, `route_verified`, and `earliest_assessment_at`.

### If unresolved

```bash
# wait 1h cooldown, then:
genlayer write --contract <address> --function retry_unresolved --args "claim-2024-0001" 1
genlayer write --contract <address> --function assess_case --args "claim-2024-0001" 2
```

Up to 3 total attempts. If every attempt is exhausted while genuinely stuck (e.g. a persistent provider outage), the owner can call `reset_case_attempts` — this **can never** touch a case that already resolved to `DELAY_CONFIRMED` or `CRITERIA_NOT_MET` (including a route-mismatch finding).

---

## Method reference

### Write methods

| Method | Access | Description |
|---|---|---|
| `open_case(case_id, flight_number, passenger_name, booking_reference, scheduled_departure_date, origin_airport, destination_airport)` | anyone | File a new claim. One claim per unique (flight, passenger+booking) combination. All arguments beyond `flight_number`/`scheduled_departure_date` are claimant-submitted metadata, not verified. |
| `assess_case(case_id, expected_attempt)` | anyone | Trigger evidence-gathering, route verification, and LLM delay-criteria assessment for the current attempt. |
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
| `read_case(case_id)` | Full case state: status, disposition, severity, delay, claimed vs. verified route, evidence hash, provider used, timing, and an explicit `entitlement_independently_verified: false` flag. |
| `read_attempt(case_id, attempt)` | The recorded outcome of one specific attempt. |
| `read_case_by_subject(flight_number, subject_hash)` | Looks up an existing case id for a (flight, passenger+booking) pair, for duplicate-filing checks. |
| `read_providers()` | Registered providers (never includes the raw API key — see security note above). |
| `read_contract_metadata()` | Version, owner, supported schemas, `scope_disclaimer`, and all timing/threshold constants. |

---

## Claim lifecycle

```
PENDING ──assess_case──▶ DELAY_CONFIRMED   (terminal)
   │                  ╲─▶ CRITERIA_NOT_MET (terminal — short delay, or an
   │                  │                     extraordinary-circumstance
   │                  │                     cancellation)
   │                  ╲─▶ UNRESOLVED ──retry_unresolved──▶ PENDING (attempt+1, up to 3)
   │                                  ╲─▶ (attempts exhausted) ──reset_case_attempts(owner)──▶ PENDING
   └──expire_pending (72h, unattempted)──▶ UNRESOLVED
```

- **DELAY_CONFIRMED** ⇒ `disposition = FLIGHT_DELAY_ATTESTED`, `severity` = `MINOR` (≥120 min delay) or `MAJOR` (≥240 min delay). **Not** a compensation approval — see the scope section above.
- **CRITERIA_NOT_MET** ⇒ `disposition = CRITERIA_NOT_MET`. Check `exclusion_mask`: bit `8` (`DELAY_INSUFFICIENT_BIT`) means the delay was below threshold; bit `32` (`EXTRAORDINARY_CIRCUMSTANCES_BIT`) means a cancellation was judged to be beyond the airline's control.
- **UNRESOLVED** ⇒ `disposition = REVIEW_REQUIRED` — a route disagreement (mismatched or missing route data — always retryable, never a permanent denial from a single data point), a cancellation with no stated cause, a data-source outage, or an LLM disagreement.

Decision rule, evaluated only once identity + route are independently confirmed:
- `LANDED` / `DIVERTED` with delay ≥ 120 min ⇒ `DELAY_CONFIRMED` (`DELAY_THRESHOLD_BIT`); below it ⇒ `CRITERIA_NOT_MET` (`DELAY_INSUFFICIENT_BIT`). **Fully deterministic — the LLM is never invoked for this.**
- `CANCELLED` with evidence the cause was within the airline's control ⇒ `DELAY_CONFIRMED` (`AIRLINE_ATTRIBUTABLE_BIT`); with evidence of an extraordinary circumstance ⇒ `CRITERIA_NOT_MET` (`EXTRAORDINARY_CIRCUMSTANCES_BIT`); with no stated cause (the common case with AviationStack today) ⇒ `UNRESOLVED`. **This is the only scenario that invokes the LLM.**
- Route mismatch or missing route data, at any flight status ⇒ `UNRESOLVED`, always retryable.

---

## Security notes

- **Deterministic identity, route, and delay-threshold checks; LLM judgment only where genuinely needed.** Flight-number/date identity, the origin/destination route check, and the delay-vs-threshold comparison for `LANDED`/`DIVERTED` flights are all enforced in plain Python — none of them involve any genuine ambiguity, so none of them touch the LLM. The model is invoked for exactly one scenario (a `CANCELLED` flight's cause) and is explicitly instructed to treat all case-submitted text as untrusted data, never as instructions, and to answer `UNRESOLVED` rather than infer a cause that isn't directly stated.
- **A single bad data point can never produce a permanent wrong answer for route.** Route mismatch and missing route data both resolve to `UNRESOLVED` — retryable, never terminal — because the contract cannot tell whether a disagreement is a claimant mistake or a provider data issue.
- **Evidence binding.** Every resolved decision's evidence hash is bound to the case id, attempt number, chain id, contract address, and provider used — not replayable elsewhere.
- **Owner powers are deliberately narrow.** The owner manages data sources and can rescue a *stuck, unresolved* case — it can never overturn a case that consensus already resolved to `DELAY_CONFIRMED`/`CRITERIA_NOT_MET`.
- **API keys are public information once stored on-chain.** See the warning above — use a low-privilege, dedicated, rotatable key.
- **This is an attestation, not a court, and not a payment authority.** See the scope section above. Pair this contract with an off-chain (or separate on-chain) booking-verification step and the appropriate legal agreement before releasing any payment.

---

## Testing

The contract has been validated with:
- `genvm-lint check` — AST safety checks + full SDK-based semantic validation.
- `pyright` (via `genvm-lint typecheck`) — no type errors.
- `genlayer-test` direct-mode functional tests (17 scenarios, including several that deliberately register **no LLM mock at all** to prove the delay-confirmed and short-delay paths never call the model: fully-deterministic delay-confirmed/criteria-not-met flows, route match/mismatch/unknown, both cancellation-cause outcomes plus the no-cause default, validator consensus agreement, provider fallback, time-gating, access control, replay protection, retry exhaustion + owner recovery, resolved-case immutability, and static checks that no placeholder or overclaiming vocabulary remains in the source). See `test_flight_delay_claim_resolver.py`.

```bash
pip install genvm-linter genlayer-test
genvm-lint check flight_delay_claim_resolver.py
pytest test_flight_delay_claim_resolver.py -v
```

## Known limitations

- AviationStack's accessible historical depth depends on the subscription tier — a production deployment should use a plan whose lookback window covers `max_claim_age_days` (default ~3 years) worth of claims.
- AviationStack does not expose a cancellation-cause field, so cancelled-flight claims will typically resolve to `UNRESOLVED` pending human review rather than an automatic determination either way. This is intentional, conservative behavior, not a bug — the architecture is ready for a future provider that does supply a cause field.
- **Passenger identity and booking-reference validity are never verified by this contract** — no publicly accessible, authoritative source for that linkage exists. This is a structural scope boundary, documented on-chain via `scope_disclaimer`, not an oversight. Any payment/escrow logic built on top of this contract's output must independently confirm the claimant actually holds the cited booking.
- No payment/escrow logic is included by design; wire a downstream contract to `disposition`/`severity`/`route_verified` for payouts, after independently confirming booking ownership.
