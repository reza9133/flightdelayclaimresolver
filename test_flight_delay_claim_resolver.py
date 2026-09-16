import json


AVIATIONSTACK_BODY = json.dumps(
    {
        "pagination": {"limit": 100, "offset": 0, "count": 1, "total": 1},
        "data": [
            {
                "flight_date": "2024-01-10",
                "flight_status": "landed",
                "departure": {
                    "airport": "Heathrow",
                    "iata": "LHR",
                    "delay": 40,
                },
                "arrival": {
                    "airport": "JFK",
                    "iata": "JFK",
                    "delay": 145,
                },
                "airline": {"name": "British Airways", "iata": "BA", "icao": "BAW"},
                "flight": {"number": "100", "iata": "BA100", "icao": "BAW100", "codeshared": None},
            }
        ],
    }
)

LLM_DELAY_CONFIRMED = json.dumps(
    {"applicability": "DELAY_CONFIRMED", "match_mask": 4, "exclusion_mask": 0}
)

CONTRACT_PATH = "/home/claude/flight_delay_claim_resolver.py"
SDK_VERSION = "v0.2.16"


def _deploy(direct_deploy):
    return direct_deploy(CONTRACT_PATH, sdk_version=SDK_VERSION)


def _open_and_configure(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)

    # Fix "now" to a date safely after the flight's scheduled day so the
    # 36h assessment gate is already open.
    direct_vm.warp("2024-01-13T00:00:00Z")

    # owner (default sender) activates the built-in aviationstack provider
    contract.set_provider_api_key("aviationstack", "TESTKEY1234567890")

    direct_vm.sender = direct_alice
    contract.open_case(
        "case-1",
        "BA100",
        "Jane Doe",
        "ABC123",
        "2024-01-10",
        "LHR",
        "JFK",
    )
    return contract


def test_full_delay_confirmed_flow(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)

    # Sanity: provider is registered + active, no api_key leak in read_providers
    providers = contract.read_providers()
    assert len(providers) == 1
    assert providers[0]["provider_id"] == "aviationstack"
    assert providers[0]["active"] is True
    assert "api_key" not in providers[0]

    case = contract.read_case("case-1")
    assert case["status"] == "PENDING"
    assert case["earliest_assessment_at"] > 0
    assert case["entitlement_independently_verified"] is False

    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_DELAY_CONFIRMED)

    contract.assess_case("case-1", 1)

    case = contract.read_case("case-1")
    assert case["status"] == "DELAY_CONFIRMED", case
    assert case["disposition"] == "FLIGHT_DELAY_ATTESTED"
    assert case["delay_minutes"] == 145
    assert case["severity"] == "MINOR"  # 145 >= 120 (minor) but < 240 (major)
    assert case["provider_used"] == "aviationstack"
    assert case["flight_status_id"] == "BA100-2024-01-10"
    assert len(case["evidence_hash"]) == 64
    # Route was claimed as LHR->JFK and the (mocked) real flight matches it.
    assert case["verified_origin_airport"] == "LHR"
    assert case["verified_destination_airport"] == "JFK"
    assert case["route_verified"] is True
    assert case["entitlement_independently_verified"] is False

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["decision"] == "DELAY_CONFIRMED"
    # IDENTITY + ROUTE bits must have been force-set by the contract even
    # though the mocked LLM never claimed them.
    assert attempt["match_mask"] & 1 == 1  # IDENTITY_BIT
    assert attempt["match_mask"] & 2 == 2  # ROUTE_BIT
    assert attempt["match_mask"] & 4 == 4  # DELAY_THRESHOLD_BIT
    assert attempt["route_verified"] is True


def test_route_mismatch_is_deterministic_and_skips_llm(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    wrong_route_body = json.loads(AVIATIONSTACK_BODY)
    # The flight's REAL route is LHR -> CDG, but the claim says LHR -> JFK.
    wrong_route_body["data"][0]["arrival"]["iata"] = "CDG"
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(wrong_route_body)})
    # Deliberately NOT mocking the LLM: if the contract's route check is
    # correctly short-circuiting BEFORE the LLM call, this test passes
    # without ever needing one. If the short-circuit were broken and the
    # code fell through to exec_prompt, the unmocked call would raise and
    # get caught by _classify's broad except -> UNRESOLVED, which would
    # fail the assertions below just the same.

    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "CRITERIA_NOT_MET", case
    assert case["disposition"] == "CRITERIA_NOT_MET"
    assert case["route_verified"] is False
    assert case["verified_origin_airport"] == "LHR"
    assert case["verified_destination_airport"] == "CDG"  # actual != claimed JFK
    assert case["severity"] == ""

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["exclusion_mask"] & 16 == 16  # ROUTE_MISMATCH_BIT
    assert attempt["match_mask"] & 2 == 0  # ROUTE_BIT must NOT be asserted
    assert attempt["match_mask"] & 1 == 1  # IDENTITY_BIT still holds (right flight, wrong route)


def test_route_unknown_is_unresolved_not_approved(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    no_route_body = json.loads(AVIATIONSTACK_BODY)
    del no_route_body["data"][0]["departure"]["iata"]
    del no_route_body["data"][0]["arrival"]["iata"]
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(no_route_body)})
    # No LLM mock needed here either — UNKNOWN route also short-circuits.

    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["route_verified"] is False
    assert case["verified_origin_airport"] == ""
    assert case["verified_destination_airport"] == ""


def test_validator_agrees_with_leader(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_DELAY_CONFIRMED)

    contract.assess_case("case-1", 1)
    # validator independently re-runs leader_fn/validator_fn with the SAME
    # mocks still active -> should agree.
    assert direct_vm.run_validator() is True


def test_no_active_provider_yields_unresolved(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.warp("2024-01-13T00:00:00Z")
    direct_vm.sender = direct_alice
    contract.open_case("case-2", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")
    # No provider activated -> _classify should hit the empty-provider path
    contract.assess_case("case-2", 1)
    case = contract.read_case("case-2")
    assert case["status"] == "UNRESOLVED"
    assert case["disposition"] == "REVIEW_REQUIRED"


def test_assess_too_early_reverts(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    # "now" is BEFORE the flight has had a chance to conclude.
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


def test_criteria_not_met_short_delay(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    short_delay_body = json.loads(AVIATIONSTACK_BODY)
    short_delay_body["data"][0]["arrival"]["delay"] = 30
    short_delay_body["data"][0]["departure"]["delay"] = 10
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(short_delay_body)})
    direct_vm.mock_llm(
        r".*", json.dumps({"applicability": "CRITERIA_NOT_MET", "match_mask": 0, "exclusion_mask": 8})
    )
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "CRITERIA_NOT_MET"
    assert case["disposition"] == "CRITERIA_NOT_MET"
    assert case["severity"] == ""
    assert case["delay_minutes"] == 30
    assert case["route_verified"] is True  # route was fine; only delay failed


def test_cancelled_flight_is_unresolved_without_llm_guess(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    cancelled_body = json.loads(AVIATIONSTACK_BODY)
    cancelled_body["data"][0]["flight_status"] = "cancelled"
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(cancelled_body)})
    # LLM correctly reports uncertainty since no cancellation-cause data exists
    direct_vm.mock_llm(r".*", json.dumps({"applicability": "UNRESOLVED", "match_mask": 0, "exclusion_mask": 0}))
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["disposition"] == "REVIEW_REQUIRED"


def test_duplicate_subject_and_flight_rejected(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    with direct_vm.expect_revert("already have a case"):
        contract.open_case(
            "case-1-dup", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK"
        )


def test_retry_exhaustion_then_owner_reset(direct_vm, direct_deploy, direct_alice, direct_owner):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    # force UNRESOLVED (no provider match) by mocking a non-matching flight number
    empty_body = json.dumps({"pagination": {}, "data": []})
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": empty_body})

    contract.assess_case("case-1", 1)
    assert contract.read_case("case-1")["status"] == "UNRESOLVED"

    direct_vm.warp("2024-01-13T02:00:00Z")  # past 1h retry cooldown
    contract.retry_unresolved("case-1", 1)
    contract.assess_case("case-1", 2)
    assert contract.read_case("case-1")["status"] == "UNRESOLVED"
    assert contract.read_case("case-1")["attempt"] == 2

    direct_vm.warp("2024-01-13T04:00:00Z")
    contract.retry_unresolved("case-1", 2)
    contract.assess_case("case-1", 3)
    case = contract.read_case("case-1")
    assert case["status"] == "UNRESOLVED"
    assert case["attempt"] == 3

    # attempts exhausted
    direct_vm.warp("2024-01-13T06:00:00Z")
    with direct_vm.expect_revert("maximum attempts"):
        contract.retry_unresolved("case-1", 3)

    # owner rescues the stuck case
    direct_vm.sender = direct_owner
    contract.reset_case_attempts("case-1")
    case = contract.read_case("case-1")
    assert case["status"] == "PENDING"
    assert case["attempt"] == 1

    # a resolved (DELAY_CONFIRMED) case can never be reset
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_DELAY_CONFIRMED)
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
