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

const CONTRACT_DEFAULT = '0x6b810DA81489834e383349187753ea1D0A9965A3';
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

// ---------------------------------------------------------------- reading --

async function read(fn, args = []) {
  return client.readContract({ address: contract, functionName: fn, args });
}

async function refreshList() {
  try {
    const [stats, list] = await Promise.all([
      read('stats'),
      read('list_bounties', ['', 0, 50]),
    ]);
    $('st-total').textContent = stats.total;
    $('st-active').textContent = stats.active;
    $('st-locked').textContent = fmtGen(stats.locked);
    const tb = $('bounty-table').querySelector('tbody');
    tb.innerHTML = '';
    for (const b of list.items) {
      const tr = el('tr');
      tr.append(
        el('td', 'mono', b.id),
        el('td', '', `${b.repo} #${b.issue_number}`),
        el('td', `status s-${b.status}`, b.status),
        el('td', '', `${fmtGen(b.reward)} GEN`),
        el('td', '', b.deadline_date),
        el('td', b.final === 'APPROVED' ? 'ok' : b.final === 'REJECTED' ? 'bad' : '', b.final || '—'),
      );
      tr.onclick = () => select(b.id);
      tb.append(tr);
    }
    $('refresh-note').textContent = `live read @ ${new Date().toLocaleTimeString()} · ${list.total} bounties`;
    if (selected) renderDetail(selected);
  } catch (e) {
    $('refresh-note').textContent = `read error: ${e.message}`;
  }
}

async function select(id) {
  selected = id;
  $('actions').hidden = !account;
  renderDetail(id);
}

async function renderDetail(id) {
  const panel = $('detail');
  try {
    const [b, ev] = await Promise.all([read('get_bounty', [id]), read('get_evidence', [id])]);
    panel.innerHTML = '';
    panel.append(el('h2', '', `bounty ${b.id}`));
    const grid = el('div', 'grid');
    for (const [k, v] of [
      ['repo / issue', `${b.repo} #${b.issue_number}`],
      ['status', b.status],
      ['reward', `${fmtGen(b.reward)} GEN (escrowed on-chain)`],
      ['poster', b.poster],
      ['deadline', b.deadline_date],
      ['pr', b.pr_url || '—'],
      ['submitter', b.submitter || '—'],
      ['payout address', b.payout_address || '—'],
    ]) {
      const d = el('div', 'kv');
      d.append(el('span', 'k', k), el('span', 'mono v', v));
      grid.append(d);
    }
    panel.append(grid);

    // ---- the on-chain AI evidence trail (the point of the whole app) ----
    const verdict = ev.verdict;
    if (verdict && verdict.deterministic) {
      const det = el('div', 'evidence');
      det.append(el('h3', '', 'deterministic checks (strict_eq layer over GitHub facts)'));
      for (const c of verdict.deterministic) {
        const li = el('div', c.ok ? 'ok' : 'bad', `${c.ok ? '✓' : '✗'} ${c.check}`);
        det.append(li);
      }
      panel.append(det);
    }
    if (verdict && verdict.judgment) {
      const j = el('div', 'evidence');
      j.append(el('h3', '', 'AI substantive judgment — independently re-run by every validator (comparative layer)'));
      j.append(el('div', `verdict-${String(verdict.judgment.decision).toLowerCase()}`,
        `decision: ${verdict.judgment.decision}`));
      const ul = el('ul');
      for (const r of verdict.judgment.reasons || []) ul.append(el('li', '', r));
      j.append(ul);
      if (verdict.pr_title || verdict.issue_title) {
        j.append(el('div', 'muted', `issue: “${verdict.issue_title || ''}” · pr: “${verdict.pr_title || ''}”`));
      }
      panel.append(j);
    }
    if (ev.history && ev.history.length) {
      const h = el('div', 'evidence');
      h.append(el('h3', '', 'on-chain history (append-only)'));
      for (const line of ev.history) h.append(el('div', 'mono muted', typeof line === 'string' ? line : JSON.stringify(line)));
      panel.append(h);
    }
  } catch (e) {
    panel.innerHTML = `<p class="bad">read failed: ${e.message}</p>`;
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
