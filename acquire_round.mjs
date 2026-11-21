// Bounded acquisition of public originals through the Patent Research Hub gateway command (route application-pdf; hub_documents.mjs),
// staged ONLY into this project. No store (no cache writes), no
// corpus intake. Usage: node acquire_round.mjs <app> <cap> <docId>...
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { importGateway, dataPath } from './hub_config.mjs';
await importGateway('acquire_round.mjs'); // exits 78 with a clear message when no Hub command is configured
import { acquirePatentDocumentOriginal, stageVerified } from './hub_documents.mjs';

const [app, capArg, ...ids] = process.argv.slice(2);
const cap = Number(capArg);
if (!/^\d{8}$/.test(app) || !(cap > 0) || ids.length === 0) throw new Error('usage: node acquire_round.mjs <8-digit app> <cap> <docId>...');
if (ids.length > cap) throw new Error(`requested ${ids.length} documents exceeds approved cap ${cap}`);

const dir = dataPath('public-originals', app);
fs.mkdirSync(dir, { recursive: true });
const receipt = { application: app, cap, started_at: new Date().toISOString(), status: 'ACQUISITION_ONLY_PAIRING_UNVERIFIED', acquired: [], failures: [] };

// A document spec is "<docId>" (document-form PDF), "<docId>@file:<fileId>" (file-form PDF), "<docId>@docx:<fileId>" (native DOCX) or "<docId>@xml"
// (USPTO xmlarchive TAR). Staged as <docId>.pdf or <docId>.tar.
const parseSpec = spec => {
  const m = /^([A-Za-z0-9_-]{1,100})(?:@(xml|(file|docx):([A-Za-z0-9][A-Za-z0-9_-]{0,119})))?$/.exec(spec);
  if (!m) throw new Error('invalid document spec ' + spec);
  return { documentIdentifier: m[1], ...(m[2] === 'xml' ? { format: 'xmlarchive' } : m[3] ? { fileId: m[4], format: m[3] === 'docx' ? 'docx' : 'pdf' } : {}) };
};
const stage = async (bytes, sha256, root, extension = 'pdf') => stageVerified(dir, stage.current, bytes, sha256, extension);   // temp file, verify, rename (hub_documents.mjs)

for (const spec of ids) {
  const id = spec;
  try {
    const doc = parseSpec(spec);
    stage.current = doc.documentIdentifier;
    const r = await acquirePatentDocumentOriginal({ applicationNumber: app, ...doc }, { stage });
    receipt.acquired.push({ ...r, project_staged: true });
    console.error('OK', id, r.bytes, r.sha256);
  } catch (e) {
    receipt.failures.push({ document_id: id, error: String(e.message || e) });
    console.error('FAIL', id, e.message);
  }
}
receipt.finished_at = new Date().toISOString();
const out = path.join(dir, `acquisition-receipt-${receipt.started_at.slice(0, 10)}-${Date.now()}.json`);
fs.writeFileSync(out, JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ receipt: out, acquired: receipt.acquired.length, failures: receipt.failures.length }));
