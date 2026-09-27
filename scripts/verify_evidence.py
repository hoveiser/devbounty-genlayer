#!/usr/bin/env python
"""Independently re-verify every transaction hash recorded in evidence/.

Hits ONLY the explorer JSON API (no SDK trust): for each hash it asserts
status == FINALIZED, no execution error, and prints the per-validator vote
map so consensus participation is visible. Exit non-zero on any failure.

Usage: python scripts/verify_evidence.py
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://studio.genlayer.com/api/explorer/transactions/{tx}"

# WAF blocks the default Python-urllib User-Agent on studio.genlayer.com
HDRS = {"User-Agent": "devbounty-evidence/1.0", "Accept": "application/json"}


def fetch(tx: str) -> dict:
    req = urllib.request.Request(API.format(tx=tx), headers=HDRS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode())
            return payload.get("transaction", payload)
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(3)


def main() -> int:
    targets = []
    flow = json.loads((ROOT / "evidence" / "live_flow.json").read_text())
    for mode, scen in flow.items():
        if not isinstance(scen, dict):
            continue
        for s in scen.get("steps", []):
            if s.get("tx"):
                targets.append((f"{mode}:{s['step']}", s["tx"]))
    dep = json.loads((ROOT / "evidence" / "deployments.json").read_text())
    for d in dep["deployments"]:
        if d.get("deploy_tx"):
            targets.append((f"deploy:{d['contract'][:8]}", d["deploy_tx"]))
    integ = json.loads((ROOT / "evidence" / "integration_studionet.json").read_text())
    for f in integ.get("flow", []):
        if f.get("tx"):
            targets.append((f"integration:{f['step']}", f["tx"]))

    ok = True
    for label, tx in targets:
        t = fetch(tx)
        status = t.get("status")
        error = t.get("error")
        votes = (t.get("consensus_data") or {}).get("votes") or {}
        vote_names = sorted(set(votes.values()))
        good = status == "FINALIZED" and not error
        ok = ok and good
        print(f"{'OK ' if good else 'BAD'} {label:<28} {tx[:16]}… {status}"
              f" votes={vote_names or 'n/a'} error={error}")
    print("\nRESULT:", "ALL VERIFIED" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
