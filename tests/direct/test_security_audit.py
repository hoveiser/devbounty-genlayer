"""Security / corner-case audit tests for DevBounty.

One section per audit point (README "Security audit" table maps to these):

1. exact escrow value      — reward is gl.message.value BY CONSTRUCTION; there
   is no declared-reward parameter, so "promise 2 GEN, send 1 GEN" cannot even
   be expressed. Proven below, including exact odd-value round trips.
2. PR-authorship race      — submit_pr's caller is not trusted to route the
   reward: the payout address must be claimed by the PR's own GitHub author in
   a "devbounty-claim: bounty <id> payout <addr>" comment on the PR, checked
   deterministically. Front-running, fake-claim and cross-bounty replay attacks
   all end REJECTED with zero transfers.
3. double submission /
   double payout           — every second action on a settled bounty reverts;
   exactly one EthSend per bounty can ever be recorded.
4. reclaim/appeal race     — reclaim is blocked while a submission is in
   flight ("submitted"), even past the deadline; vanished PRs (HTTP 404)
   settle as REJECTED instead of reverting forever, so escrow can be neither
   stolen from a live submission nor locked by a dead one.
5. spam/griefing           — README note only (create requires real escrow,
   reclaim returns it; verification is opt-in for third parties).
6. create_bounty input
   validation              — repo/issue reference shape and the reclaim window
   are validated BEFORE any storage write, so a malformed bounty can never be
   created (and never burns an id).
7. no numeric tolerance    — consensus comparisons are exact/binary; there is
   no float, epsilon, ratio or percentage band anywhere in the agreement path.
"""

import json
import re

import pytest

from devbounty_gh_mocks import (
    CONTRACT_PATH,
    ISSUE_NO,
    PAYOUT,
    PR_URL,
    REWARD,
    claim_comment,
    deploy_devbounty,
    hex_of,
    install_payout_hook,
    mock_github,
    mock_llm_default,
    pr_payload,
    warp_to,
)

ATTACKER = "0x2222222222222222222222222222222222222222"
ONE_GEN = 10**18


@pytest.fixture()
def posted(direct_vm, direct_deploy, direct_alice):
    c = deploy_devbounty(direct_vm, direct_deploy, direct_alice)
    direct_vm.value = REWARD
    res = c.create_bounty("acme", "widgets", ISSUE_NO, 30)
    assert res["status"] == "open"
    return c


def _submit(posted, direct_vm, wallet, payout=PAYOUT):
    direct_vm.sender = wallet
    posted.submit_pr("000001", PR_URL, payout)


def _approve_and_pay(posted, direct_vm, wallet, payout=PAYOUT):
    _submit(posted, direct_vm, wallet, payout)
    mock_github(direct_vm)  # default claims: author claimed `payout`
    mock_llm_default(direct_vm, "APPROVED")
    transfers = install_payout_hook(direct_vm)
    ev = posted.verify_resolution("000001")
    return ev, transfers


# ============================================================ 1. exact value ==


def test_reward_is_exactly_the_sent_value(direct_vm, direct_deploy, direct_alice):
    """create_bounty has NO reward parameter — the escrowed value is the reward.

    The spec's attack "claims reward=2 GEN but sends 1 GEN" is not expressible;
    the closest form (underfunding / zero funding) is covered by
    test_create_requires_value in test_devbounty.py.
    """
    c = deploy_devbounty(direct_vm, direct_deploy, direct_alice)
    direct_vm.value = ONE_GEN
    res = c.create_bounty("acme", "widgets", ISSUE_NO, 30)
    b = c.get_bounty(res["id"])
    assert b["reward"] == ONE_GEN  # promised == escrowed, exactly
    assert c.stats()["locked"] == ONE_GEN

    # arbitrary odd value round-trips with zero drift (u256 atto, never float)
    odd = 1234567890123456789
    direct_vm.value = odd
    res2 = c.create_bounty("acme", "widgets", "6", 30)
    assert c.get_bounty(res2["id"])["reward"] == odd
    assert c.stats()["locked"] == ONE_GEN + odd

    # no post-creation path inflates a reward: submit_pr cannot touch it
    direct_vm.sender = direct_alice
    c.submit_pr(res2["id"], PR_URL, PAYOUT)
    assert c.get_bounty(res2["id"])["reward"] == odd


# ================================================= 2. PR-authorship race ======


def test_frontrunner_cannot_steer_payout_to_own_wallet(
    posted, direct_vm, direct_bob
):
    """The audited attack: opportunist submits someone else's genuine PR first,
    registering their OWN payout address. The real author's claim comment names
    a different address, so the deterministic layer must reject — even though
    every GitHub fact check passes and the LLM would happily approve."""
    _submit(posted, direct_vm, direct_bob, payout=ATTACKER)
    mock_github(direct_vm)  # author claimed PAYOUT, not ATTACKER
    mock_llm_default(direct_vm, "APPROVED")  # must not be consulted at all
    transfers = install_payout_hook(direct_vm)

    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["payout_claimed_by_pr_author"] is False
    # all genuine-fact checks passed — authorship is the ONLY failing signal
    assert all(
        ok
        for name, ok in checks.items()
        if name != "payout_claimed_by_pr_author"
    )
    assert ev["final"] == "REJECTED"
    assert ev["judgment"]["decision"] == "SKIPPED"  # LLM never reached
    assert posted.get_bounty("000001")["status"] == "rejected"
    assert transfers == []  # nobody paid


def test_real_author_flow_pays_despite_third_party_submitter(
    posted, direct_vm, direct_bob
):
    """Complement of the race test: any wallet may call submit_pr, but the
    payout goes to the author-claimed address — front-running can redirect
    nothing, so the author still receives the reward."""
    _approve_and_pay(posted, direct_vm, direct_bob)
    assert posted.get_bounty("000001")["status"] == "paid"
    b = posted.get_bounty("000001")
    assert b["payout_address"] == PAYOUT  # the author-claimed address
    assert b["submitter"] == hex_of(direct_bob)  # caller recorded, but powerless


def test_claim_from_non_author_does_not_count(posted, direct_vm, direct_bob):
    """Anyone can comment on a public PR — only the PR author's own comments
    bind a payout address."""
    _submit(posted, direct_vm, direct_bob, payout=ATTACKER)
    mock_github(
        direct_vm,
        claims=[claim_comment(login="mallory-passing-by", payout=ATTACKER)],
    )
    mock_llm_default(direct_vm, "APPROVED")
    transfers = install_payout_hook(direct_vm)
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["payout_claimed_by_pr_author"] is False
    assert ev["final"] == "REJECTED"
    assert transfers == []


def test_claim_for_different_bounty_does_not_transfer(posted, direct_vm, direct_bob):
    """A claim names its bounty id; a claim made for bounty 000002 must not
    authorize payout on bounty 000001 (id-bound, no replay across bounties)."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm, claims=[claim_comment(bounty_id="000002", payout=PAYOUT)])
    mock_llm_default(direct_vm, "APPROVED")
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["payout_claimed_by_pr_author"] is False
    assert ev["final"] == "REJECTED"


def test_claim_for_longer_bounty_id_does_not_match(posted, direct_vm, direct_bob):
    """'bounty 0000010' must not satisfy the check for bounty 000001."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(
        direct_vm,
        claims=[
            {
                "user": {"login": "alice-dev"},
                "body": f"devbounty-claim: bounty 0000010 payout {PAYOUT}",
            }
        ],
    )
    mock_llm_default(direct_vm, "APPROVED")
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["payout_claimed_by_pr_author"] is False


def test_overlong_hex_token_is_not_a_valid_claim(posted, direct_vm, direct_bob):
    """A 41+ hex-char run must not be harvested as the claimed address."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(
        direct_vm,
        claims=[
            {
                "user": {"login": "alice-dev"},
                "body": f"devbounty-claim: bounty 000001 payout {ATTACKER}a",
            }
        ],
    )
    mock_llm_default(direct_vm, "APPROVED")
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["payout_claimed_by_pr_author"] is False


# ================================ 3. double submission / double payout ========


def test_double_payout_is_impossible_on_a_settled_bounty(
    posted, direct_vm, direct_alice, direct_bob
):
    ev, transfers = _approve_and_pay(posted, direct_vm, direct_bob)
    assert ev["final"] == "APPROVED"
    assert len(transfers) == 1  # exactly one EthSend emitted

    # every re-entry path reverts once PAID:
    with direct_vm.expect_revert("requires status submitted"):
        posted.verify_resolution("000001")  # no second verification
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("cannot submit for bounty in status paid"):
        posted.submit_pr("000001", PR_URL, ATTACKER)  # no re-submit
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("cannot reclaim bounty in status paid"):
        posted.reclaim_after_timeout("000001")  # poster cannot claw back
    with direct_vm.expect_revert("appeal requires status rejected"):
        posted.appeal("000001")  # no appeal -> no second verification round

    assert len(transfers) == 1  # still exactly one payout, ever
    assert posted.get_bounty("000001")["status"] == "paid"


def test_double_submission_while_pending_reverts(posted, direct_vm, direct_bob, direct_charlie):
    _submit(posted, direct_vm, direct_bob)
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("cannot submit for bounty in status submitted"):
        posted.submit_pr("000001", PR_URL, ATTACKER)


def test_rejected_bounty_can_be_resubmitted_and_pays_once(
    posted, direct_vm, direct_alice, direct_bob
):
    """rejected is NOT terminal (unlike paid/reclaimed): resubmission is the
    intended recovery path, and it still ends in exactly one payout."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm)
    mock_llm_default(direct_vm, "REJECTED", reasons=["diff unrelated"])
    ev = posted.verify_resolution("000001")
    assert ev["final"] == "REJECTED"

    _submit(posted, direct_vm, direct_bob)  # allowed again from rejected
    direct_vm.clear_mocks()  # mock_llm is first-match — drop the REJECTED one
    mock_github(direct_vm)
    mock_llm_default(direct_vm, "APPROVED")
    transfers = install_payout_hook(direct_vm)
    ev = posted.verify_resolution("000001")
    assert ev["final"] == "APPROVED"
    assert len(transfers) == 1
    with direct_vm.expect_revert("requires status submitted"):
        posted.verify_resolution("000001")


# ==================================================== 4. reclaim/appeal race ==


def test_reclaim_blocked_while_submission_in_flight(
    posted, direct_vm, direct_alice, direct_bob
):
    """The exact audited sequence: contributor submits a genuine PR, the poster
    waits out the deadline and tries to pull the escrow out from under the
    pending verification. Reclaim must revert — and the contributor's payout
    must still work afterwards."""
    _submit(posted, direct_vm, direct_bob)
    warp_to(direct_vm, "2027-01-01T00:00:00Z")  # long past the 30-day deadline
    direct_vm.sender = direct_alice  # the poster
    with direct_vm.expect_revert("cannot reclaim bounty in status submitted"):
        posted.reclaim_after_timeout("000001")

    # verification completes normally and pays the contributor:
    mock_github(direct_vm)
    mock_llm_default(direct_vm, "APPROVED")
    transfers = install_payout_hook(direct_vm)
    ev = posted.verify_resolution("000001")
    assert ev["final"] == "APPROVED"
    assert transfers == [{"address": PAYOUT, "value": REWARD, "kind": "eth_send"}]
    # and reclaim stays blocked after settlement too:
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("cannot reclaim bounty in status paid"):
        posted.reclaim_after_timeout("000001")


def test_reclaim_after_rejection_and_deadline_still_works(
    posted, direct_vm, direct_alice, direct_bob
):
    """A failed verification re-opens the poster's exit: rejected + past the
    deadline -> reclaim pays the poster back."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm)
    mock_llm_default(direct_vm, "REJECTED", reasons=["does not implement issue"])
    posted.verify_resolution("000001")
    warp_to(direct_vm, "2027-01-01T00:00:00Z")
    direct_vm.sender = direct_alice
    transfers = install_payout_hook(direct_vm)
    res = posted.reclaim_after_timeout("000001")
    assert res["status"] == "reclaimed"
    assert transfers == [{"address": hex_of(direct_alice), "value": REWARD, "kind": "eth_send"}]


def test_vanished_pr_settles_rejected_instead_of_locking_escrow(
    posted, direct_vm, direct_alice, direct_bob
):
    """Companion guarantee: with reclaim blocked in 'submitted', a griefer must
    not be able to FREEZE escrow by submitting a PR url that later 404s. The
    tolerant fact fetch turns the 404 into a deterministic REJECTION (not a
    permanent revert), after which the poster can reclaim."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm, pr_status=404)
    mock_llm_default(direct_vm, "APPROVED")
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["pr_number_matches_url"] is False  # vanished PR cannot verify
    assert checks["pr_merged"] is False
    assert ev["final"] == "REJECTED"
    warp_to(direct_vm, "2027-01-01T00:00:00Z")
    direct_vm.sender = direct_alice
    assert posted.reclaim_after_timeout("000001")["status"] == "reclaimed"


def test_appeal_race_after_paid_is_closed(
    posted, direct_vm, direct_alice, direct_bob
):
    """Appeal only exists from 'rejected' — once money moved, neither the
    poster nor any submitter can reopen the bounty to trigger a second payout
    or a reclaim under a fresh verification."""
    _approve_and_pay(posted, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("appeal requires status rejected"):
        posted.appeal("000001")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("appeal requires status rejected"):
        posted.appeal("000001")
    assert posted.get_bounty("000001")["status"] == "paid"


# ========================================== 6. create_bounty input validation ==
#
# create_bounty is the ONLY entry point that takes poster-supplied reference
# data, and it is payable — so shape validation must happen strictly BEFORE any
# storage write, or a malformed call could burn a bounty id / strand escrow.

MALFORMED_REFS = [
    pytest.param("", "widgets", "5", 30, "bad repo_owner", id="empty-owner"),
    pytest.param("acme", "", "5", 30, "bad repo_name", id="empty-repo"),
    pytest.param("acme", "widgets", "", 30, "bad issue_number", id="empty-issue"),
    pytest.param(" ", "widgets", "5", 30, "bad repo_owner", id="whitespace-owner"),
    pytest.param("acme/widgets", "widgets", "5", 30, "bad repo_owner", id="slash-in-owner"),
    pytest.param("acme", "../etc", "5", 30, "bad repo_name", id="traversal-in-repo"),
    pytest.param("a" * 101, "widgets", "5", 30, "bad repo_owner", id="owner-too-long"),
    pytest.param("acme", "widgets  ", "5", 30, "bad repo_name", id="repo-with-space"),
    pytest.param("acme", "widgets", " 5", 30, "bad issue_number", id="issue-padded"),
    pytest.param("acme", "widgets", "000005", 30, "bad issue_number", id="issue-leading-zero"),
    pytest.param("acme", "widgets", "0", 30, "bad issue_number", id="issue-zero"),
    pytest.param("acme", "widgets", "-1", 30, "bad issue_number", id="issue-negative"),
    pytest.param("acme", "widgets", "1e9", 30, "bad issue_number", id="issue-not-numeric"),
    pytest.param(
        "acme", "widgets", "1" + "0" * 10, 30, "bad issue_number", id="issue-11-digits"
    ),
]

MALFORMED_WINDOWS = [
    pytest.param(0, id="zero-days"),
    pytest.param(-1, id="negative-days"),
    pytest.param(-99999, id="large-negative"),
    pytest.param(366, id="one-past-cap"),
    pytest.param(10_000, id="ten-thousand-days"),
]


@pytest.mark.parametrize(
    "owner, repo, issue, days, expect", MALFORMED_REFS
)
def test_create_bounty_rejects_malformed_reference_before_any_write(
    posted, direct_vm, direct_alice, owner, repo, issue, days, expect
):
    """Malformed repo/issue references revert, and nothing was written.

    `posted` already holds exactly one legitimate bounty (000001); any state
    change from the rejected call would show up in stats or the id sequence.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = REWARD  # funded: the rejection must be about the SHAPE
    with direct_vm.expect_revert(expect):
        posted.create_bounty(owner, repo, issue, days)

    # no bounty created, no id burned, no extra escrow accounted
    assert posted.stats() == {"total": 1, "active": 1, "locked": REWARD}
    assert posted.list_bounties("", 0, 10)["total"] == 1
    with pytest.raises(Exception):
        posted.get_bounty("000002")
    # the surviving bounty is untouched
    assert posted.get_bounty("000001")["status"] == "open"


@pytest.mark.parametrize("days", MALFORMED_WINDOWS)
def test_create_bounty_rejects_malformed_reclaim_window(
    posted, direct_vm, direct_alice, days
):
    """The poster cannot set an unbounded/negative window, so a bounty can never
    be instantly reclaimable nor effectively immortal."""
    direct_vm.sender = direct_alice
    direct_vm.value = REWARD
    with direct_vm.expect_revert("reclaim_days"):
        posted.create_bounty("acme", "widgets", "5", days)
    assert posted.stats()["total"] == 1


def test_create_bounty_accepts_the_reference_and_window_boundaries(
    direct_vm, direct_deploy, direct_alice
):
    """Proves the validators are shape checks, not over-broad rejections — valid
    GitHub identifiers (100-char cap, dotted/underscored) and the 1/365-day
    window boundaries must be accepted."""
    c = deploy_devbounty(direct_vm, direct_deploy, direct_alice)
    warp_to(direct_vm, "2028-01-01T00:00:00Z")
    direct_vm.value = REWARD
    assert c.create_bounty("a" * 100, "my.repo_1", "9999999999", 1)["status"] == "open"
    assert c.create_bounty("o", "r", "1", 365)["status"] == "open"
    assert c.get_bounty("000001")["deadline_date"] == "2028-01-02"
    # 2028 is a leap year: 365 real days from Jan 1 2028 ends Dec 31 2028
    # (a naive month/year rollover would say 2029-01-01).
    assert c.get_bounty("000002")["deadline_date"] == "2028-12-31"
    assert c.stats()["total"] == 2


def test_zero_value_reverts_before_any_state_change(direct_vm, direct_deploy, direct_alice):
    """The escrow check is the FIRST statement in create_bounty, so no bounty
    (and no burned id) can ever exist unfunded — regardless of the rest of the
    arguments."""
    c = deploy_devbounty(direct_vm, direct_deploy, direct_alice)
    direct_vm.value = 0
    with direct_vm.expect_revert("must be called with GEN value"):
        c.create_bounty("acme", "widgets", ISSUE_NO, 30)
    assert c.stats() == {"total": 0, "active": 0, "locked": 0}
    with direct_vm.expect_revert("must be called with GEN value"):
        c.create_bounty("bad owner!", "", "", 0)
    assert c.stats() == {"total": 0, "active": 0, "locked": 0}


# ================================================= 7. no numeric tolerance ====


def test_contract_source_contains_no_numeric_tolerance():
    """Static guard: every consensus outcome here is binary, so there must be no
    float arithmetic, epsilon, ratio band or absolute-difference tolerance in
    the agreement path. Pinned so a future edit cannot silently introduce one.
    """
    src = CONTRACT_PATH.read_text()
    body = "\n".join(  # drop comments/docstrings from the scan: prose may say "float"
        line for line in src.splitlines() if not line.lstrip().startswith(("#", '"'))
    )
    for pattern in (
        r"\bfloat\b",
        r"\babs\s*\(",
        r"\bround\s*\(",
        r"\btoler",           # tolerance / tolerate
        r"\bepsilon\b",
        r"\bratio\b",
        r"\bapprox", 
        r"\bfuzzy",
        r"[0-9]+\.[0-9]+",    # any decimal literal at all
    ):
        assert re.search(pattern, body) is None, f"unexpected numeric tolerance: {pattern}"


def test_consensus_comparison_is_exact_even_for_the_closest_miss(
    posted, direct_vm, direct_alice, direct_bob
):
    """Behavioral proof: the decision field must match byte-for-byte. The
    closest possible 'near-agreement' — validator reached the same reasons,
    opposite verdict — still forces rotation, i.e. no band around APPROVED."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm)
    mock_llm_default(direct_vm, "APPROVED", reasons=["fixes the off-by-one in paginate"])
    posted.verify_resolution("000001")

    # identical reason text, flipped verdict -> must disagree. The paired True
    # case below pins this to the VERDICT, not to the reason-overlap gate.
    assert direct_vm.run_validator(
        leader_result={
            "decision": "REJECTED",
            "reasons": ["fixes the off-by-one in paginate"],
        }
    ) is False
    assert direct_vm.run_validator(
        leader_result={
            "decision": "APPROVED",
            "reasons": ["fixes the off-by-one in paginate"],
        }
    ) is True
    # verdict is binary: no third/graded outcome can be produced
    assert posted.get_bounty("000001")["status"] == "paid"
    assert json.loads(posted.get_bounty("000001")["verdict"])["final"] == "APPROVED"


def test_deterministic_checks_are_boolean_not_thresholded(
    posted, direct_vm, direct_alice, direct_bob
):
    """The one number that feeds a check (pr_additions) is a bare > 0 test: a
    huge-but-zero diff fails and a single added line passes, so there is no
    minimum-size band a submission could sit inside and be waved through."""
    _submit(posted, direct_vm, direct_bob)
    mock_github(direct_vm, pr=pr_payload(additions=0))
    mock_llm_default(direct_vm, "APPROVED")
    ev = posted.verify_resolution("000001")
    checks = {c["check"]: c["ok"] for c in ev["deterministic"]}
    assert checks["pr_changes_code"] is False  # additions == 0, files still present
    assert ev["final"] == "REJECTED"
    assert ev["judgment"]["decision"] == "SKIPPED"  # facts gate is hard, not fuzzy
    assert all(isinstance(c["ok"], bool) for c in ev["deterministic"])

