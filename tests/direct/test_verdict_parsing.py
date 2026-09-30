"""Canonical, fail-closed verdict parsing (reviewer Task 2).

The payout layer must authorize a release ONLY when the model returns the exact
canonical verdict APPROVE. Every other output, negative or mixed wording, an
unexpected extra field, garbage, empty text, or an unknown verdict value,
resolves to REJECT and moves no funds.

Each case drives the full verify_resolution path with the deterministic GitHub
checks passing (valid repo, merged PR, author-claimed payout), so the verdict
parser is the only thing that can decide the outcome. No substring or "contains"
matching is allowed: "APPROVE" embedded in prose must not authorize anything.
"""

import pytest

from devbounty_gh_mocks import (
    ISSUE_NO,
    PAYOUT,
    PR_URL,
    REWARD,
    deploy_devbounty,
    install_payout_hook,
    mock_github,
)

# (raw model output, expect_approve). Valid JSON is auto-parsed by Direct Mode
# into a dict/list; anything else is delivered to the contract as a raw string.
VERDICT_CASES = [
    # canonical APPROVE, only whitespace and case normalized, authorizes payout
    pytest.param('{"verdict": "APPROVE"}', True, id="exact-approve"),
    pytest.param('{"verdict": "  approve  "}', True, id="approve-whitespace"),
    pytest.param('{"verdict": "ApPrOvE", "reasons": ["looks right"]}', True, id="approve-mixed-case"),
    pytest.param('{"verdict": "REJECT", "reasons": ["no"]}', False, id="exact-reject"),
    # negative text must never read as approval
    pytest.param('{"verdict": "not approved"}', False, id="negative-not-approved"),
    pytest.param('{"verdict": "do not approve"}', False, id="negative-do-not-approve"),
    pytest.param('{"verdict": "disapprove"}', False, id="negative-disapprove"),
    pytest.param('{"verdict": "cannot approve"}', False, id="negative-cannot-approve"),
    pytest.param('{"verdict": "I would not approve this"}', False, id="negative-sentence"),
    # mixed wording, both tokens present, must fail closed
    pytest.param('{"verdict": "APPROVE but actually REJECT"}', False, id="mixed-approve-reject"),
    pytest.param('{"verdict": "reject, then approve"}', False, id="mixed-reject-approve"),
    # strict structure: an extra field that could change meaning is rejected
    pytest.param('{"verdict": "APPROVE", "admin_override": true}', False, id="extra-field"),
    pytest.param('{"decision": "APPROVED"}', False, id="legacy-decision-field"),
    pytest.param('{"reasons": ["good work"]}', False, id="missing-verdict-field"),
    # unknown verdict value
    pytest.param('{"verdict": "MAYBE"}', False, id="unknown-verdict"),
    # empty, garbage, malformed, non-object, and no-JSON prose
    pytest.param("", False, id="empty-output"),
    pytest.param("APPROVE", False, id="bare-word-no-json"),
    pytest.param("the contributor should APPROVE this change", False, id="prose-containing-approve"),
    pytest.param("not approved", False, id="prose-negative"),
    pytest.param("{ this is not valid json }", False, id="malformed-json"),
    pytest.param("garbage!!!", False, id="garbage"),
    pytest.param("[]", False, id="json-array-not-object"),
]


@pytest.fixture()
def submitted(direct_vm, direct_deploy, direct_alice, direct_bob):
    """A bounty with one genuine on-time submission whose claim passes every
    deterministic check, isolating the verdict parser as the sole decider."""
    c = deploy_devbounty(direct_vm, direct_deploy, direct_alice)
    direct_vm.value = REWARD
    c.create_bounty("acme", "widgets", ISSUE_NO, 30)
    direct_vm.sender = direct_bob
    c.submit_pr("000001", PR_URL, PAYOUT)
    return c


@pytest.mark.parametrize("payload, expect_approve", VERDICT_CASES)
def test_verdict_parsing_is_canonical_and_fail_closed(
    submitted, direct_vm, payload, expect_approve
):
    mock_github(direct_vm)  # deterministic facts all pass, so the LLM is reached
    direct_vm.mock_llm(r"(?s).*", payload)
    transfers = install_payout_hook(direct_vm)

    ev = submitted.verify_resolution("000001")
    status = submitted.get_bounty("000001")["status"]

    if expect_approve:
        assert ev["final"] == "APPROVED"
        assert ev["judgment"]["decision"] == "APPROVED"
        assert status == "paid"
        assert transfers == [{"address": PAYOUT, "value": REWARD, "kind": "eth_send"}]
    else:
        # nothing but an exact canonical APPROVE authorizes payout; every other
        # verdict resolves to REJECT and moves no funds
        assert ev["final"] == "REJECTED"
        assert status == "rejected"
        assert transfers == []
