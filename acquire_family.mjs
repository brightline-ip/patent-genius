// Download prosecution documents for the first N selected families of a selection frame (draw order
// from frame_select.py, so the first N are themselves a random subsample). Per application: fetch the
// document inventory (no store), keep inventory.json, choose prosecution documents by code priority up
// to a cap, then download them with acquire_round.mjs (project-local staging, hash-verified, no overwrite).
// Usage: node acquire_family.mjs <selection-frame.json[.gz]> <n_families> <per_app_cap> [--preset <name> | --codes A,B,C]
// Which document types are downloaded comes from --preset / --codes, else from scope.json (documents). With none selected, nothing is requested (exit 64).
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { gunzipSync } from 'node:zlib';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { importGateway, dataPath, documentSelection, checkedPath } from './hub_config.mjs';
await importGateway('acquire_family.mjs'); // exits 78 when no Hub command is configured
import { patentDocumentInventory } from './hub_documents.mjs';

// Document codes in download priority order (the order of the preset or of --codes); drawings are images and skipped.
const { positional, codes: PRIORITY } = documentSelection(process.argv.slice(2), 'acquire_family.mjs');
const RENDERER = 3; // compact.py RENDERER: XML documents normalized by an older renderer are fetched again
const INVENTORY_ATTEMPTS = [0, 4000, 15000]; // first try, then two retries (first-attempt timeouts are frequent)
const DEFER_PAUSE_MS = 30000; // families whose inventory still fails get one more pass at the end of the run

const [framePath, nArg, capArg] = positional;
const n = Number(nArg), cap = Number(capArg);
if (!framePath || !(n >= 1 && n <= 30) || !(cap >= 1 && cap <= 40)) throw new Error('usage: node acquire_family.mjs <selection-frame.json> <n_families 1..30> <per_app_cap 1..40>');
const frameBytes = fs.readFileSync(checkedPath(framePath, 'the frame path'));
const frame = JSON.parse((framePath.endsWith('.gz') ? gunzipSync(frameBytes) : frameBytes).toString('utf8'));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const readJson = p => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return {}; } };

// Form per document, best first: USPTO XML (exact text), native DOCX (as-filed APP.TEXT), document-form PDF,
// file-form PDF (e-filed amendments). Anything else is recorded with the Hub's status and skipped.
const form = d => d.xml_available ? 'xml' : d.docx_file_id ? 'docx' : d.status === 'AUTHENTICATED_DOWNLOAD_REQUIRED' ? 'pdf'
  : d.status === 'AUTHENTICATED_FILE_DOWNLOAD_REQUIRED' && d.file_id ? 'file' : null;
const spec = d => ({ xml: d.document_id + '@xml', docx: d.document_id + '@docx:' + d.docx_file_id, pdf: d.document_id, file: d.document_id + '@file:' + d.file_id })[form(d)];
const staged = d => d.document_id + ({ xml: '.tar', docx: '.docx' }[form(d)] || '.pdf');
const SOURCE = { xml: 'uspto-xmlarchive', docx: 'uspto-docx' };

async function inventory(app, dir, rec) {
  const invPath = path.join(dir, 'inventory.json');
  let inv = fs.existsSync(invPath) ? JSON.parse(fs.readFileSync(invPath, 'utf8')) : null;
  // Inventories saved before the Hub recorded xml_available/docx_file_id are refreshed; if the refresh fails the
  // saved inventory is kept, so only an application with no inventory at all can fail its family.
  const stale = inv && !(inv.documents || []).some(d => 'docx_file_id' in d);
  if (inv && !stale) return inv;
  let lastError = null;
  for (const wait of INVENTORY_ATTEMPTS) {
    if (wait) await sleep(wait);
    try { const fresh = await patentDocumentInventory({ applicationNumber: app }); fs.writeFileSync(invPath, JSON.stringify(fresh, null, 2) + '\n'); return fresh; }
    catch (e) { lastError = e; }
  }
  if (stale) { rec.inventory_refresh_failed = String(lastError?.message || lastError).slice(0, 120); return inv; }
  throw Object.assign(new Error('inventory: ' + String(lastError?.message || lastError)), { inventoryFailure: true });
}

async function processFamily(fam, applications) {
  for (const app of fam.application_numbers) {
    const dir = dataPath('public-originals', app); fs.mkdirSync(dir, { recursive: true });
    const rec = { family_key: fam.family_key, stratum: fam.stratum, application: app };
    try {
      const inv = await inventory(app, dir, rec);
      const docs = (inv.documents || []).filter(d => PRIORITY.includes(d.document_code));
      docs.sort((a, b) => PRIORITY.indexOf(a.document_code) - PRIORITY.indexOf(b.document_code) || String(a.official_date).localeCompare(String(b.official_date)));
      // Skip what is already normalized, except: OCR is upgraded when XML/DOCX is offered, and XML/DOCX renderings
      // from an older renderer are fetched again (archives are pruned after normalization).
      const index = readJson(dataPath('corpus', 'normalized', app, 'index.json')).documents || {};
      const done = d => { const e = index[d.document_id]; if (!e) return false; const want = SOURCE[form(d)];
        return want ? e.source === want && e.renderer === RENDERER : true; };
      rec.not_retrievable = docs.filter(d => !form(d)).map(d => ({ document_id: d.document_id, code: d.document_code, hub_status: d.status }));
      const capped = docs.filter(form).slice(0, cap);
      rec.inventory_documents = (inv.documents || []).length;
      rec.eligible_documents = docs.length; rec.omitted_by_cap = Math.max(0, docs.filter(form).length - cap);
      rec.forms = Object.fromEntries(['xml', 'docx', 'pdf', 'file'].map(f => [f, capped.filter(d => form(d) === f).length]));
      rec.codes = Object.fromEntries(PRIORITY.map(c => [c, docs.filter(d => d.document_code === c).length]).filter(([, k]) => k));
      const ids = capped.filter(d => !done(d) && !fs.existsSync(path.join(dir, staged(d)))).map(spec);
      if (ids.length) {
        const out = execFileSync(process.execPath, [fileURLToPath(new URL('./acquire_round.mjs', import.meta.url)), app, String(cap), ...ids], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });
        rec.acquire = JSON.parse(out.trim().split('\n').pop());
      } else rec.acquire = { acquired: 0, failures: 0, note: 'nothing new to download' };
    } catch (e) {
      rec.error = String(e.message || e);
      applications.push(rec); console.log(JSON.stringify({ app, error: rec.error }));
      if (e.inventoryFailure) return false;
      continue;
    }
    applications.push(rec);
    console.log(JSON.stringify({ app, docs: rec.eligible_documents, forms: rec.forms, acquire: rec.acquire, refresh_failed: rec.inventory_refresh_failed }));
  }
  return true;
}

// Families in draw order. A family whose inventory fails even after the retries is DEFERRED (not replaced) and
// tried once more at the end of the run; only if it fails again is it replaced by the next family in draw order.
const families = frame.selection.selected;
const receipt = { frame_id: frame.frame_id, n_families: n, per_app_cap: cap, priority: PRIORITY, started_at: new Date().toISOString(),
  replacement_rule: 'inventory failure after retries defers the family to a second pass; a family failing twice is replaced by the next in draw order',
  applications: [], families_used: [], families_deferred: [], families_replaced: [] };
let next = 0; const deferred = [];
while (receipt.families_used.length < n) {
  if (receipt.families_used.length + deferred.length < n && next < families.length) {
    const rank = next++, fam = families[rank];
    if (await processFamily(fam, receipt.applications)) receipt.families_used.push({ draw_rank: rank + 1, family_key: fam.family_key });
    else { deferred.push(rank); receipt.families_deferred.push({ draw_rank: rank + 1, family_key: fam.family_key }); }
  } else if (deferred.length) {
    const rank = deferred.shift(), fam = families[rank];
    await sleep(DEFER_PAUSE_MS);
    if (await processFamily(fam, receipt.applications)) receipt.families_used.push({ draw_rank: rank + 1, family_key: fam.family_key, second_pass: true });
    else receipt.families_replaced.push({ draw_rank: rank + 1, family_key: fam.family_key });
  } else break; // selection exhausted
}
receipt.finished_at = new Date().toISOString();
const outDir = dataPath('corpus', 'acquisitions'); fs.mkdirSync(outDir, { recursive: true });
const out = path.join(outDir, `${frame.frame_id}-first${n}-${Date.now()}.json`);
fs.writeFileSync(out, JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ receipt: out, applications: receipt.applications.length, errors: receipt.applications.filter(a => a.error).length,
  families_used: receipt.families_used.length, deferred: receipt.families_deferred.map(f => f.draw_rank), replaced: receipt.families_replaced.map(f => f.draw_rank) }));
