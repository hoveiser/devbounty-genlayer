"""Integration test: DevBounty against REAL GenLayer studionet consensus.

Nothing is stubbed or mocked — validator nodes independently hit the GitHub
API and independently run the LLM judgment. The demo repo/issue/PR
(hoveiser/devbounty-demo #1 fixed by merged PR #2) are real and permanent.

Time-boxed: a full studionet round can take minutes per transaction; this
test performs deploy -> funded create_bounty -> PR-author claim comment ->
submit_pr -> verify_resolution and proves settlement against the RECIPIENT
BALANCE via the same RPC client gltest uses internally.

Run:  gltest tests/integration/ -v -s --network studionet
"""

import json
import sys
import time
from pathlib import Path

import pytest
from eth_account import Account

from gltest import get_contract_factory
from gltest.accounts import get_default_account
from gltest.assertions import tx_execution_succeeded
from gltest.clients import get_gl_client
from gltest.contracts.contract import Contract
from gltest.utils import extract_contract_address
from genlayer_py.types.transactions import TransactionStatus

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from github_claim import post_claim_comment  # noqa: E402

REPO_OWNER = "hoveiser"
REPO_NAME = "devbounty-demo"
ISSUE_NO = "1"
PR_URL = "https://github.com/hoveiser/devbounty-demo/pull/2"
REWARD = 10**18  # 1 GEN atto


def _final(receipt, trail, step):
    assert tx_execution_succeeded(receipt), f"execution failed: {receipt}"
    # GenLayerTransaction behaves dict-like; status_name may be key or attr
    name = getattr(receipt, "status_name", None) or receipt["status_name"]
    assert name == "FINALIZED", name
    h = getattr(receipt, "hash", None)
    if h is None and hasattr(receipt, "get"):
        h = receipt.get("hash")
    trail.append({"step": step, "tx": str(h), "status": name})
    print(f"{step}: {h}")
    return receipt


def _deploy(factory, attempts=3):
    # SDK deviation: factory.deploy() (the convenience wrapper) resolves the
    # schema BY CODE, which is Localnet-only — it always fails on studionet.
    # The working studionet path is: raw deploy tx -> address from receipt ->
    # schema BY ADDRESS (works despite the "Localnet only" docstring).
    account = get_default_account()
    last = None
    for _ in range(attempts):
        try:
            receipt = factory.deploy_contract_tx(
                args=[], account=account, wait_transaction_status=TransactionStatus.FINALIZED
            )
            assert tx_execution_succeeded(receipt), f"deploy execution failed: {receipt}"
            address = extract_contract_address(receipt)
            schema = get_gl_client().get_contract_schema(address)
            return Contract.new(address=address, schema=schema, account=account)
        except Exception as e:  # transient RPC 502s from the studio proxy
            last = e
            time.sleep(15)
    raise last


def _github_token():
    import os

    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("GITHUB_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    assert tok, "GITHUB_TOKEN required to post the PR-author claim comment"
    return tok


@pytest.mark.slow
def test_full_consensus_flow_on_studionet():
    trail = []
    factory = get_contract_factory("DevBounty")
    contract = _deploy(factory)
    print("deployed contract:", contract.address)
    trail.append({"step": "deploy", "contract": contract.address})

    # funded bounty — native value on a payable write, enforced by the network
    res = contract.create_bounty(
        args=[REPO_OWNER, REPO_NAME, ISSUE_NO, 30]
    ).transact(value=REWARD, wait_transaction_status=TransactionStatus.FINALIZED)
    _final(res, trail, "create_bounty")
    listing = contract.list_bounties(args=["", 0, 50]).call()
    assert listing["total"] == 1
    bid = listing["items"][0]["id"]
    assert listing["items"][0]["status"] == "open"

    # contributor registers payout EOA: PR-author claim comment first (the
    # authorship mitigation), then submit_pr with that address
    payout = Account.create()
    claim = f"devbounty-claim: bounty {bid} payout {payout.address}"
    post_claim_comment(_github_token(), PR_URL, bid, payout.address)
    print("claim comment posted for", payout.address)
    res = contract.submit_pr(args=[bid, PR_URL, payout.address]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED
    )
    _final(res, trail, "submit_pr")

    # consensus verification: every validator independently fetches GitHub and
    # independently runs the substantive LLM judgment
    res = contract.verify_resolution(args=[bid]).transact(
        wait_transaction_status=TransactionStatus.FINALIZED
    )
    _final(res, trail, "verify_resolution")

    ev = contract.get_evidence(args=[bid]).call()
    verdict = ev["verdict"]
    assert verdict, "no on-chain evidence stored"
    assert all(c["ok"] for c in verdict["deterministic"]), verdict["deterministic"]
    check_names = {c["check"] for c in verdict["deterministic"]}
    assert check_names == {
        "issue_exists_and_is_issue",
        "pr_number_matches_url",
        "pr_targets_bounty_repo",
        "pr_merged",
        "pr_targets_default_branch",
        "pr_changes_code",
        "payout_claimed_by_pr_author",
    }, check_names
    assert verdict["judgment"]["decision"] == "APPROVED", verdict["judgment"]
    assert verdict["final"] == "APPROVED"
    assert len(verdict["judgment"]["reasons"]) > 0
    assert contract.get_bounty(args=[bid]).call()["status"] == "paid"

    # settlement proof: recipient balance over the same RPC, not a status field
    client = get_gl_client()
    bal = int(client.get_balance(payout.address))
    for _ in range(12):
        if bal == REWARD:
            break
        bal = int(client.get_balance(payout.address))
    assert bal == REWARD, f"expected {REWARD} atto, got {bal}"
    trail.append({
        "settlement_proof": {
            "recipient": payout.address,
            "balance": str(bal),
            "expected": str(REWARD),
            "ok": True,
        }
    })
    trail.append({"pr_author_claim": claim})
    print("\nEVIDENCE:", json.dumps(verdict, indent=2)[:1200])
    out = Path(__file__).resolve().parents[2] / "evidence" / "integration_studionet.json"
    out.write_text(json.dumps({"chain": "studionet", "flow": trail,
                               "verdict": verdict}, indent=2, default=str))
    print("wrote", out)
