import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { dataPath } from './hub_config.mjs';

export const BUDGET_EXIT = 75;
export class BudgetStop extends Error { constructor(message) { super(message); this.name = 'BudgetStop'; this.code = 'BUDGET'; } }
export const isBudgetStop = e => e?.code === 'BUDGET';
// Optional request-budget ledger for large harvests. It is OFF unless PG_BUDGET_FRAMES is set to a regular expression;
// frame ids that match it must reserve every provider request in the append-only ledger named by PG_BUDGET_LEDGER.
export const activeBudgetFrame = id => {
  const pattern = process.env.PG_BUDGET_FRAMES;
  if (!pattern) return false;
  try { return new RegExp(pattern).test(id || ''); } catch { throw new BudgetStop('PG_BUDGET_FRAMES is not a valid regular expression'); }
};

const caches = new Map();
const fail = m => { throw new BudgetStop(m); };
const budgetIo = (fn, message) => { try { return fn(); } catch (e) { if (isBudgetStop(e)) throw e; fail(`${message}: ${e.code || e.message || e}`); } };
function parseLines(text, state, first = false) {
  if (text && !text.endsWith('\n')) fail('budget ledger has a torn final line');
  for (const line of text.split('\n').filter(Boolean)) {
    let row; try { row = JSON.parse(line); } catch { fail('budget ledger contains malformed JSON'); }
    if (!state.header) {
      if (!first || row.type !== 'request_budget' || row.version !== 1 || !Number.isInteger(row.cap) || !Number.isInteger(row.baseline) || row.cap < 0 || row.baseline < 0 || row.cap < row.baseline) fail('invalid budget ledger header');
      state.header = row; state.nextSeq = 1; continue;
    }
    if (row.type !== 'reserve' || row.seq !== state.nextSeq || !activeBudgetFrame(row.frame_id)) fail('invalid budget ledger reservation sequence/schema');
    state.nextSeq++; state.reserved++;
  }
}
function load(fd, ledger) {
  const st = budgetIo(() => fs.fstatSync(fd), 'cannot inspect open budget ledger'), named = budgetIo(() => fs.statSync(ledger), 'cannot inspect named budget ledger');
  const identity = `${st.dev}:${st.ino}`;
  if (`${named.dev}:${named.ino}` !== identity) fail('budget ledger path was replaced');
  let state = caches.get(ledger);
  if (state && state.identity !== identity) fail('budget ledger file identity changed');
  if (state && st.size < state.size) fail('budget ledger was truncated');
  if (!state) {
    state = { identity, size: 0, header: null, nextSeq: 0, reserved: 0 };
    const text = budgetIo(() => fs.readFileSync(fd, 'utf8'), 'cannot read budget ledger'); parseLines(text, state, true); state.size = Buffer.byteLength(text); caches.set(ledger, state);
  } else if (st.size > state.size) {
    const length = st.size - state.size, buf = Buffer.alloc(length); const got = budgetIo(() => fs.readSync(fd, buf, 0, length, state.size), 'cannot read budget ledger tail');
    if (got !== length) fail('short read from budget ledger tail');
    parseLines(buf.toString('utf8'), state, false); state.size = st.size;
  }
  if (!state.header) fail('budget ledger is empty');
  return state;
}
function verifyLock() {
  const expected = process.env.PG_BUDGET_LOCK_STAMP, lockPath = process.env.PG_BUDGET_LOCK_PATH || dataPath('locks', 'harvest.lock');
  if (!expected) fail('PG_BUDGET_LOCK_STAMP is missing');
  const actual = budgetIo(() => fs.readFileSync(lockPath, 'utf8'), 'live harvest lock stamp is unavailable');
  if (actual !== expected) fail('live harvest lock stamp does not match this process');
  return expected;
}
export function requestBudget(frameId) {
  if (!activeBudgetFrame(frameId)) return { active: false, reserve() {} };
  return { active: true, reserve(operation, detail = {}) {
    const ledger = process.env.PG_BUDGET_LEDGER;
    if (!ledger) fail('PG_BUDGET_LEDGER is missing for a frame covered by PG_BUDGET_FRAMES');
    const stamp = verifyLock();
    let fd; try { fd = fs.openSync(ledger, 'r+'); } catch { fail('budget ledger is unavailable (collectors never create it)'); }
    try {
      const state = load(fd, ledger), used = state.header.baseline + state.reserved;
      if (used >= state.header.cap) fail(`Request cap exhausted: ${used}/${state.header.cap}`);
      const row = { type: 'reserve', seq: state.nextSeq, at: new Date().toISOString(), frame_id: frameId, operation, pid: process.pid, lock_stamp: stamp, detail };
      const data = Buffer.from(JSON.stringify(row) + '\n');
      const wrote = budgetIo(() => fs.writeSync(fd, data, 0, data.length, state.size), 'cannot append budget reservation');
      if (wrote !== data.length) fail('short write appending budget reservation');
      budgetIo(() => fs.fsyncSync(fd), 'cannot fsync budget reservation');
      state.size += data.length; state.nextSeq++; state.reserved++;
      return { seq: row.seq, used: used + 1, cap: state.header.cap };
    } finally { if (fd !== undefined) budgetIo(() => fs.closeSync(fd), 'cannot close budget ledger'); }
  }};
}

function cli() {
  const [cmd, ledger, capArg, baselineArg, ...note] = process.argv.slice(2);
  if (cmd === 'init') {
    const cap = Number(capArg), baseline = Number(baselineArg);
    if (!ledger || !Number.isInteger(cap) || !Number.isInteger(baseline) || cap < 0 || baseline < 0 || cap < baseline || !note.join(' ').trim()) throw new Error('usage: node request_budget.mjs init <ledger> <cap> <baseline> <approval note>');
    const fd = fs.openSync(ledger, 'wx'); try { fs.writeSync(fd, JSON.stringify({ type: 'request_budget', version: 1, cap, baseline, approval_note: note.join(' '), created_at: new Date().toISOString() }) + '\n'); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    return;
  }
  if (cmd === 'stat') {
    const fd = fs.openSync(ledger, 'r'); try { const s = load(fd, ledger); console.log(JSON.stringify({ cap: s.header.cap, baseline: s.header.baseline, reserved: s.reserved, used: s.header.baseline + s.reserved, remaining: s.header.cap - s.header.baseline - s.reserved })); } finally { fs.closeSync(fd); }
    return;
  }
  throw new Error('usage: node request_budget.mjs <init|stat> ...');
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) cli();
