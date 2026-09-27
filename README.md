# DevBounty

**Bounties for GitHub issue resolution, settled by GenLayer consensus.**

Anyone posts a bounty against a *real, public GitHub issue* and locks GEN reward
inside an intelligent contract. A contributor submits a PR URL. The contract then
verifies — **independently, on leader *and* validator nodes** — that the PR (a)
targets the right repository, (b) is merged into the default branch, and (c)
*substantively addresses* what the issue describes. Only a consensus verdict can
move money: APPROVED pays the contributor's registered address from escrow;
otherwise the poster reclaims after a timeout. One appeal re-runs the whole
verification with a fresh validator set.

Live dapp frontend: **[https://hoveiser.github.io/devbounty-genlayer/](https://hoveiser.github.io/devbounty-genlayer/)**
— deployed via GitHub Pages, reading on-chain state directly from the public
studionet RPC in the browser (no backend, no indexer). Source:
[`frontend/`](frontend/) (plain HTML/JS + the official `genlayer-js` SDK).

> The deployed bundle has the live contract address baked in **at build time**
> (`CONTRACT_ADDRESS` env → esbuild `--define`, see
> [`frontend/build.mjs`](frontend/build.mjs)); CI asserts it and rejects
> placeholder builds. Auto-deployed on every push to `main` by
> [`.github/workflows/deploy-pages.yml`](.github/workflows/deploy-pages.yml).

![DevBounty frontend with live on-chain evidence](artifacts/devbounty_frontend_full.png)

---

## Why this *needs* GenLayer (and what a single LLM would cost)

The payout decision combines an **external fact check** (GitHub API) with an
**AI-mediated judgment** ("does this diff actually fix what the issue asks?").
That is exactly the setting where a single off-chain oracle is exploitable:

* **Leader-only exploit:** if one node fetches GitHub and prompts one LLM, the
  attacker bribes/fools that one path. A manipulated `merged: true`, a cached
  stale response, or a prompt-injected issue body directly moves escrowed GEN.
* **Prompt injection:** issue/PR bodies are attacker-controlled text embedded in
  the judgment prompt. Here, untrusted content is sanitized (tag-escape, control
  chars, override-phrase redaction, hard truncation) and wrapped in
  `<untrusted_*>` markers; the Direct-Mode suite contains an
  [injection test](tests/direct/test_devbounty.py) with a *trap mock* that only
  fires if raw breakout markup leaked into the prompt — it must (and does) fail
  closed to `REJECTED`.
* **GenLayer's fix:** every validator re-fetches the GitHub data and
  **re-runs the LLM judgment itself**; a verdict only commits when independent
  executions agree. Disagreement rotates the leader. This is not decoration —
  the rejection demo below shows money moving *only* because the consensus AI
  layer said so after every deterministic check passed.

## Equivalence split (two layers, deliberately separated)

| Layer | What it decides | Equivalence principle |
|---|---|---|
| **Facts** | issue exists & is an issue · PR number matches URL · PR targets bounty repo · PR merged · targets default branch · contains real changes | `gl.eq_principle.strict_eq` over a JSON summary of **only stable GitHub fields** (`number`, `merged`, `base.repo.full_name`, `base.ref`, `additions`, file list). Volatile fields (`updated_at`, reaction/comment counts) are never extracted, so edits can't break equivalence; a mid-verification edit just causes disagreement → rotation, never a wrong verdict. |
| **Judgment** | "does the diff substantively resolve the issue?" — inherently subjective | genuine **comparative validator** via `gl.vm.run_nondet_unsafe`: the validator **independently repeats the whole fetch + `exec_prompt`** and compares the stable `decision` field plus token-overlap on `reasons`. LLM misbehavior is classified `[LLM]` and **always disagrees** (forces rotation). Error taxonomy: `[EXPECTED]`/`[EXTERNAL]` match exactly, `[TRANSIENT]` agrees-if-both. |

Merged-PR rule (documented choice): the PR is considered **merged** iff the
GitHub API reports `merged: true` **and** its base branch equals the repo's
`default_branch` at verification time.

## Architecture

```
 poster                     contributor                  GenLayer studionet
┌──────────┐  GEN (native)  ┌──────────┐   GitHub API   ┌─────────────────────────────┐
│ create & │───────────────>│ submit   │<──────────────>│ DevBounty intelligent       │
│ fund     │  create_bounty │  merged  │  fetched by    │ contract                    │
│ bounty   │  @payable      │  PR +    │  leader AND    │  TreeMap<id, Bounty>        │
└──────────┘                │  payout  │  every         │  append-only history        │
     │  reclaim_after_      │  address │  validator     └──────────────┬──────────────┘
     │  timeout (deadline   └────┬─────┘                verify_resolution:
     │  via gl.message_raw)      │ submit_pr                    │
     ▼                           ▼                   ┌──────────┴───────────┐
 poster ◄── EthSend ── escrow   payout EOA ◄── EthSend ── on APPROVED       │
 reclaims                       (never on REJECTED)   strict_eq facts ──────┤
                                                       comparative LLM ─────┘
 frontend (genlayer-js) ── reads get_bounty / get_evidence / list_bounties / stats
                       ── live tx lifecycle: SUBMITTED→PENDING→ACCEPTED→FINALIZED
```

* **Frontend owns nothing authoritative.** It only renders on-chain reads
  (`readContract` over the studionet RPC) and signs writes. There is no indexer.
* **The contract owns** escrow, verdicts, payouts (`@gl.evm.contract_interface`
  → `EthSend`), reclaim, and the one-shot appeal.
* Money is `u256` atto GEN end-to-end — no floats. Storage uses `TreeMap` /
  `DynArray` / `str` status only (no Enum in storage). Failures raise
  `gl.vm.UserError`.

## Live on studionet — every tx independently verified

Chain: **studionet** (`https://studio.genlayer.com/api`, chain id 61999).
Verification of *every* hash below was done against the explorer's **JSON API**
(not the HTML shell) and is recorded in [`evidence/live_flow.json`](evidence/live_flow.json):

```bash
curl -sS -H 'User-Agent: devbounty/1.0' \
  https://studio.genlayer.com/api/explorer/transactions/<hash> \
  | jq '.transaction.status, .transaction.consensus_data.votes'
```

The real-world fixture is [github.com/hoveiser/devbounty-demo](https://github.com/hoveiser/devbounty-demo):
issue **#1** documents an off-by-one in `sum_range`; **PR #2** (merged, squash)
fixes it and adds regression tests; **PR #3** (merged) is a decorative README
ASCII banner *deliberately unrelated* to the issue.

### Scenario A — genuine fix pays out (contract `0x6b81…65A3`)

| step | tx | explorer verdict |
|---|---|---|
| deploy | `0xcdb3e992…5898a` | FINALIZED · `MAJORITY_AGREE` |
| `create_bounty` **with 2 GEN native value** | `0xba17878405…5a671` | FINALIZED · votes all `agree` · `value_credited: true` |
| `submit_pr` (PR #2 + fresh payout EOA) | `0x280d65b1…4cd6` | FINALIZED |
| `verify_resolution` | `0x26562fd0…78e7` | FINALIZED |

**Settlement proven against the recipient's balance, not a status field:**
payout EOA `0x631A…1405` went `0 → 2000000000000000000` atto (delta exactly the
escrowed 2 GEN). On-chain evidence stores the six deterministic checks, the LLM
`decision: APPROVED` and its **reason list** ("range 1,n → 0,n+1", regression
tests added, expected `sum_range(4)=10`, …) — visible in the frontend screenshot.

### Scenario B — merged-but-unsubstantive PR is rejected by the AI layer (contract `0x9e76…9D7f`)

Bounty `000001` (funded, 2 GEN locked) for the same issue; contributor submits
**PR #3** (README banner).

| step | tx | result |
|---|---|---|
| `submit_pr` | `0x5795b9ef…c502` | FINALIZED |
| `verify_resolution` | `0xbd06d054…8cb0` | FINALIZED |

On-chain evidence: **all six deterministic checks ✓** (right repo ✓, merged ✓,
default branch ✓, real diff ✓) — and the consensus LLM still returned
`REJECTED`: *“pr modifies only readme.md with an ascii banner; no changes to
math_utils.py bounds or addition of test_math_utils.py as required by the
issue.”* Recipient balance delta: **0**. The escrow stayed locked for appeal or
reclaim. **This is the proof that the AI judgment layer — not a checkbox — is
doing the settlement work every validator independently agreed on.**

### Scenario C — the same story through the official gltest harness

`tests/integration/test_studionet_flow.py` passed against real consensus
(contract `0xA59a…f77`, recorded in
[`evidence/integration_studionet.json`](evidence/integration_studionet.json)):
create `0x0f0f1bb7…27edf` · submit `0x92db3554…80e39` · verify
`0x675d0c5a…f96713`, all FINALIZED, verdict APPROVED, payout EOA balance
`0 → 1000000000000000000` atto asserted.

## Repository layout

```
contracts/DevBounty.py            the intelligent contract (pinned runner header, lint-clean)
frontend/                          plain HTML/CSS/JS dapp using genlayer-js (real SDK, see below)
frontend/build.mjs                 esbuild wrapper: bakes CONTRACT_ADDRESS into the bundle
.github/workflows/deploy-pages.yml CI: build frontend → deploy to GitHub Pages on every push to main
tests/direct/                      14 Direct-Mode unit tests (mocked GitHub/LLM) incl. injection test
tests/integration/                 gltest flow against REAL studionet consensus (no stubs)
scripts/live_flow.py               the live scenario runner whose output is evidence/live_flow.json
scripts/verify_evidence.py         re-verifies every recorded hash against the explorer JSON API
evidence/                          deployments.json, live_flow.json, integration_studionet.json, run states
artifacts/                         frontend screenshot
gltest.config.yaml                 gltest network config (studionet, env-expanded key)
```

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
# CLI (deploy/interact):  npm install -g genlayer   (v0.39.2 used here)

# .env (gitignored): GENLAYER_PRIVATE_KEY=0x…   (never logged/committed)

genvm-lint check contracts/DevBounty.py         # lint + validate
pytest tests/direct/ -v                          # 14 tests, no network needed

# live flow on studionet (scenarios are resumable, state in evidence/):
.venv/bin/python scripts/live_flow.py --mode approve --contract <addr>
.venv/bin/python scripts/live_flow.py --mode reject  --contract <addr>

# integration test against real consensus (minutes per tx):
.venv/bin/gltest tests/integration/ -v -s --network studionet

# frontend (local)
cd frontend && npm install && npm run build && python3 -m http.server 8765
# open http://localhost:8765 — reads live state from the deployed contract
# npm run build bakes in CONTRACT_ADDRESS (defaults to the live 0x6b81…65A3;
# the Pages deploy workflow passes it explicitly).

# deployed copy: https://hoveiser.github.io/devbounty-genlayer/
# pushed to main → .github/workflows/deploy-pages.yml rebuilds and redeploys
# via actions/deploy-pages. studio.genlayer.com sets CORS headers for the
# Pages origin, so the static site reads the chain directly. (Note: the
# studio RPC intermittently answers gen_call without ACAO under load — the
# app self-recovers on the next auto-refresh cycle and keeps last-good rows.)
```

`create_bounty` needs native GEN attached; the genlayer **CLI cannot attach
value to a call**, so value-bearing flows go through `genlayer-py`
(`scripts/live_flow.py`).

## Where the real SDK differed from the docs (all verified empirically)

Contract/GenVM layer:

1. `gl.message.sender_account` (used in official skill skeletons) **does not
   exist** on studionet's runner — the field is `gl.message.sender_address`
   (`Address`). `gl.message` is a NamedTuple: `(contract_address,
   sender_address, origin_address, value, chain_id)`.
2. **`Address.as_hex` is a *property* returning a checksummed `str`, not a
   method** — calling it raises `TypeError: 'str' object is not callable`.
   Compare with `str(a.as_hex).lower()`.
3. **No timestamp on `gl.message`.** Transaction time arrives only as
   `gl.message_raw["datetime"]` (fixed-width ISO-8601 UTC, e.g.
   `2026-09-27T09:14:14.081651Z`). Date logic is plain integer day-math —
   no `datetime` parsing (forbidden module inside contracts anyway).
4. **`DynArray` is not user-instantiable** (`TypeError: this class can't be
   instantiated by user`). Pass a plain `[]` to `@allow_storage` dataclass
   constructors; mutations need the read-modify-reassign pattern on TreeMaps.
5. **Payout to an EOA emits an `EthSend` gl_call op, not `PostMessage`** —
   `@gl.evm.contract_interface` + `emit_transfer(value=…)` produces
   `{address, calldata: b'', value}`. Pointing `gl.get_contract_at(eoa)` at a
   wallet silently no-ops. Direct Mode needs a `_gl_call_hook` to observe it.
6. The newest runner hash advertised by tooling (`py-genlayer:1zr6nqk…`)
   **cannot be loaded by genvm-linter 0.11.0** (`E101 No module named
   'genlayer.py'` — new bootloader layout). This contract pins the older
   `py-genlayer:1jb45aa8…` which lint+validate fully (`Methods: 9 (4 view,
   5 write)`) and is accepted by studionet consensus. Never
   `test`/`latest`/unpinned.
7. `gl.vm.run_validator` / captured-validator semantics in Direct Mode differ
   from real consensus: leader-only execution, `expect_revert` matches
   *substrings* of `UserError('[EXPECTED] …')`, and `mock_web`/`mock_llm` are
   **first-match regex** — `/pulls/77/files` patterns must be anchored or they
   shadow `/pulls/77`.

Tooling / network layer:

8. **`gltest.config.yaml` does not follow the documented shape.** The real
   parser accepts only root keys `networks` / `paths` / `environment`;
   `contract_path:` and top-level `network:`/wait keys hard-fail with
   “Invalid configuration keys”. Working config committed here.
9. `genlayer-test==0.29.2` **silently downgrades `genlayer-py` to 0.16.3**, whose
   client has no `Config`/new-style network objects — network config is
   nested (`rpc_urls={'default': {'http': […]}}`).
9a. **gltest on studionet has three undocumented traps** (all hit and fixed
    here): (i) `factory.deploy()` resolves the schema **by code**, which is
    Localnet-only — on studionet you must take `deploy_contract_tx` →
    `extract_contract_address` → `get_contract_schema(address)` (the
    “Localnet only” docstring on that one is also wrong — it works) →
    `Contract.new(...)`; (ii) gltest **wipes the artifacts directory at every
    run** — point `paths.artifacts` somewhere that isn't committed evidence;
    (iii) `default_wait_interval` is **milliseconds**, not seconds (the docs'
    example values silently give you a ~0.6s total wait).
10. `client.wait_for_transaction_receipt(status=FINALIZED)` **returns a still
    `ACCEPTED` receipt after its retries silently run out** (no exception) —
    on studionet, FINALIZED can take ≫ the default window. `scripts/live_flow.py`
    polls the explorer JSON API instead.
11. The Vercel-hosted explorer (`genlayer-explorer.vercel.app`, the one baked
    into `genlayer-js` chain configs) is **503 DEPLOYMENT_PAUSED**. The live
    JSON API is `https://studio.genlayer.com/api/explorer/transactions/<hash>`;
    querying by the consensus `tx_id` fails (-32002) — use the submission hash.
12. That same JSON API **403s the default `Python-urllib` User-Agent** (WAF);
    any explicit UA works.
13. `genlayer-js` **1.1.8 ≠ older docs.** There is no `genlayer()` /
    `readContract({fnName})` / `genlayer-js/contracts|providers|wallet`
    subpath API anymore: it's viem-style — `createClient({chain: chains.studionet})`,
    `client.readContract({address, functionName, args})`,
    `client.writeContract({account, …, value})`,
    `client.waitForTransactionReceipt({hash, status})`; enums live in
    `genlayer-js/types`; `getTransaction` returns **numeric** `status`
    (map via `transactionsStatusNumberToName`); `createAccount` takes a
    **positional private-key string**, not an options object; the package
    doesn't even export its own `package.json` subpath.
14. **The genlayer CLI has no flag to attach native GEN to a contract call**,
    so payable enforcement can only be demonstrated via `genlayer-py` (or a
    wallet) — `value_credited: true` in the explorer JSON proves it on-chain.
15. Direct Mode `warp()` updates sender/origin in the cached message context
    but **does not refresh `gl.message_raw["datetime"]`** — tests patch it
    explicitly (see `warp_to` helper).
16. Studionet is gasless (0-balance accounts can transact) **yet native value
    transfer genuinely works** — escrow in/out are real balance movements
    (contrary to “you can't test value on Studio” assumptions).

## What each test tier proves — and doesn't

* **Direct Mode** (`pytest tests/direct/`, 14 passing): business logic, access
  control, guards, evidence shape, sanitizer + injection fail-closed, payout
  *emission* (EthSend recorded via hook), validator **comparison logic** via
  manual `run_validator` captures (agree / decision-disagree / LLM-error-disagree).
  It cannot prove: VM-level `@payable` enforcement, real multi-validator
  consensus, real balance movement, real GitHub/LLM behavior — hence the live
  flows below.
* **Integration** (`tests/integration/`, gltest → real studionet, time-boxed):
  full 5-validator consensus on deploy/create/submit/verify, real GitHub API
  fetches, real LLM judgments, payout asserted against recipient balance.
* **Live scenarios** (`scripts/live_flow.py` → `evidence/live_flow.json`):
  the approve *and* reject stories above; every tx hash independently verified
  against the explorer JSON API with per-validator vote maps.

## Quality-bar self-check

- [x] Pinned runner hash `py-genlayer:1jb45aa8…`; **no** `test`/`latest`/unversioned.
- [x] `genvm-lint check` green (lint + validate; 9 methods, 4 view / 5 write).
- [x] Storage: `TreeMap`/`DynArray`/`u256` atto/`str` statuses only; no Enum, no float money, no bare `Exception` (all `gl.vm.UserError` with error taxonomy).
- [x] Non-deterministic fetch extracts **only stable fields**; equivalence split: `strict_eq` (facts) vs genuine comparative rerun (judgment), decision + reasons compared, `[LLM]` error ⇒ disagree.
- [x] Injection sanitization + dedicated trap-mock test that fails closed.
- [x] Payable real on studionet: 2 GEN in (`value_credited: true`), payout asserted via recipient **balances** before/after in both scenarios (approve pays 2 GEN; reject pays 0).
- [x] Frontend uses the actually-installed `genlayer-js` API and reads **only real on-chain state** (verified in-browser: live rows, evidence, reasons, lifecycle; screenshot in `artifacts/`). No indexer, no mocks.
- [x] Every tx hash verifiable via explorer **JSON API**; verification is scripted (`scripts/verify_evidence.py` — one-liner per hash).
- [x] Appeal (one-shot, poster-or-submitter) and timeout reclaim implemented + tested; deadline math from `gl.message_raw["datetime"]`.
- [x] Deployed, live, and demoed end-to-end on studionet with a real repo/issue/PR set — including a rejection that only consensus-AI could produce.
- [x] Secrets: `.env` gitignored and never printed; key passed via env/stdin only.

## License

MIT — see [LICENSE](LICENSE).
