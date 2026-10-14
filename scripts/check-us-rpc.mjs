// Public-data-only end-to-end smoke for the Electron <-> Python US RPC protocol.
// Never requires credentials or submits orders. Deliberately sets live gate to 0.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const pyDir = join(root, 'python');
const python = process.platform === 'win32'
  ? join(pyDir, '.venv', 'Scripts', 'python.exe')
  : join(pyDir, '.venv', 'bin', 'python');
const tmp = mkdtempSync(join(tmpdir(), 'oracle-us-rpc-'));
const child = spawn(python, ['service.py'], {
  cwd: pyDir,
  env: { ...process.env, KRYPT_POLYBOT_USERDATA: tmp, ORACLE_US_ALLOW_LIVE: '0' },
  windowsHide: true,
  stdio: ['pipe', 'pipe', 'pipe'],
});
let buffer = '';
let nextId = 0;
const pending = new Map();
let stderr = '';
child.stdout.setEncoding('utf8');
child.stdout.on('data', data => {
  buffer += data;
  for (;;) {
    const i = buffer.indexOf('\n');
    if (i < 0) break;
    const line = buffer.slice(0, i).trim();
    buffer = buffer.slice(i + 1);
    try {
      const msg = JSON.parse(line);
      if (msg.type !== 'rpc' || !pending.has(msg.id)) continue;
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.ok ? resolve(msg.result) : reject(new Error(String(msg.error)));
    } catch { /* logs and out-of-band events may arrive */ }
  }
});
child.stderr.setEncoding('utf8');
child.stderr.on('data', data => { stderr = (stderr + data).slice(-2000); });
function call(method, params = {}) {
  const id = String(++nextId);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(method + ' timeout'));
    }, 25000);
    pending.set(id, {
      resolve: x => {clearTimeout(timer);resolve(x);},
      reject: e => {clearTimeout(timer);reject(e);},
    });
    child.stdin.write(JSON.stringify({ type: 'rpc', id, method, params }) + '\n');
  });
}
async function main() {
  try {
    const state = await call('usStatus');
    if (state.connected || state.armed || state.liveConfigured) throw new Error('Unsafe new-profile state');
    console.log('US STATUS: PASS - disconnected, disarmed, live disabled');
    const marketSlug = 'cpc-btc-150k-12-31-2026';
    const market = await call('usMarket', { marketSlug });
    if (!market.market?.slug) throw new Error('No official US market returned');
    console.log('US MARKET: PASS - ' + market.market.slug);
    const book = await call('usBook', { marketSlug });
    if (!book.marketData?.state) throw new Error('Missing US order book state');
    console.log('US BOOK: PASS - ' + book.marketData.state);
    const report = await call('usPreview', {
      marketSlug, intent: 'ORDER_INTENT_BUY_LONG',
      quantity: 10, price: book.marketData.offers?.[0]?.px?.value || '0.05'
    });
    if (!report.orderBookObserved || report.feeAdjustedProfitUnknown !== true) {
      throw new Error('Missing honest exchange preview fields');
    }
    console.log('US PREVIEW: PASS - quote checked, no order sent');
    let blocked = false;
    try {
      await call('usPlace', {
        marketSlug, intent: 'ORDER_INTENT_BUY_LONG',
        quantity: 10, price: '0.05',
        requestId: '7f3ce51e-a35c-4603-9619-4c3c60e323c2',
      });
    } catch (e) {
      blocked = /disabled|Deployment/i.test(String(e.message));
    }
    if (!blocked) throw new Error('US live order was not blocked');
    console.log('US LIVE GATE: PASS - real order blocked before API submission');
    console.log('END-TO-END US RPC: PASS');
  } finally {
    try { await call('shutdown'); } catch { /* may close the pipe */ }
    child.kill();
    setTimeout(() => rmSync(tmp, {recursive:true,force:true}), 1000).unref();
  }
}
main().catch(e => { console.error('US RPC SMOKE FAILED:', e.message); process.exitCode = 1; child.kill(); });
