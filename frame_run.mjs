// Run every query of a frame plan through frame_search.mjs, one at a time, under a total page
// (= request) budget. Skips queries that already have a completed receipt; stops on the first error.
// Usage: node frame_run.mjs <plan.json> <max_rows_per_query> <page_budget>
// Needs the Patent Research Hub (PG_HUB_ROOT); exits 78 when it is not configured.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { importGateway, dataPath, checkFrameId, checkedPath } from './hub_config.mjs';
import { requestBudget, isBudgetStop, BUDGET_EXIT } from './request_budget.mjs';

await importGateway('frame_run.mjs'); // exits 78 with a clear message when no Hub or gateway is configured, before any request or budget use

const [planPath, maxArg, budgetArg] = process.argv.slice(2);
const maxRows = Number(maxArg), budget = Number(budgetArg);
if (!planPath || !(maxRows > 0) || !(budget > 0)) throw new Error('usage: node frame_run.mjs <plan.json> <max_rows_per_query> <page_budget>');
const plan = JSON.parse(fs.readFileSync(checkedPath(planPath, 'the plan path'), 'utf8'));
checkFrameId(plan.frame_id);
const globalBudget = requestBudget(plan.frame_id);
const frameSearch = path.join(path.dirname(fileURLToPath(import.meta.url)), 'frame_search.mjs');
const windows = Object.fromEntries(plan.windows.map(w => [w.id, w]));
let used = 0;

// The Hub's search route turns a provider "no matching records" (HTTP 404) into an empty n8n response, which is
// indistinguishable from a failure. A window that fails that way is resolved by DIFFERENCE: count the previous
// window alone and the previous window joined with this one, both now (2 requests). Equal counts prove zero
// filings in this window; anything else stays an error. Evidence goes into the receipt.
async function zeroByDifference(index, rcPath) {
  const q = plan.queries[index], prev = plan.queries[index - 1];
  if (!prev || prev.stratum !== q.stratum) return { resolved: false, requests: 0, reason: 'no previous window to difference against' };
  const rc = JSON.parse(fs.readFileSync(rcPath, 'utf8'));
  if (!/empty response/.test(rc.error || '') || (rc.pages || []).length) return { resolved: false, requests: 0, reason: 'not an empty-response failure on the first page' };
  const { callGateway, readConfig } = await importGateway('frame_run.mjs');
  const last = d => new Date(Date.parse(d + 'T00:00:00Z') - 86400000).toISOString().slice(0, 10);
  const range = /applicationMetaData\.filingDate:\[\d{4}-\d{2}-\d{2} TO \d{4}-\d{2}-\d{2}\]/;
  const w = windows[q.window_id], pw = windows[prev.window_id];
  const joined = q.q.replace(range, `applicationMetaData.filingDate:[${pw.from} TO ${last(w.to)}]`);
  if (joined === q.q || !range.test(prev.q)) return { resolved: false, requests: 0, reason: 'query has no filingDate range' };
  const count = async text => { globalBudget.reserve('zero_by_difference', { query_id: q.query_id }); const r = await callGateway('uspto', { source: 'odp', url: 'https://api.uspto.gov/api/v1/patent/applications/search', method: 'POST', body: { q: text, pagination: { offset: 0, limit: 1 } } }, { config: readConfig() });
    const p = JSON.parse(r.text); if (r.status !== 200 || !Number.isInteger(p.count)) throw new Error('count unavailable'); return p.count; };
  let a, b, requests = 0;
  try { requests++; a = await count(prev.q); requests++; b = await count(joined); }
  catch (e) { if (isBudgetStop(e)) throw e; return { resolved: false, requests, reason: 'difference query failed: ' + String(e.message || e).slice(0, 80) }; }
  const evidence = { method: 'count(previous window + this window) - count(previous window), both queried now', previous_query_id: prev.query_id,
    previous_count_now: a, joined_query: joined, joined_count_now: b, difference: b - a, checked_at: new Date().toISOString(), requests };
  if (b !== a) return { resolved: false, requests, reason: `difference is ${b - a}, not zero`, evidence };
  (rc.resumes ||= []).push({ at: evidence.checked_at, prior_error: rc.error, resolution: 'zero by difference' });
  Object.assign(rc, { provider_count: 0, rows_retrieved: 0, truncated: false, stop_reason: 'exhausted', error: null, zero_by_difference: evidence });
  fs.writeFileSync(rcPath, JSON.stringify(rc, null, 2) + '\n');
  return { resolved: true, requests, evidence };
}

for (let qi = 0; qi < plan.queries.length; qi++) {
  const q = plan.queries[qi];
  const dir = dataPath('public-originals', 'frames', plan.frame_id, q.query_id);
  const rc = path.join(dir, 'receipt.json');
  if (fs.existsSync(rc)) {
    const prior = JSON.parse(fs.readFileSync(rc, 'utf8'));
    if (prior.stop_reason === 'exhausted') { console.error('skip (done)', q.query_id); continue; }
  }
  // never exceed the budget: the last query may only use what is left (it then reports truncated and stops)
  const rowsAllowed = Math.min(maxRows, (budget - used) * 50);
  if (rowsAllowed < 1) { console.error(`budget exhausted before ${q.query_id}: used ${used}/${budget}`); process.exitCode = 2; break; }
  // A week folder with pages but no receipt was left by a runner that died mid-week: move it aside (its pages are
  // still counted against the budget via the .orphan-* folder) and harvest the week cleanly.
  if (fs.existsSync(dir) && !fs.existsSync(rc)) {
    const aside = `${dir}.orphan-${new Date().toISOString().replace(/[:.]/g, '-')}`;
    fs.renameSync(dir, aside); console.error('moved orphaned partial week aside:', path.basename(aside));
  }
  const before = fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.startsWith('page-')).length : 0;
  const failedBefore = fs.existsSync(rc) ? (JSON.parse(fs.readFileSync(rc, 'utf8')).failed_attempts || 0) : 0;
  const failedDelta = () => (fs.existsSync(rc) ? (JSON.parse(fs.readFileSync(rc, 'utf8')).failed_attempts || 0) : 0) - failedBefore;
  let out;
  const resume = { ...process.env, ...(fs.existsSync(rc) ? { FRAME_RESUME: '1' } : {}),
 };
  try { out = execFileSync(process.execPath, [frameSearch, plan.frame_id, q.query_id, q.q, String(rowsAllowed), '50'], { encoding: 'utf8', env: resume, stdio: ['ignore', 'pipe', 'inherit'] }); }
  catch (e) {
    if (e.status === BUDGET_EXIT) { process.exitCode = BUDGET_EXIT; break; }
    used += (fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => f.startsWith('page-')).length - before : 0) + Math.max(1, failedDelta());
    console.error('query failed', q.query_id, String(e.stdout || e.message).trim());
    let z; try { z = fs.existsSync(rc) && budget - used >= 2 ? await zeroByDifference(qi, rc) : { resolved: false, requests: 0, reason: 'no receipt or no budget for the difference check' }; }
    catch (e2) { if (isBudgetStop(e2)) { process.exitCode = BUDGET_EXIT; break; } throw e2; }
    used += z.requests;
    if (z.resolved) { console.log(JSON.stringify({ query_id: q.query_id, provider_count: 0, rows: 0, truncated: false, zero_by_difference: z.evidence, used })); continue; }
    console.error('unresolved', q.query_id, z.reason); process.exitCode = 1; break;
  }
  used += fs.readdirSync(dir).filter(f => f.startsWith('page-')).length - before + failedDelta(); // saved pages + retried failures
  const r = JSON.parse(out.trim().split('\n').pop());
  console.log(JSON.stringify({ query_id: q.query_id, provider_count: r.provider_count, rows: r.rows, truncated: r.truncated, used }));
  if (r.truncated) {
    console.error('truncated: shorten windows (window_days in scope.json)'); process.exitCode = 3; break;
  }
}
console.error(`pages used: ${used}`);
