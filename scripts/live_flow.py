#!/usr/bin/env python
"""DevBounty live flow on GenLayer studionet.

Scenario A (happy path, --mode approve, run against a FRESH contract):
  create+fund a real bounty for hoveiser/devbounty-demo#1, submit the merged
  fix PR #2, trigger multi-validator consensus verification, prove the payout
  against the RECIPIENT BALANCE before vs after (never status fields alone).

Scenario B (genuine AI-layer rejection, --mode reject, run against the OLD
  contract that already holds funded bounty 000001 for the same issue):
  submit merged PR #3 — a decorative README banner. All six deterministic
  GitHub-fact checks pass (right repo, merged, default branch, real diff);
  only the substantive LLM judgment can reject it. Proves the consensus AI
  layer carries real settlement weight.

Every transaction hash is independently verified against the explorer's JSON
API (studio.genlayer.com/api/explorer/... — not the HTML shell) and written
to evidence/live_flow.json.

NOTE on waiting: genlayer-py 0.16.3's wait_for_transaction_receipt silently
returns an still-ACCEPTED receipt once its internal retries are exhausted
instead of raising — studionet can take minutes to FINALIZE. We therefore
poll the explorer JSON API directly with a long window.

Value-bearing writes use genlayer-py directly: the genlayer CLI has no flag
to attach native value to a contract call.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from eth_account import Account

import genlayer_py as gl

ROOT = Path(__file__).resolve().parents[1]
EXPLORER_API = "https://studio.genlayer.com/api/explorer/transactions/{tx}"

REPO_OWNER = "hoveiser"
REPO_NAME = "devbounty-demo"
ISSUE_NO = "1"
APPROVE_PR_URL = "https://github.com/hoveiser/devbounty-demo/pull/2"
REJECT_PR_URL = "https://github.com/hoveiser/devbounty-demo/pull/3"

REWARD = int(2 * 10**18)  # 2 GEN in atto


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def explorer_get(tx: str) -> dict:
    # studio.genlayer.com's WAF 403s the default Python-urllib User-Agent
    req = urllib.request.Request(
        EXPLORER_API.format(tx=tx),
        headers={"User-Agent": "devbounty-evidence/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode())
    t = payload.get("transaction", payload)
    return {
        "hash": t.get("hash"),
        "status": t.get("status"),
        "from": t.get("from_address"),
        "to": t.get("to_address"),
        "error": t.get("error"),
        "execution_status": t.get("execution_status"),
        # consensus_data.votes: per-validator agree/disagree — proves real
        # multi-validator consensus was reached, not just a lifecycle status
        "validator_votes": (t.get("consensus_data") or {}).get("votes"),
    }


def wait_finalized_explorer(tx_hash: str, window_s=900, interval_s=10) -> dict:
    """Poll the explorer JSON API until FINALIZED (or window expires)."""
    deadline = time.time() + window_s
    info = {}
    while time.time() < deadline:
        try:
            info = explorer_get(str(tx_hash))
        except Exception as e:  # explorer briefly unavailable mid-round
            print(f"  explorer poll error ({e}); retrying")
        if info.get("status") == "FINALIZED":
            return info
        print(f"  {tx_hash[:18]}… status={info.get('status')}")
        time.sleep(interval_s)
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["approve", "reject"], required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument(
        "--evidence",
        default=str(ROOT / "evidence" / "live_flow.json"),
        help="evidence file (merged into if it exists)",
    )
    ap.add_argument(
        "--state",
        default=None,
        help="resume state file; completed steps are skipped on re-run",
    )
    args = ap.parse_args()

    key = os.environ.get("GENLAYER_PRIVATE_KEY") or load_env(ROOT / ".env").get(
        "GENLAYER_PRIVATE_KEY"
    )
    if not key:
        print("GENLAYER_PRIVATE_KEY missing from environment/.env", file=sys.stderr)
        return 2
    acct = Account.from_key(key)
    client = gl.create_client(chain=gl.studionet, account=acct)
    contract = args.contract
    pr_url = APPROVE_PR_URL if args.mode == "approve" else REJECT_PR_URL

    state_path = Path(args.state or ROOT / "evidence" / f"state_{args.mode}.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    if "payout_address" not in state or (
        args.mode == "approve" and "submit_pr" not in state
    ):
        # fresh EOA each full run; once submit_pr is on-chain the registered
        # address must be reused when resuming, or the balance proof breaks.
        # in reject mode this address is registered but must NEVER be paid
        state["payout_address"] = Account.create().address
        state_path.write_text(json.dumps(state, indent=2))
    payout_addr = state["payout_address"]

    print(f"mode                  : {args.mode}")
    print(f"contract              : {contract}")
    print(f"poster/origin account : {acct.address}")
    print(f"payout EOA            : {payout_addr}")
    print(f"pr under test         : {pr_url}")

    ev_path = Path(args.evidence)
    evidence = (
        json.loads(ev_path.read_text())
        if ev_path.exists() and ev_path.read_text().strip()
        else {}
    )
    scenario = {
        "mode": args.mode,
        "contract": contract,
        "chain": "studionet",
        "pr_url": pr_url,
        "steps": [],
    }
    evidence[args.mode] = scenario

    def persist():
        ev_path.write_text(json.dumps(evidence, indent=2, default=str))

    def do_step(name, fn):
        """Idempotent: submit tx once, remember hash, resume waiting on re-run."""
        if name in state:
            tx = state[name]
            print(f"[resume] {name} already submitted: {tx}")
        else:
            tx = str(fn())
            state[name] = tx
            state_path.write_text(json.dumps(state, indent=2))
        info = wait_finalized_explorer(tx)
        entry = {"step": name, "tx": tx, "explorer": info}
        scenario["steps"].append(entry)
        persist()
        return info

    def read_view(fn, aargs=None):
        return client.read_contract(contract, fn, args=aargs or [])

    bal0 = int(client.get_balance(payout_addr))
    print(f"payout balance before : {bal0}")

    # 1) bounty — approve mode creates+funds a fresh one (payable write WITH
    #    native value; proves payable enforcement on the real network);
    #    reject mode reuses the existing funded bounty 000001 on the old contract
    if args.mode == "approve":
        do_step(
            "create_bounty",
            lambda: client.write_contract(
                contract, "create_bounty", args=[REPO_OWNER, REPO_NAME, ISSUE_NO, 30],
                value=REWARD,
            ),
        )
        listing = read_view("list_bounties", ["", 0, 50])
        bid = next(
            (i["id"] for i in listing["items"] if i["repo"] == f"{REPO_OWNER}/{REPO_NAME}"),
            None,
        )
        assert bid, "bounty not found after create"
        state["bid"] = bid
    else:
        bid = state.get("bid") or "000001"
        b = read_view("get_bounty", [bid])
        assert b and b.get("status") in ("open", "submitted", "rejected"), f"bad bounty: {b}"
    state_path.write_text(json.dumps(state, indent=2))
    print(f"bounty id             : {bid} status={read_view('get_bounty', [bid])['status']}")

    # 2) contributor registers payout address + PR url
    if read_view("get_bounty", [bid])["status"] != "submitted" or "submit_pr" in state:
        do_step(
            "submit_pr",
            lambda: client.write_contract(
                contract, "submit_pr", args=[bid, pr_url, payout_addr], value=0
            ),
        )

    # 3) consensus verification: validators independently fetch GitHub data
    #    and independently run the LLM judgment
    do_step(
        "verify_resolution",
        lambda: client.write_contract(contract, "verify_resolution", args=[bid], value=0),
    )

    ev = read_view("get_evidence", [bid])
    scenario["onchain_evidence"] = ev
    persist()
    print(json.dumps(ev, indent=2, default=str)[:2000])

    # 4) settlement proof — RECIPIENT BALANCE, not a status field
    expected_delta = REWARD if args.mode == "approve" else 0
    bal_after = bal0
    for _ in range(24):
        bal_after = int(client.get_balance(payout_addr))
        if bal_after - bal0 == expected_delta:
            break
        time.sleep(5)
    paid = bal_after - bal0
    print(f"payout balance after  : {bal_after} (delta {paid}, expected {expected_delta})")
    scenario["settlement_proof"] = {
        "recipient": payout_addr,
        "before": str(bal0),
        "after": str(bal_after),
        "delta": str(paid),
        "expected": str(expected_delta),
        "ok": paid == expected_delta,
        "verdict_final": (ev.get("verdict") or {}).get("final"),
    }

    # independent explorer-JSON verification of every tx in this scenario
    ok_all = True
    for s in scenario["steps"]:
        v = s["explorer"]
        good = v.get("status") == "FINALIZED" and not v.get("error")
        ok_all = ok_all and good
        print(f"verified {s['step']}: {v.get('status')} error={v.get('error')}")

    persist()
    ok = scenario["settlement_proof"]["ok"] and ok_all
    if args.mode == "reject":
        ok = ok and scenario["settlement_proof"]["verdict_final"] == "REJECTED"
    print(f"\nwrote {ev_path}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
