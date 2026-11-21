// Supplementary download for applications already in the corpus: procedural documents that the first acquisition did
// not prioritise (election responses, interview summaries, after-final pilot requests, appeal papers). Uses the saved
// inventory.json (no inventory requests), skips anything already normalized, and downloads with acquire_round.mjs.
// Writes a receipt to corpus/acquisitions/.
// Usage: node acquire_supplement.mjs [max_per_app=12] [--preset <name> | --codes A,B,C]
// Which document types are downloaded comes from --preset / --codes, else from scope.json (documents). With none selected, nothing is requested (exit 64).
// Needs corpus/normalized/ from an earlier acquire_family run; exits 66 when it is missing.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dataPath, documentSelection } from './hub_config.mjs';

const { positional, codes: SUPPLEMENT } = documentSelection(process.argv.slice(2), 'acquire_supplement.mjs');
const cap = Number(positional[0] || 12);
if (!(cap >= 1 && cap <= 40)) throw new Error('usage: node acquire_supplement.mjs [max_per_app 1..40]');
const readJson = p => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return null; } };
const form = d => d.xml_available ? 'xml' : d.docx_file_id ? 'docx' : d.status === 'AUTHENTICATED_DOWNLOAD_REQUIRED' ? 'pdf'
  : d.status === 'AUTHENTICATED_FILE_DOWNLOAD_REQUIRED' && d.file_id ? 'file' : null;
const spec = d => ({ xml: d.document_id + '@xml', docx: d.document_id + '@docx:' + d.docx_file_id, pdf: d.document_id, file: d.document_id + '@file:' + d.file_id })[form(d)];
const staged = d => d.document_id + ({ xml: '.tar', docx: '.docx' }[form(d)] || '.pdf');

const receipt = { kind: 'supplement', codes: SUPPLEMENT, per_app_cap: cap, started_at: new Date().toISOString(), applications: [] };
if (!fs.existsSync(dataPath('corpus', 'normalized'))) {
  console.error(`acquire_supplement.mjs: ${dataPath('corpus', 'normalized')} does not exist. Run acquire_family.mjs first (it downloads and you normalize the first documents); nothing was requested.`);
  process.exit(66);
}
const apps = fs.readdirSync(dataPath('corpus', 'normalized')).filter(a => /^\d{8}$/.test(a) && fs.existsSync(dataPath('public-originals', a, 'inventory.json'))).sort();
for (const app of apps) {
  const dir = dataPath('public-originals', app);
  const inv = readJson(path.join(dir, 'inventory.json')) || {};
  const index = (readJson(dataPath('corpus', 'normalized', app, 'index.json')) || {}).documents || {};
  const docs = (inv.documents || []).filter(d => SUPPLEMENT.includes(d.document_code) && !index[d.document_id])
    .sort((a, b) => SUPPLEMENT.indexOf(a.document_code) - SUPPLEMENT.indexOf(b.document_code) || String(a.official_date).localeCompare(String(b.official_date)));
  if (!docs.length) continue;
  const rec = { application: app, wanted: docs.length, not_retrievable: docs.filter(d => !form(d)).map(d => ({ document_id: d.document_id, code: d.document_code, hub_status: d.status })) };
  const ids = docs.filter(form).slice(0, cap).filter(d => !fs.existsSync(path.join(dir, staged(d)))).map(spec);
  rec.omitted_by_cap = Math.max(0, docs.filter(form).length - cap);
  try {
    if (ids.length) {
      const out = execFileSync(process.execPath, [fileURLToPath(new URL('./acquire_round.mjs', import.meta.url)), app, String(cap), ...ids], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });
      rec.acquire = JSON.parse(out.trim().split('\n').pop());
    } else rec.acquire = { acquired: 0, failures: 0, note: 'already staged' };
  } catch (e) { rec.error = String(e.message || e).slice(0, 300); }  // one application failing does not stop the run
  receipt.applications.push(rec);
  console.log(JSON.stringify({ app, wanted: rec.wanted, acquire: rec.acquire, error: rec.error }));
}
receipt.finished_at = new Date().toISOString();
const outDir = dataPath('corpus', 'acquisitions'); fs.mkdirSync(outDir, { recursive: true });
const out = path.join(outDir, `supplement-procedural-${Date.now()}.json`);
fs.writeFileSync(out, JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ receipt: out, applications: receipt.applications.length, errors: receipt.applications.filter(a => a.error).length,
  wanted: receipt.applications.reduce((s, a) => s + a.wanted, 0) }));
