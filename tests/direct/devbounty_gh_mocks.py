"""Shared Direct Mode test helpers for DevBounty.

Deliberately NOT named conftest.py: with multiple test directories, pytest can
resolve `from conftest import X` to the wrong directory's conftest when the
whole suite runs from the repo root (known tooling pitfall).
"""

import json
import sys
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "DevBounty.py"

REWARD = 3 * 10**18  # 3 GEN in atto — money is always u256 atto, never float

OWNER = "acme"
REPO = "widgets"
ISSUE_NO = "5"
PR_NO = "77"
PAYOUT = "0x1111111111111111111111111111111111111111"
PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{PR_NO}"

BASE_ADDR = "0x2bd806c97f0e00af1a1fc3328fa763a9269723c8"


def hex_of(addr) -> str:
    """Normalize a test fixture address (bytes / str / Address) to 0x-lowercase."""
    if isinstance(addr, (bytes, bytearray)):
        return "0x" + bytes(addr).hex()
    raw = addr.as_hex if hasattr(addr, "as_hex") else str(addr)
    return str(raw).lower()


def warp_to(direct_vm, ts: str) -> None:
    """vm.warp() alone does NOT refresh gl.message_raw['datetime'] in this
    Direct Mode build (it only updates sender/origin). Patch the cached raw
    dict as well so contracts reading transaction time see the warped value."""
    direct_vm.warp(ts)
    gl = sys.modules.get("genlayer.gl")
    if gl is not None and getattr(gl, "message_raw", None) is not None:
        gl.message_raw["datetime"] = ts


def issue_payload(body=None, title="Fix off-by-one in pagination", number=5):
    return {
        "number": number,
        "title": title,
        "body": body
        if body is not None
        else "The list endpoint skips item 0. Please fix the off-by-one bug in paginate().",
    }


def pr_payload(
    merged=True,
    base_repo=f"{OWNER}/{REPO}",
    base_ref="main",
    additions=42,
    number=77,
    body="Closes #5. Fixes the off-by-one and adds regression tests.",
    title="fix: off-by-one in paginate",
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "merged": merged,
        "additions": additions,
        "commits": 2,
        "base": {"repo": {"full_name": base_repo}, "ref": base_ref},
    }


def repo_meta_payload(default_branch="main"):
    return {"default_branch": default_branch}


def files_payload(count=2):
    return [
        {
            "filename": "lib/paginate.py",
            "status": "modified",
            "additions": 30,
            "deletions": 2,
            "patch": "@@ -10,7 +10,9 @@\n-def paginate(items, page):\n-    return items[page * 25 + 1:]\n+def paginate(items, page):\n+    return items[page * 25:]",
        },
        {
            "filename": "tests/test_paginate.py",
            "status": "added",
            "additions": 12,
            "deletions": 0,
            "patch": "@@ -0,0 +1,12 @@\n+def test_first_item_included():\n+    assert paginate(list(range(30)), 0)[0] == 0",
        },
    ][: max(1, min(count, 2))]


def mock_github(
    direct_vm,
    issue=None,
    pr=None,
    meta=None,
    files=None,
    owner=OWNER,
    repo=REPO,
    issue_no=ISSUE_NO,
    pr_no=PR_NO,
):
    """Register the four GitHub API mocks the contract fetches during verify.

    Patterns are anchored so /pulls/77/files cannot shadow /pulls/77.
    """
    o_r = f"/repos/{owner}/{repo}"
    direct_vm.mock_web(
        rf"{o_r}/issues/{issue_no}$",
        {"status": 200, "body": json.dumps(issue or issue_payload())},
    )
    direct_vm.mock_web(
        rf"{o_r}/pulls/{pr_no}/files",
        {"status": 200, "body": json.dumps(files if files is not None else files_payload())},
    )
    direct_vm.mock_web(
        rf"{o_r}/pulls/{pr_no}$",
        {"status": 200, "body": json.dumps(pr or pr_payload())},
    )
    direct_vm.mock_web(
        rf"{o_r}$",
        {"status": 200, "body": json.dumps(meta or repo_meta_payload())},
    )


def llm_verdict(decision, reasons=("implements the fix described in the issue",)):
    return json.dumps({"decision": decision, "reasons": list(reasons)})


def mock_llm_default(direct_vm, decision, reasons=None):
    payload = llm_verdict(decision, reasons) if reasons is not None else llm_verdict(decision)
    direct_vm.mock_llm(r"(?s).*", payload)


def deploy_devbounty(direct_vm, direct_deploy, sender):
    direct_vm.sender = sender
    return direct_deploy(str(CONTRACT_PATH))


def install_payout_hook(direct_vm):
    """Direct Mode does not implement external messaging.

    SDK finding: an @gl.evm.contract_interface emit_transfer to an EOA surfaces
    as an ``EthSend`` gl_call op (address, calldata=b'', value), NOT the
    ``PostMessage`` op used for IC->IC calls — which is why pointing
    gl.get_contract_at(eoa).emit_transfer at a wallet silently no-ops. A manual
    hook records EthSend/PostMessage so payouts can be asserted locally. This
    does NOT prove the network moves value — studionet evidence does that.
    """
    transfers = []

    def hook(_vm, request):
        if "EthSend" in request:
            op = request["EthSend"]
            addr = op["address"]
            transfers.append(
                {
                    "address": str(addr.as_hex if hasattr(addr, "as_hex") else addr).lower(),
                    "value": int(op.get("value") or 0),
                    "kind": "eth_send",
                }
            )
            return {"ok": None}
        if "PostMessage" in request:
            pm = request["PostMessage"]
            addr = pm["address"]
            transfers.append(
                {
                    "address": str(addr.as_hex if hasattr(addr, "as_hex") else addr).lower(),
                    "value": int(pm.get("value") or 0),
                    "kind": "post_message",
                }
            )
            return {"ok": None}
        return None

    direct_vm._gl_call_hook = hook
    return transfers
