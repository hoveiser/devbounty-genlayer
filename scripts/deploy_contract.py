#!/usr/bin/env python
"""Deploy contracts/DevBounty.py to studionet via genlayer-py and print the
contract address once the deploy transaction is FINALIZED.

The genlayer CLI needs an unlocked/keychain account which is awkward headless;
the SDK signs directly with GENLAYER_PRIVATE_KEY from .env (never logged).
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

from eth_account import Account

import genlayer_py as gl

ROOT = Path(__file__).resolve().parents[1]
EXPLORER_API = "https://studio.genlayer.com/api/explorer/transactions/{tx}"


def load_env() -> dict:
    return {
        l.split("=", 1)[0]: l.split("=", 1)[1].strip()
        for l in (ROOT / ".env").read_text().splitlines()
        if "=" in l and not l.strip().startswith("#")
    }


def explorer_get(tx: str) -> dict:
    req = urllib.request.Request(
        EXPLORER_API.format(tx=tx),
        headers={"User-Agent": "devbounty-evidence/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode())
    return payload.get("transaction", payload)


def main() -> int:
    env = load_env()
    acct = Account.from_key(env["GENLAYER_PRIVATE_KEY"])
    client = gl.create_client(chain=gl.studionet, account=acct)
    code = (ROOT / "contracts" / "DevBounty.py").read_text()

    tx = client.deploy_contract(code, args=[])
    tx = str(tx)
    print("deploy tx:", tx, flush=True)

    deadline = time.time() + 900
    while time.time() < deadline:
        try:
            t = explorer_get(tx)
        except Exception as e:
            print("  poll:", e, flush=True)
            time.sleep(10)
            continue
        status = t.get("status")
        err = t.get("error")
        if status == "FINALIZED":
            if err:
                print("DEPLOY EXECUTION FAILED:", err, file=sys.stderr)
                return 1
            addr = (
                t.get("contract_address")
                or t.get("to_address")
                or ""
            )
            if not addr or int(str(addr), 16) == 0:
                # fallback: the deploy tx 'to' is null; contract addr comes from
                # the receipt the studio exposes as result/contract_address
                print("could not determine contract address from:", json.dumps(t)[:600])
                return 1
            print("CONTRACT:", addr)
            return 0
        print(f"  status={status}", flush=True)
        time.sleep(10)
    print("deploy did not FINALIZE in window", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
