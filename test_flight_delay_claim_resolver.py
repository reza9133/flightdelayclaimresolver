import json


def _flight_body(**overrides):
    body = {
        "pagination": {"limit": 100, "offset": 0, "count": 1, "total": 1},
        "data": [
            {
                "flight_date": "2024-01-10",
                "flight_status": "landed",
                "departure": {"airport": "Heathrow", "iata": "LHR", "delay": 40},
                "arrival": {"airport": "JFK", "iata": "JFK", "delay": 145},
                "airline": {"name": "British Airways", "iata": "BA", "icao": "BAW"},
                "flight": {"number": "100", "iata": "BA100", "icao": "BAW100", "codeshared": None},
            }
        ],
    }
    record = body["data"][0]
    for key, value in overrides.items():
        if key in ("departure", "arrival"):
            record[key].update(value)
        else:
            record[key] = value
    return json.dumps(body)


AVIATIONSTACK_BODY = _flight_body()

CONTRACT_PATH = "/home/claude/flight_delay_claim_resolver.py"
SDK_VERSION = "v0.2.16"


def _deploy(direct_deploy):
    return direct_deploy(CONTRACT_PATH, sdk_version=SDK_VERSION)


def _open_and_configure(direct_vm, direct_deploy, direct_alice, case_id="case-1"):
    contract = _deploy(direct_deploy)
    # Fix "now" to a date safely after the flight's scheduled day so the
    # 36h assessment gate is already open.
    direct_vm.warp("2024-01-13T00:00:00Z")
    # owner (default sender) activates the built-in aviationstack provider
    contract.set_provider_api_key("aviationstack", "TESTKEY1234567890")
    direct_vm.sender = direct_alice
    contract.open_case(case_id, "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")
    return contract


# ---------------------------------------------------------------------------
# Core deterministic flow (LANDED/DIVERTED + sufficient delay) — no LLM
# ---------------------------------------------------------------------------
def test_full_delay_confirmed_flow_never_calls_llm(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)

    providers = contract.read_providers()
    assert len(providers) == 1
    assert providers[0]["active"] is True
    assert "api_key" not in providers[0]

    case = contract.read_case("case-1")
    assert case["status"] == "PENDING"
    assert case["entitlement_independently_verified"] is False

    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    # Deliberately NOT mocking the LLM: a LANDED flight with a decisive
    # delay figure is resolved fully deterministically. If the code
    # regressed and tried to call exec_prompt anyway, the unmocked call
    # would raise (caught by _classify's broad except) and produce
    # UNRESOLVED instead of DELAY_CONFIRMED, failing the assertions below.

    contract.assess_case("case-1", 1)

    case = contract.read_case("case-1")
    assert case["status"] == "DELAY_CONFIRMED", case
    assert case["disposition"] == "FLIGHT_DELAY_ATTESTED"
    assert case["delay_minutes"] == 145
    assert case["severity"] == "MINOR"  # 145 >= 120 (minor) but < 240 (major)
    assert case["provider_used"] == "aviationstack"
    assert case["flight_status_id"] == "BA100-2024-01-10"
    assert len(case["evidence_hash"]) == 64
    assert case["verified_origin_airport"] == "LHR"
    assert case["verified_destination_airport"] == "JFK"
    assert case["route_verified"] is True
    assert case["entitlement_independently_verified"] is False

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["decision"] == "DELAY_CONFIRMED"
    assert attempt["match_mask"] & 1 == 1  # IDENTITY_BIT
    assert attempt["match_mask"] & 2 == 2  # ROUTE_BIT
    assert attempt["match_mask"] & 4 == 4  # DELAY_THRESHOLD_BIT
    assert attempt["exclusion_mask"] == 0
    assert attempt["route_verified"] is True


def test_validator_agrees_with_leader_deterministically(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    contract.assess_case("case-1", 1)
    assert direct_vm.run_validator() is True


def test_criteria_not_met_short_delay_is_deterministic(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    body = _flight_body(arrival={"delay": 30}, departure={"delay": 10})
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": body})
    # No LLM mock: a short delay is also resolved deterministically.

    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "CRITERIA_NOT_MET"
    assert case["disposition"] == "CRITERIA_NOT_MET"
    assert case["severity"] == ""
    assert case["delay_minutes"] == 30
    assert case["route_verified"] is True

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["match_mask"] & 4 == 0  # DELAY_THRESHOLD_BIT not set
    assert attempt["exclusion_mask"] & 8 == 8  # DELAY_INSUFFICIENT_BIT


# ---------------------------------------------------------------------------
# Route verification
# ---------------------------------------------------------------------------
def test_route_mismatch_never_becomes_a_permanent_denial(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    # The flight's REAL route is LHR -> CDG, but the claim says LHR -> JFK.
    body = _flight_body(arrival={"iata": "CDG"})
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": body})
    # No LLM mock needed: route mismatch short-circuits before it too.

    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    # A route disagreement is never auto-denied -- it could be the
    # claimant's mistake or a single provider's bad data point, so the
    # contract stays non-committal (UNRESOLVED) rather than issuing a
    # permanent, non-appealable "no".
    assert case["status"] == "UNRESOLVED", case
    assert case["disposition"] == "REVIEW_REQUIRED"
    assert case["route_verified"] is False
    # The discrepancy is still fully visible for a human reviewer.
    assert case["verified_origin_airport"] == "LHR"
    assert case["verified_destination_airport"] == "CDG"
    assert case["origin_airport"] == "LHR"
    assert case["destination_airport"] == "JFK"

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["match_mask"] == 0
    assert attempt["exclusion_mask"] == 0

    # It is retryable like any other UNRESOLVED case.
    direct_vm.warp("2024-01-13T02:00:00Z")
    contract.retry_unresolved("case-1", 1)
    assert contract.read_case("case-1")["status"] == "PENDING"


def test_route_unknown_is_unresolved_not_approved(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    body_dict = json.loads(_flight_body())
    del body_dict["data"][0]["departure"]["iata"]
    del body_dict["data"][0]["arrival"]["iata"]
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(body_dict)})

    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["route_verified"] is False
    assert case["verified_origin_airport"] == ""
    assert case["verified_destination_airport"] == ""


# ---------------------------------------------------------------------------
# Cancellation: the one case that genuinely reaches the LLM
# ---------------------------------------------------------------------------
def test_cancelled_with_no_cause_data_is_unresolved(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    body = _flight_body(flight_status="cancelled")
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": body})
    direct_vm.mock_llm(
        r".*", json.dumps({"applicability": "UNRESOLVED", "match_mask": 0, "exclusion_mask": 0})
    )
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["disposition"] == "REVIEW_REQUIRED"


def test_cancelled_airline_attributable_is_delay_confirmed(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    body = _flight_body(flight_status="cancelled")
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": body})
    direct_vm.mock_llm(
        r".*", json.dumps({"applicability": "DELAY_CONFIRMED", "match_mask": 16, "exclusion_mask": 0})
    )
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "DELAY_CONFIRMED"
    assert case["disposition"] == "FLIGHT_DELAY_ATTESTED"
    attempt = contract.read_attempt("case-1", 1)
    assert attempt["match_mask"] & 16 == 16  # AIRLINE_ATTRIBUTABLE_BIT
    assert attempt["match_mask"] & 1 == 1  # IDENTITY_BIT forced
    assert attempt["match_mask"] & 2 == 2  # ROUTE_BIT forced


def test_cancelled_extraordinary_circumstances_is_criteria_not_met(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    body = _flight_body(flight_status="cancelled")
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": body})
    direct_vm.mock_llm(
        r".*", json.dumps({"applicability": "CRITERIA_NOT_MET", "match_mask": 0, "exclusion_mask": 32})
    )
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "CRITERIA_NOT_MET"
    attempt = contract.read_attempt("case-1", 1)
    assert attempt["exclusion_mask"] & 32 == 32  # EXTRAORDINARY_CIRCUMSTANCES_BIT


# ---------------------------------------------------------------------------
# Everything else (unchanged behavior, re-verified against the new code)
# ---------------------------------------------------------------------------
def test_no_active_provider_yields_unresolved(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.warp("2024-01-13T00:00:00Z")
    direct_vm.sender = direct_alice
    contract.open_case("case-2", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")
    contract.assess_case("case-2", 1)
    case = contract.read_case("case-2")
    assert case["status"] == "UNRESOLVED"
    assert case["disposition"] == "REVIEW_REQUIRED"


def test_assess_too_early_reverts(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.warp("2024-01-10T05:00:00Z")
    contract.set_provider_api_key("aviationstack", "TESTKEY1234567890")
    direct_vm.sender = direct_alice
    contract.open_case("case-3", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")
    with direct_vm.expect_revert("sufficient time"):
        contract.assess_case("case-3", 1)


def test_non_owner_cannot_add_provider(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("not the contract owner"):
        contract.add_provider(
            "evil", "Evil", "AVIATIONSTACK_V1", "https://evil.example/flights", "somekey123", 1
        )


def test_duplicate_subject_and_flight_rejected(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    with direct_vm.expect_revert("already have a case"):
        contract.open_case("case-1-dup", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")


def test_retry_exhaustion_then_owner_reset(direct_vm, direct_deploy, direct_alice, direct_owner):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    empty_body = json.dumps({"pagination": {}, "data": []})
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": empty_body})

    contract.assess_case("case-1", 1)
    assert contract.read_case("case-1")["status"] == "UNRESOLVED"

    direct_vm.warp("2024-01-13T02:00:00Z")
    contract.retry_unresolved("case-1", 1)
    contract.assess_case("case-1", 2)
    assert contract.read_case("case-1")["attempt"] == 2

    direct_vm.warp("2024-01-13T04:00:00Z")
    contract.retry_unresolved("case-1", 2)
    contract.assess_case("case-1", 3)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["attempt"] == 3

    direct_vm.warp("2024-01-13T06:00:00Z")
    with direct_vm.expect_revert("maximum attempts"):
        contract.retry_unresolved("case-1", 3)

    direct_vm.sender = direct_owner
    contract.reset_case_attempts("case-1")
    case = contract.read_case("case-1")
    assert case["status"] == "PENDING"
    assert case["attempt"] == 1

    direct_vm.clear_mocks()
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    contract.assess_case("case-1", 1)
    assert contract.read_case("case-1")["status"] == "DELAY_CONFIRMED"
    with direct_vm.expect_revert("resolved cases cannot be reset"):
        contract.reset_case_attempts("case-1")


def test_scope_disclaimer_present_in_metadata(direct_vm, direct_deploy):
    contract = _deploy(direct_deploy)
    meta = contract.read_contract_metadata()
    assert "compensation" in meta["scope_disclaimer"].lower()
    assert "NOT verify" in meta["scope_disclaimer"]
    assert meta["classification"] == "FLIGHT_DELAY_ATTESTATION_ONLY"


def test_placeholder_domain_is_gone():
    with open(CONTRACT_PATH, "r") as f:
        src = f.read()
    assert "flightstatus.example" not in src
    assert "api.aviationstack.com" in src


def test_no_overclaiming_status_strings_remain():
    with open(CONTRACT_PATH, "r") as f:
        src = f.read()
    assert "COMPENSATION_APPROVED" not in src
    assert '"ELIGIBLE"' not in src
    assert '"NOT_ELIGIBLE"' not in src


def test_pending_expiry_exceeds_assessment_gate():
    import re

    with open(CONTRACT_PATH) as f:
        src = f.read()
    pending = int(re.search(r"PENDING_EXPIRY_SECONDS\s*=\s*(\d+)", src).group(1))
    gate = int(re.search(r"MIN_ASSESSMENT_DELAY_SECONDS\s*=\s*(\d+)", src).group(1))
    assert pending > gate, "PENDING_EXPIRY_SECONDS must exceed MIN_ASSESSMENT_DELAY_SECONDS"
