// One bounded ODP search dry run through the existing Hub gateway route (no store).
// Usage: node frame_dryrun.mjs "<q>" <limit>   — writes public-originals/frames/dryrun-<ts>.json
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { importGateway, dataPath } from './hub_config.mjs';
const { callGateway, readConfig } = await importGateway('frame_dryrun.mjs');
const [q, limitArg] = process.argv.slice(2);
const limit = Number(limitArg || 1);
if (!q || !(limit >= 1 && limit <= 100)) throw new Error('usage: node frame_dryrun.mjs "<q>" <limit 1..100>');
const url = 'https://api.uspto.gov/api/v1/patent/applications/search';
const body = { q, pagination: { offset: 0, limit } };
const started = new Date().toISOString();
const out = dataPath('public-originals', 'frames'); fs.mkdirSync(out, { recursive: true });
const rec = { route: 'uspto', source: 'odp', url, body, started_at: started };
try {
  const r = await callGateway('uspto', { source: 'odp', url, method: 'POST', body }, { config: readConfig() });
  rec.status = r.status; rec.contentType = r.contentType; rec.response_sha256 = createHash('sha256').update(String(r.text)).digest('hex');
  let parsed = null; try { parsed = JSON.parse(r.text); } catch { rec.parse_error = true; }
  rec.raw_text = r.text; rec.parsed_summary = parsed && typeof parsed === 'object' ? { keys: Object.keys(parsed), count: parsed.count, bag_rows: Array.isArray(parsed.patentFileWrapperDataBag) ? parsed.patentFileWrapperDataBag.length : null } : null;
} catch (e) { rec.error = String(e.message || e); }
rec.finished_at = new Date().toISOString();
const file = path.join(out, `dryrun-${started.replace(/[:.]/g, '-')}.json`);
fs.writeFileSync(file, JSON.stringify(rec, null, 2) + '\n');
console.log(JSON.stringify({ file, status: rec.status, error: rec.error, summary: rec.parsed_summary }, null, 1));
