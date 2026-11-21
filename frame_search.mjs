// Bounded frame harvest for ONE query through the existing Hub gateway route (Path A, no store).
// Pages sequentially until provider count is exhausted or max_rows is hit, keeps every raw page
// with its SHA-256, and refuses to overwrite an existing query folder.
// Usage: node frame_search.mjs <frame_id> <query_id> "<q>" <max_rows> [page_limit<=100]
//   writes public-originals/frames/<frame_id>/<query_id>/{page-<offset>.json, receipt.json}
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { requestBudget, isBudgetStop, BUDGET_EXIT } from './request_budget.mjs';
import { importGateway, dataPath, checkFrameId } from './hub_config.mjs';
const { callGateway, readConfig } = await importGateway('frame_search.mjs');

const [frameId, queryId, q, maxArg, limitArg] = process.argv.slice(2);
const maxRows = Number(maxArg), limit = Number(limitArg || 100);
const ID = /^[a-z0-9][a-z0-9._-]{0,80}$/;
if (frameId) checkFrameId(frameId);
if (!ID.test(frameId || '') || !ID.test(queryId || '') || !q || q.length > 2000 || !(Number.isInteger(maxRows) && maxRows >= 1 && maxRows <= 5000) || !(Number.isInteger(limit) && limit >= 1 && limit <= 100))
  throw new Error('usage: node frame_search.mjs <frame_id> <query_id> "<q>" <max_rows 1..5000> [page_limit 1..100]');
const url = 'https://api.uspto.gov/api/v1/patent/applications/search';
const dir = dataPath('public-originals', 'frames', frameId, queryId);
const resume = process.env.FRAME_RESUME === '1';
const config = readConfig();
const globalBudget = requestBudget(frameId);
const sha = t => createHash('sha256').update(t).digest('hex');
const PAGE_RETRY_WAITS_MS = (process.env.FRAME_RETRY_WAITS_MS || '0,10000,30000').split(',').map(Number); // first try, then two retries
const localAllowance = process.env.FRAME_REQUEST_ALLOWANCE === undefined ? Infinity : Number(process.env.FRAME_REQUEST_ALLOWANCE);
if (!(localAllowance >= 0) || (localAllowance !== Infinity && !Number.isInteger(localAllowance))) throw new Error('invalid local request allowance');
let localAttempts = 0;
let receipt;
if (fs.existsSync(dir)) {
  // FRAME_RESUME=1 continues an errored or budget-capped (max_rows) harvest of the identical query; earlier pages are kept, never refetched.
  if (!resume) throw new Error(`refusing to overwrite ${dir}; use a new query_id (or FRAME_RESUME=1 after an error)`);
  receipt = JSON.parse(fs.readFileSync(path.join(dir, 'receipt.json'), 'utf8'));
  // A smaller page_limit is allowed on resume (large pages can exceed the gateway's response size); each page records its own limit.
  if (!['error', 'max_rows'].includes(receipt.stop_reason) || receipt.q !== q || maxRows < receipt.max_rows || (receipt.stop_reason === 'error' && maxRows !== receipt.max_rows) || limit > receipt.page_limit) throw new Error('resume refused: prior harvest did not stop on error, or parameters differ');
  (receipt.resumes ||= []).push({ at: new Date().toISOString(), prior_error: receipt.error, prior_stop: receipt.stop_reason, max_rows_from: receipt.max_rows, max_rows_to: maxRows, page_limit_from: receipt.page_limit, page_limit_to: limit });
  receipt.page_limit = limit; receipt.max_rows = maxRows; receipt.error = null; receipt.stop_reason = null;
} else {
  fs.mkdirSync(dir, { recursive: true });
  receipt = { frame_id: frameId, query_id: queryId, route: 'uspto', source: 'odp', url, q, max_rows: maxRows, page_limit: limit, retrieved_at: new Date().toISOString(), provider_count: null, pages: [], truncated: false, stop_reason: null, error: null };
}

let rows = receipt.pages.reduce((n, p) => n + p.rows, 0), offset = rows;
let pageLimit = limit; // shrinks on HTTP 413 (a page too large for the provider) and grows back after a success
try { while (true) {
  const body = { q, pagination: { offset, limit: Math.min(pageLimit, maxRows - rows) } };
  // A page is an idempotent read: transient failures (provider/n8n timeouts surface as an empty n8n response, or a
  // 429/5xx status) are retried twice with backoff before the window is recorded as an error. Every failed attempt
  // counts against the budget (receipt.failed_attempts). A 4xx other than 429 is not retried.
  let r = null, lastError = null;
  for (const wait of PAGE_RETRY_WAITS_MS) {
    if (wait) await new Promise(res => setTimeout(res, wait));
    try {
      if (localAttempts >= localAllowance) { const e = new Error('local frame request allowance exhausted'); e.code = 'FRAME_LOCAL_BUDGET'; throw e; }
      globalBudget.reserve('frame_search', { query_id: queryId, offset, limit: body.pagination.limit });
      localAttempts++;
      r = await callGateway('uspto', { source: 'odp', url, method: 'POST', body }, { config });
      if (r.status === 200 && typeof r.text === 'string') { lastError = null; break; }
      lastError = `provider status ${r.status}`;
    } catch (e) { if (isBudgetStop(e) || e.code === 'FRAME_LOCAL_BUDGET') throw e; lastError = String(e.message || e); }
    receipt.failed_attempts = (receipt.failed_attempts || 0) + 1;
    (receipt.retries ||= []).push({ offset, at: new Date().toISOString(), error: lastError.slice(0, 120) });
    if (r && r.status >= 400 && r.status < 500 && r.status !== 429) break;
  }
  // 413: this offset's page is too large for the provider; halve the page size and retry the same offset.
  if (lastError && r && r.status === 413 && body.pagination.limit > 1) {
    pageLimit = Math.max(1, Math.floor(body.pagination.limit / 2));
    (receipt.page_size_reductions ||= []).push({ offset, from: body.pagination.limit, to: pageLimit, at: new Date().toISOString() });
    console.error(`413 at offset ${offset}: page size ${body.pagination.limit} -> ${pageLimit}`);
    continue;
  }
  if (lastError) { receipt.error = `offset ${offset}: ${lastError}`; receipt.stop_reason = 'error'; break; }
  if (pageLimit < limit) pageLimit = Math.min(limit, pageLimit * 2); // grow back after a page that fit
  const parsed = JSON.parse(r.text);
  const bag = Array.isArray(parsed.patentFileWrapperDataBag) ? parsed.patentFileWrapperDataBag : [];
  if (receipt.provider_count === null) receipt.provider_count = Number.isInteger(parsed.count) ? parsed.count : null;
  const file = path.join(dir, `page-${String(offset).padStart(6, '0')}.json`);
  fs.writeFileSync(file, r.text);
  receipt.pages.push({ offset, limit: body.pagination.limit, rows: bag.length, response_sha256: sha(fs.readFileSync(file)), raw_path: path.relative(dataPath(), file).replaceAll('\\', '/') });
  rows += bag.length; offset += bag.length;
  console.error(`page offset=${body.pagination.offset} rows=${bag.length} total=${rows}/${receipt.provider_count}`);
  if (bag.length === 0) { receipt.stop_reason = 'empty_page'; break; }
  if (receipt.provider_count !== null && rows >= receipt.provider_count) { receipt.stop_reason = 'exhausted'; break; }
  if (rows >= maxRows) { receipt.stop_reason = 'max_rows'; break; }
  await new Promise(res => setTimeout(res, 200)); // requests are strictly sequential (~2 s each); 200 ms keeps us far under the ODP 4-15 req/s rate
} } catch (e) {
  if (!isBudgetStop(e) && e.code !== 'FRAME_LOCAL_BUDGET') throw e;
  receipt.error = e.message; receipt.stop_reason = 'error'; receipt.budget_stop = true;
  process.exitCode = e.code === 'FRAME_LOCAL_BUDGET' ? 2 : BUDGET_EXIT;
}
receipt.rows_retrieved = rows;
receipt.truncated = receipt.provider_count === null || rows < receipt.provider_count;
receipt.finished_at = new Date().toISOString();
fs.writeFileSync(path.join(dir, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ dir, provider_count: receipt.provider_count, rows, truncated: receipt.truncated, stop_reason: receipt.stop_reason, error: receipt.error }));
if (receipt.error && process.exitCode !== BUDGET_EXIT && process.exitCode !== 2) process.exitCode = 1;
