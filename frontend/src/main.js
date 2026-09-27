// DevBounty frontend — reads and writes REAL on-chain state on GenLayer
// studionet through the official `genlayer-js` SDK (v1.1.8, viem-style
// client). No indexer, no mock data: every bounty row, verdict and reason
// list displayed here is the result of a live readContract call.
//
// SDK surface verified against the actually-installed package (not docs):
//   createClient({ chain: chains.studionet })
//   client.readContract({ address, functionName, args })
//   client.writeContract({ account, address, functionName, args, value })
//   client.getTransaction({ hash })            -> numeric `status`
//   client.waitForTransactionReceipt({ hash, status })
//   createAccount(privateKey)                  -> positional string arg
//   genlayer-js/types: transactionsStatusNumberToName, ExecutionResult
import { createClient, createAccount, generatePrivateKey, chains } from 'genlayer-js';
import { transactionsStatusNumberToName, transactionResultNumberToName } from 'genlayer-js/types';

// __CONTRACT_ADDRESS__ is injected at build time by build.mjs (esbuild --define)
// from the CONTRACT_ADDRESS env var — this is how the GitHub Pages bundle gets
// the live studionet deployment baked in instead of a localhost default.
// The zero-address fallback only appears if a build forgot to pass one; CI
// asserts the real address is present in the bundle.
const CONTRACT_DEFAULT =
  typeof __CONTRACT_ADDRESS__ !== 'undefined'
    ? __CONTRACT_ADDRESS__
    : '0x0000000000000000000000000000000000000000';
const EXPLORER_JSON = 'https://studio.genlayer.com/api/explorer/transactions/';
const LIFECYCLE = ['SUBMITTED', 'PENDING', 'ACCEPTED', 'FINALIZED'];

const client = createClient({ chain: chains.studionet });
let account = null; // in-memory only
let contract = localStorage.getItem('db.contract') || CONTRACT_DEFAULT;
let selected = null;

const $ = (id) => document.getElementById(id);
const fmtGen = (atto) => {
  const a = BigInt(atto || 0);
  const whole = a / 10n ** 18n;
  const frac = (a % 10n ** 18n) / 10n ** 14n; // 4 decimals
  return frac ? `${whole}.${String(frac).padStart(4, '0')}` : `${whole}`;
};
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined) e.textContent = txt;
  return e;
};

// ------------------------------------------------------------- rendering --
// pure DOM helpers (styling pass only — nothing here touches contract calls)

const statusBadge = (s) => badge('b-' + s, (s || '').toUpperCase());
const verdictBadge = (v, status) => {
  if (v === 'APPROVED') return badge('v-approved', 'APPROVED');
  if (v === 'REJECTED') return badge('v-rejected', 'REJECTED');
  if (v === 'SKIPPED') return badge('v-skipped', 'SKIPPED');
  return status === 'submitted' ? badge('v-pending', 'pending') : badge('v-none', '—');
};
function badge(cls, txt) {
  const b = el('span', `badge ${cls}`);
  b.append(el('span', 'dot'), el('span', '', txt));
  return b;
}
const sk = (cls = 'sk-line') => el('span', `sk ${cls}`);
const loadingRow = () => {
  const tr = el('tr', 'skeleton-row');
  const td = el('td');
  td.colSpan = 6;
  td.append(sk('sk-line sk-w80'));
  tr.append(td);
  return tr;
};
const detailSkeleton = () => {
  const d = el('div', 'sk-cell');
  d.append(sk('sk-line sk-w55'), sk('sk-block'), sk('sk-block'), sk('sk-line sk-w70'));
  return d;
};
let listBusy = false; // suppress the auto-refresh flicker over existing rows

// ---------------------------------------------------------------- reading --

async function read(fn, args = []) {
  return client.readContract({ address: contract, functionName: fn, args });
}

async function refreshList() {
  const tb = $('bounty-table').querySelector('tbody');
  // first paint shows shimmer skeletons; later refreshes update in place
  if (!tb.querySelector('tr:not(.skeleton-row)')) {
    tb.innerHTML = '';
    for (let i = 0; i < 3; i++) tb.append(loadingRow());
  }
  listBusy = true;
  try {
    const [stats, list] = await Promise.all([
      read('stats'),
      read('list_bounties', ['', 0, 50]),
    ]);
    $('st-total').textContent = stats.total;
    $('st-active').textContent = stats.active;
    $('st-locked').textContent = fmtGen(stats.locked);
    tb.innerHTML = '';
    for (const b of list.items) {
      const tr = el('tr');
      tr.append(
        el('td', 'mono', b.id),
        el('td', '', `${b.repo} #${b.issue_number}`),
      );
      const stTd = el('td');
      stTd.append(statusBadge(b.status));
      tr.append(stTd);
      tr.append(
        el('td', 'reward', `${fmtGen(b.reward)} GEN`),
        el('td', 'deadline col-deadline', b.deadline_date),
      );
      const vTd = el('td', 'col-verdict');
      vTd.append(verdictBadge(b.final, b.status));
      tr.append(vTd);
      if (b.id === selected) tr.classList.add('selected');
      tr.onclick = () => select(b.id);
      tb.append(tr);
    }
    if (!list.items.length) {
      const tr = el('tr');
      const td = el('td', 'muted', 'no bounties on this contract yet');
      td.colSpan = 6;
      tr.append(td);
      tb.append(tr);
    }
    $('refresh-note').textContent = `live read @ ${new Date().toLocaleTimeString()} · ${list.total} bounties`;
    if (selected) renderDetail(selected);
  } catch (e) {
    $('refresh-note').textContent = `read error: ${e.message}`;
  } finally {
    listBusy = false;
  }
}

async function select(id) {
  selected = id;
  $('actions').hidden = !account;
  document.querySelectorAll('#bounty-table tbody tr').forEach((r) => r.classList.remove('selected'));
  // immediately highlight the clicked row (don't wait for next refreshList)
  const rows = $('bounty-table').querySelectorAll('tbody tr');
  for (const r of rows) { if (r.querySelector('.mono')?.textContent === id) { r.classList.add('selected'); break; } }
  renderDetail(id);
}

async function renderDetail(id) {
  const panel = $('detail');
  if (!listBusy) {
    panel.innerHTML = '';
    panel.append(el('h2', '', `bounty ${id}`), detailSkeleton());
  }
  try {
    const [b, ev] = await Promise.all([read('get_bounty', [id]), read('get_evidence', [id])]);
    panel.innerHTML = '';
    const head = el('div', 'detail-head');
    const headVerdict = verdictBadge(b.final, b.status);
    headVerdict.id = 'detail-verdict'; // replaced below once the evidence verdict is known
    head.append(el('h2', '', `bounty ${b.id}`), statusBadge(b.status), headVerdict);
    panel.append(head);
    const grid = el('div', 'grid');
    for (const [k, v] of [
      ['repo / issue', `${b.repo} #${b.issue_number}`],
      ['reward', `${fmtGen(b.reward)} GEN (escrowed on-chain)`],
      ['poster', b.poster],
      ['deadline', b.deadline_date],
      ['pr', b.pr_url || '—'],
      ['submitter', b.submitter || '—'],
      ['payout address', b.payout_address || '—'],
    ]) {
      const d = el('div', 'kv');
      d.append(el('span', 'k', k));
      if (k === 'pr' && b.pr_url) {
        const a = el('a', 'v', b.pr_url.replace(/^https:\/\/github\.com\//, 'github.com/'));
        a.href = b.pr_url;
        a.target = '_blank';
        a.rel = 'noopener';
        d.append(a);
      } else {
        d.append(el('span', 'mono v', v));
      }
      grid.append(d);
    }
    panel.append(grid);

    // ---- the on-chain AI evidence trail (the point of the whole app) ----
    const verdict = ev.verdict;
    if (verdict && verdict.judgment && verdict.judgment.decision) {
      headVerdict.replaceWith(verdictBadge(verdict.judgment.decision, b.status));
    }
    if (verdict && verdict.deterministic) {
      const det = el('div', 'evidence');
      det.append(el('h3', '', 'deterministic checks (strict_eq layer over GitHub facts)'));
      for (const c of verdict.deterministic) {
        const row = el('div', `check ${c.ok ? 'ok' : 'bad'}`);
        row.append(el('span', 'mark', c.ok ? '✓' : '✗'), el('span', '', c.check));
        det.append(row);
      }
      panel.append(det);
    }
    if (verdict && verdict.judgment) {
      const j = el('div', 'evidence');
      j.append(el('h3', '', 'AI substantive judgment — independently re-run by every validator (comparative layer)'));
      const pillRow = el('div', 'verdict-pill-row');
      pillRow.append(verdictBadge(verdict.judgment.decision, 'submitted'));
      j.append(pillRow);
      const ul = el('ul');
      for (const r of verdict.judgment.reasons || []) ul.append(el('li', '', r));
      j.append(ul);
      if (verdict.pr_title || verdict.issue_title) {
        j.append(el('div', 'judgment-note', `issue: “${verdict.issue_title || ''}” · pr: “${verdict.pr_title || ''}”`));
      }
      panel.append(j);
    }
    if (ev.history && ev.history.length) {
      const h = el('div', 'evidence');
      h.append(el('h3', '', 'on-chain history (append-only)'));
      for (const line of ev.history) h.append(el('div', 'history-line', typeof line === 'string' ? line : JSON.stringify(line)));
      panel.append(h);
    }
  } catch (e) {
    panel.innerHTML = '';
    panel.append(el('h2', '', `bounty ${id}`));
    panel.append(el('p', 'bad', `read failed: ${e?.message ?? e}`));
  }
}

// ------------------------------------------------------------ lifecycle UI --

function paintLifecycle(stage, note) {
  $('tx-lifecycle').hidden = false;
  const idx = LIFECYCLE.indexOf(stage);
  document.querySelectorAll('.stage').forEach((s) => {
    const i = LIFECYCLE.indexOf(s.dataset.s);
    s.classList.toggle('done', i <= idx && idx >= 0);
  });
  $('tx-note').textContent = note || '';
}

async function trackTx(hash) {
  // poll raw lifecycle with getTransaction (numeric status), then fetch the
  // final receipt and distinguish lifecycle from EXECUTION result
  for (let i = 0; i < 300; i++) {
    let t = null;
    try {
      t = await client.getTransaction({ hash });
    } catch { /* transient */ }
    if (t) {
      const name = t.statusName || transactionsStatusNumberToName?.(t.status) || String(t.status);
      paintLifecycle(name, `${hash.slice(0, 18)}… ${name}` +
        (t.value && t.value !== '0' ? ` · value ${fmtGen(t.value)} GEN` : '') +
        (t.value_credited ? ' · value_credited ✓' : ''));
      if (name === 'FINALIZED' || name === 'CANCELED') {
        const r = await client.waitForTransactionReceipt({ hash, status: 'FINALIZED', retries: 1, interval: 100 });
        const res = r.result_name || transactionResultNumberToName?.(r.result) || '';
        paintLifecycle('FINALIZED',
          `${hash.slice(0, 18)}… FINALIZED · execution: ${res || 'n/a'} · explorer JSON: ${EXPLORER_JSON}${hash}`);
        return r;
      }
    }
    await new Promise((r) => setTimeout(r, 5000));
  }
  paintLifecycle('', 'timed out waiting for FINALIZED');
  return null;
}

async function writeCall(fn, args, valueGen = 0) {
  if (!account) { alert('sign first (bottom panel)'); return; }
  const value = valueGen ? BigInt(Math.round(valueGen * 1e18)) : 0n;
  let hash;
  try {
    hash = await client.writeContract({ account, address: contract, functionName: fn, args, value });
  } catch (e) {
    paintLifecycle('', `${fn} submission failed: ${e.message}`);
    return;
  }
  paintLifecycle('SUBMITTED', `${hash} submitted…`);
  await trackTx(String(hash));
  await refreshList();
  if (selected) renderDetail(selected);
}

// ----------------------------------------------------------------- wiring --

$('switch-contract').onclick = () => {
  const v = $('contract-input').value.trim();
  if (!/^0x[0-9a-fA-F]{40}$/.test(v)) { alert('bad address'); return; }
  localStorage.setItem('db.contract', v);
  location.reload();
};
$('refresh').onclick = refreshList;
$('connect').onclick = () => {
  const pk = $('pk').value.trim();
  if (!/^0x[0-9a-fA-F]{64}$/.test(pk)) { alert('bad key'); return; }
  account = createAccount(pk);
  $('pk').value = '';
  afterConnect();
};
$('gen-key').onclick = () => {
  account = createAccount(generatePrivateKey());
  afterConnect();
};
function afterConnect() {
  $('acct-note').textContent = `signed in as ${account.address}`;
  $('act-who').textContent = account.address;
  $('actions').hidden = false;
}

$('f-create').onsubmit = (e) => {
  e.preventDefault();
  const f = e.target;
  writeCall('create_bounty',
    [f.owner.value.trim(), f.repo.value.trim(), f.issue.value.trim(), Number(f.days.value)],
    Number(f.gen.value));
};
$('f-submit').onsubmit = (e) => {
  e.preventDefault();
  if (!selected) { alert('select a bounty first'); return; }
  writeCall('submit_pr', [selected, e.target.pr_url.value.trim(), e.target.payout.value.trim()]);
};
$('b-verify').onclick = () => selected && writeCall('verify_resolution', [selected]);
$('b-appeal').onclick = () => selected && writeCall('appeal', [selected]);
$('b-reclaim').onclick = () => selected && writeCall('reclaim_after_timeout', [selected]);

$('contract-addr').textContent = contract;
refreshList();
setInterval(refreshList, 20000); // gentle: studionet rate-limits 60 req/min per IP
