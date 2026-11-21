// File-wrapper documents through the Hub gateway (routes application-documents and application-pdf). No Hub module is imported and no
// key is held by this process: each call is a short-lived `Hub.cmd gateway` child (hub_gateway.mjs). An original can be tens of MB, so the
// answer is read in full, decoded, size- and signature-checked here, and handed to the caller's stage() function, which writes it.
//   patentDocumentInventory({applicationNumber})                         -> inventory object (documents with code, date, form, status)
//   acquirePatentDocumentOriginal({applicationNumber, documentIdentifier, fileId?, format?}, {stage})
//        format: 'pdf' or 'docx' (with fileId), or 'xmlarchive' (an uncompressed TAR of XML); none = the document-form PDF.
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { callGateway } from './hub_gateway.mjs';

export const MAX_DOCUMENT_BYTES = 40 * 1024 * 1024;
const APP = /^\d{8}$/, DOC = /^[A-Za-z0-9_-]{1,100}$/, FILE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$/;
const base = (app, doc) => `https://api.uspto.gov/api/v1/download/applications/${app}/${doc}`;

// The request body and what the answer must be (extension and URL); anything outside the bounded contract throws.
export function originalIdentity({ applicationNumber, documentIdentifier, fileId, format }) {
  if (!APP.test(applicationNumber || '') || !DOC.test(documentIdentifier || '')) throw new Error('Invalid public application/document identity');
  let url = base(applicationNumber, documentIdentifier) + '.pdf', extension = 'pdf', file_id = null;
  if (format === 'xmlarchive') {
    if (fileId) throw new Error('xmlarchive takes no file identifier');
    url = base(applicationNumber, documentIdentifier) + '/xmlarchive'; extension = 'tar';
  } else if (format === 'pdf' || format === 'docx') {
    if (!FILE.test(fileId || '')) throw new Error('Invalid document file identifier');
    file_id = fileId; url = `${base(applicationNumber, documentIdentifier)}/files/${fileId}.${format}`; extension = format;
  } else if (format !== undefined && format !== null) throw new Error('Unsupported document format');
  else if (fileId) throw new Error('A file identifier needs a format');
  return { applicationNumber, documentIdentifier, file_id, format: format ?? null, url, extension };
}

const TYPES = { pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', tar: 'application/x-tar' };

// Size and signature of the bytes. A TAR is checked by its first header block's checksum and 512-byte framing only: never extracted here.
export function verifyBytes(b, extension) {
  const bad = () => new Error('Original size or signature mismatch');
  if (!b || b.length < 5 || b.length > MAX_DOCUMENT_BYTES) throw bad();
  if (extension === 'pdf') { if (b.subarray(0, 5).toString('ascii') !== '%PDF-') throw bad(); }
  else if (extension === 'docx') { if (b.subarray(0, 2).toString('ascii') !== 'PK') throw bad(); }
  else if (extension === 'tar') {
    if (b.length < 1024 || b.length % 512 !== 0) throw bad();
    const field = b.subarray(148, 156).toString('ascii').replace(/[\u0000 ]+$/, '').trim();
    if (!/^[0-7]{1,8}$/.test(field)) throw bad();
    let sum = 0; for (let k = 0; k < 512; k++) sum += (k >= 148 && k < 156) ? 32 : b[k];
    if (sum !== parseInt(field, 8)) throw bad();
  } else throw new Error('Unsupported original extension');
}

export function decodeOriginal(envelope, id) {
  if (envelope?.status !== 200) throw new Error(`USPTO did not return the document (status ${Number(envelope?.status) || 0})`);
  if (envelope.application_number !== id.applicationNumber || envelope.document_identifier !== id.documentIdentifier || envelope.extension !== id.extension || envelope.url !== id.url || (id.file_id && envelope.file_id !== id.file_id)) throw new Error('Application-document retrieval failed or identity changed');
  if (String(envelope.contentType || '').split(';')[0].trim().toLowerCase() !== TYPES[id.extension]) throw new Error('Application document has an unexpected content type');
  const text = envelope.base64;
  if (typeof text !== 'string' || !text.length || text.length % 4 || text.length > 4 * Math.ceil(MAX_DOCUMENT_BYTES / 3) || !/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new Error('Invalid or oversized application-document encoding');
  const bytes = Buffer.from(text, 'base64');
  if (bytes.length !== envelope.bytes || bytes.toString('base64') !== text) throw new Error('Application-document size or signature verification failed');
  verifyBytes(bytes, id.extension);
  return { bytes, sha256: createHash('sha256').update(bytes).digest('hex') };
}

// Writes a verified original into `dir` as <docId>.<ext>: to a temporary file first, read back and hash-checked, then renamed into place. A
// mismatch deletes the temporary file, so a partial or wrong file never has the staged name (acquire_family treats that name as "done").
// Removes temporary files that an earlier run of this program left in `dir` (<name>.part-<pid>) when that process no longer exists.
export function sweepPartFiles(dir) {
  let names = []; try { names = fs.readdirSync(dir); } catch { return 0; }
  let removed = 0;
  for (const n of names) {
    const m = /\.part-(\d+)$/.exec(n); if (!m) continue;
    const pid = Number(m[1]); let alive = pid === process.pid;
    if (!alive) { try { process.kill(pid, 0); alive = true; } catch (e) { alive = e.code === 'EPERM'; } }
    if (!alive) { try { fs.unlinkSync(path.join(dir, n)); removed += 1; } catch { /* leave it */ } }
  }
  return removed;
}

export function stageVerified(dir, docId, bytes, sha256, extension) {
  if (!['pdf', 'tar', 'docx'].includes(extension)) throw new Error('unexpected staged extension ' + extension);
  sweepPartFiles(dir);
  const target = path.join(dir, `${docId}.${extension}`);
  if (fs.existsSync(target)) throw new Error(`refusing to overwrite ${target}`);
  const tmp = `${target}.part-${process.pid}`;
  try {
    const fd = fs.openSync(tmp, 'wx');
    try { fs.writeSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    const back = fs.readFileSync(tmp);
    const onDisk = createHash('sha256').update(back).digest('hex');
    if (onDisk !== sha256 || back.length !== bytes.length) throw new Error('staged hash mismatch');
    fs.renameSync(tmp, target);
    return { path: target, sha256: onDisk, bytes: bytes.length };
  } catch (e) { try { fs.unlinkSync(tmp); } catch { /* nothing to remove */ } throw e; }
}

export async function acquirePatentDocumentOriginal(input, { stage }) {
  const id = originalIdentity(input);
  const body = { application_number: id.applicationNumber, document_identifier: id.documentIdentifier, ...(id.file_id ? { file_id: id.file_id } : {}), ...(id.format ? { format: id.format } : {}) };
  const envelope = await callGateway('application-pdf', body);
  const { bytes, sha256 } = decodeOriginal(envelope, id);
  const staged = await stage(bytes, sha256, null, id.extension);
  return { version: 1, status: 'ORIGINAL_VERIFIED', application_number: id.applicationNumber, document_id: id.documentIdentifier, file_id: id.file_id, format: id.format, extension: id.extension,
    source_url: id.url, sha256, bytes: bytes.length, staged_path: staged.path ?? staged, normalization_status: 'NOT_SUBMITTED' };
}

// ---- inventory --------------------------------------------------------------------------------------------------------
const text = (v, max) => typeof v === 'string' && v.length <= max && !/[\x00-\x1f\x7f]/.test(v) ? v : null;

export function parseInventory(rows, applicationNumber) {
  if (!Array.isArray(rows) || rows.length > 20000) throw new Error('USPTO document inventory schema or size is unsupported');
  const documents = [], issues = [], seen = new Set();
  rows.forEach((row, index) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) throw new Error('Invalid USPTO inventory entry');
    const id = text(row.documentIdentifier, 100);
    if (!id || !DOC.test(id) || row.applicationNumberText !== applicationNumber) { issues.push({ row: index, status: 'UNRESOLVED_IDENTITY' }); return; }
    if (seen.has(id)) { issues.push({ row: index, status: 'DUPLICATE_DOCUMENT_ID' }); return; }
    seen.add(id);
    const url = base(applicationNumber, id) + '.pdf', options = row.downloadOptionBag;
    if (options !== undefined && (!Array.isArray(options) || options.length > 30)) throw new Error('Invalid download-option inventory');
    const all = options || [], pdf = all.filter(o => o && ['PDF', 'application/pdf'].includes(o.mimeTypeIdentifier));
    const matching = pdf.filter(o => o.downloadUrl === url);
    const filePrefix = url.slice(0, -'.pdf'.length) + '/files/';
    const fileForm = pdf.filter(o => typeof o.downloadUrl === 'string' && o.downloadUrl.startsWith(filePrefix) && /^[A-Za-z0-9][A-Za-z0-9_-]{0,119}\.pdf$/.test(o.downloadUrl.slice(filePrefix.length)));
    const unknown = pdf.filter(o => o.downloadUrl !== url && !fileForm.includes(o));
    const xmlAvailable = all.some(o => o && o.mimeTypeIdentifier === 'XML' && o.downloadUrl === url.slice(0, -'.pdf'.length) + '/xmlarchive');
    const docx = all.filter(o => o && o.mimeTypeIdentifier === 'MS_WORD' && typeof o.downloadUrl === 'string' && o.downloadUrl.startsWith(filePrefix) && /^[A-Za-z0-9][A-Za-z0-9_-]{0,119}\.docx$/.test(o.downloadUrl.slice(filePrefix.length)));
    const docxFileId = docx.length === 1 ? docx[0].downloadUrl.slice(filePrefix.length, -'.docx'.length) : null;
    let status = 'NO_SUPPORTED_PDF', chosen = null, fileId = null;
    if (unknown.length) status = 'UNRECOGNIZED_DOWNLOAD_DESTINATION';
    else if (matching.length === 1) { status = 'AUTHENTICATED_DOWNLOAD_REQUIRED'; chosen = matching[0]; }    // the document form wins when both exist
    else if (matching.length > 1 || fileForm.length > 1) status = 'AMBIGUOUS_PDF_OPTIONS';
    else if (fileForm.length === 1) { status = 'AUTHENTICATED_FILE_DOWNLOAD_REQUIRED'; chosen = fileForm[0]; fileId = chosen.downloadUrl.slice(filePrefix.length, -'.pdf'.length); }
    const pages = chosen?.pageTotalQuantity;
    documents.push({ application_number: applicationNumber, document_id: id, document_code: text(row.documentCode, 80), description: text(row.documentCodeDescriptionText, 500),
      official_date: text(row.officialDate, 80), direction: text(row.directionCategory ?? row.documentDirectionCategory, 40), page_count: Number.isSafeInteger(pages) && pages >= 0 ? pages : null,
      url: chosen ? chosen.downloadUrl : null, file_id: fileId, xml_available: xmlAvailable, docx_file_id: docxFileId, status, original_acquired: false });
  });
  return { version: 1, application_number: applicationNumber, provider_rows: rows.length, unique_documents: documents.length, documents, issues,
    warnings: ['Public file-wrapper inventory only; no originals have been downloaded or normalized.'] };
}

export async function patentDocumentInventory({ applicationNumber }) {
  if (!APP.test(applicationNumber || '')) throw new Error('Use an eight-digit US application number');
  const envelope = await callGateway('application-documents', { application_number: applicationNumber });
  if (envelope?.application_number !== applicationNumber || envelope.status !== 200) throw new Error(`Unrecognized or failed USPTO inventory response (status ${Number(envelope?.status) || 0})`);
  const raw = envelope.body;
  const rows = Array.isArray(raw) ? raw : raw?.documentBag;
  const serialized = JSON.stringify(raw ?? null);
  const inv = parseInventory(rows, applicationNumber);
  return { ...inv, response_sha256: createHash('sha256').update(serialized).digest('hex'), retrieved_at: envelope.retrieved_at || new Date().toISOString() };
}
