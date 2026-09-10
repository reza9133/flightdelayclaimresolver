import json
import datetime as dt


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

LLM_ELIGIBLE_DECISION = json.dumps(
    {"applicability": "ELIGIBLE", "match_mask": 2, "exclusion_mask": 0}
)


def _open_and_configure(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("/home/claude/flight_delay_claim_resolver.py", sdk_version="v0.2.16")

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


def test_full_eligible_flow(direct_vm, direct_deploy, direct_alice):
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

    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_ELIGIBLE_DECISION)

    contract.assess_case("case-1", 1)

    case = contract.read_case("case-1")
    assert case["status"] == "ELIGIBLE", case
    assert case["disposition"] == "COMPENSATION_APPROVED"
    assert case["delay_minutes"] == 145
    assert case["severity"] == "MINOR"  # 145 >= 120 (minor) but < 240 (major)
    assert case["provider_used"] == "aviationstack"
    assert case["flight_status_id"] == "BA100-2024-01-10"
    assert len(case["evidence_hash"]) == 64

    attempt = contract.read_attempt("case-1", 1)
    assert attempt["decision"] == "ELIGIBLE"
    # IDENTITY bit must have been force-set by the contract even though the
    # mocked LLM never claimed it.
    assert attempt["match_mask"] & 1 == 1  # IDENTITY_BIT
    assert attempt["match_mask"] & 2 == 2  # DELAY_THRESHOLD_BIT


def test_validator_agrees_with_leader(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_ELIGIBLE_DECISION)

    contract.assess_case("case-1", 1)
    # validator independently re-runs leader_fn/validator_fn with the SAME
    # mocks still active -> should agree.
    assert direct_vm.run_validator() is True


def test_no_active_provider_yields_unresolved(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("/home/claude/flight_delay_claim_resolver.py", sdk_version="v0.2.16")
    direct_vm.warp("2024-01-13T00:00:00Z")
    direct_vm.sender = direct_alice
    contract.open_case("case-2", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")
    # No provider activated -> _classify should hit the empty-provider path
    contract.assess_case("case-2", 1)
    case = contract.read_case("case-2")
    assert case["status"] == "UNRESOLVED"
    assert case["disposition"] == "REVIEW_REQUIRED"


def test_assess_too_early_reverts(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("/home/claude/flight_delay_claim_resolver.py", sdk_version="v0.2.16")
    # "now" is BEFORE the flight has had a chance to conclude.
    direct_vm.warp("2024-01-10T05:00:00Z")
    contract.set_provider_api_key("aviationstack", "TESTKEY1234567890")
    direct_vm.sender = direct_alice
    contract.open_case("case-3", "BA100", "Jane Doe", "ABC123", "2024-01-10", "LHR", "JFK")

    with direct_vm.expect_revert("sufficient time"):
        contract.assess_case("case-3", 1)


def test_non_owner_cannot_add_provider(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("/home/claude/flight_delay_claim_resolver.py", sdk_version="v0.2.16")
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("not the contract owner"):
        contract.add_provider(
            "evil", "Evil", "AVIATIONSTACK_V1", "https://evil.example/flights", "somekey123", 1
        )


def test_not_eligible_short_delay(direct_vm, direct_deploy, direct_alice):
    contract = _open_and_configure(direct_vm, direct_deploy, direct_alice)
    short_delay_body = json.loads(AVIATIONSTACK_BODY)
    short_delay_body["data"][0]["arrival"]["delay"] = 30
    short_delay_body["data"][0]["departure"]["delay"] = 10
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": json.dumps(short_delay_body)})
    direct_vm.mock_llm(
        r".*", json.dumps({"applicability": "NOT_ELIGIBLE", "match_mask": 0, "exclusion_mask": 2})
    )
    contract.assess_case("case-1", 1)
    case = contract.read_case("case-1")
    assert case["status"] == "NOT_ELIGIBLE"
    assert case["disposition"] == "CLAIM_DENIED"
    assert case["severity"] == ""
    assert case["delay_minutes"] == 30


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

    # a resolved (ELIGIBLE) case can never be reset
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"api\.aviationstack\.com", {"status": 200, "body": AVIATIONSTACK_BODY})
    direct_vm.mock_llm(r".*", LLM_ELIGIBLE_DECISION)
    contract.assess_case("case-1", 1)
    assert contract.read_case("case-1")["status"] == "ELIGIBLE"
    with direct_vm.expect_revert("resolved cases cannot be reset"):
        contract.reset_case_attempts("case-1")


def test_placeholder_domain_is_gone():
    with open("/home/claude/flight_delay_claim_resolver.py", "r") as f:
        src = f.read()
    assert "flightstatus.example" not in src
    assert "api.aviationstack.com" in src


def test_pending_expiry_exceeds_assessment_gate():
    import sys

    sys.path.insert(0, "/home/claude")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "flight_contract_constants", "/home/claude/flight_delay_claim_resolver.py"
    )
    # We can't import it directly (uses `from genlayer import *`), so just
    # regex-extract the two constants and check the invariant textually.
    with open("/home/claude/flight_delay_claim_resolver.py") as f:
        src = f.read()
    import re

    pending = int(re.search(r"PENDING_EXPIRY_SECONDS\s*=\s*(\d+)", src).group(1))
    gate = int(re.search(r"MIN_ASSESSMENT_DELAY_SECONDS\s*=\s*(\d+)", src).group(1))
    assert pending > gate, "PENDING_EXPIRY_SECONDS must exceed MIN_ASSESSMENT_DELAY_SECONDS"
