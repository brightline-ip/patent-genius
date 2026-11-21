// Optional integration with the Patent Research Hub, the supported provider of USPTO / PTAB / GovInfo access.
// Patent Genius never talks to a provider directly. Every script that needs one (frame harvest, document downloads, collection) reaches the Hub
// only through its gateway command, started as a short-lived child (hub_gateway.mjs): none loads keys.env or imports a Hub module.
//   PG_HUB_CMD             full path of the Hub's Hub.cmd (the installer records it); see hub_gateway.mjs for the other ways it is found
//   PG_HUB_ROOT            install directory of the Patent Research Hub (Hub.cmd inside it). Nothing in Patent Genius imports Hub modules.
//   PG_TOOL_DATA_DIR       the ONE data folder (scope.json, corpus/, public-originals/, locks/). Default: the per-user
//                          folder %LOCALAPPDATA%\Brightline\PatentGenius-Data (XDG data home elsewhere). Writing inside the
//                          install folder is refused. This mirrors pg_tool/paths.py.
//   PG_PYTHON              Python executable used by scripts that start Python (default: python3 on Linux/macOS, python on Windows)
//   FRAME_GATEWAY_MODULE   no longer used: it is ignored (with a warning) so a replacement module can never stand in for the Hub
// With no Hub command found, a command that needs the Hub prints a clear message and exits with code 78.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const HUB_CONFIG_EXIT = 78;
export const installDir = path.dirname(fileURLToPath(import.meta.url));

export function hubNotConfigured(what) {
  console.error(`${what} needs the Patent Research Hub, which is not configured.\n` +
    'Install the Patent Research Hub (0.2.0 or later) and set PG_HUB_CMD to its Hub.cmd (or PG_HUB_ROOT to its install directory).\n' +
    'Without it, the offline commands (import, retrieve, audit, the pg_tool service, frame_build / frame_select, compact) still work.');
  process.exit(HUB_CONFIG_EXIT);
}

// Provider access for the frame scripts: { callGateway, readConfig } backed by the Hub's gateway command (a child per call).
// Exits 78 with a clear message when no Hub command is configured, before any request or budget use.
export async function importGateway(what) {
  if (process.env.FRAME_GATEWAY_MODULE) console.error('FRAME_GATEWAY_MODULE is ignored: provider access goes through the Hub gateway command only.');
  const gw = await import('./hub_gateway.mjs');
  let hub = null;
  try { hub = gw.findHub(); } catch (e) { console.error(`${what}: ${e.message}`); process.exit(HUB_CONFIG_EXIT); }
  if (!hub) hubNotConfigured(what);
  return { callGateway: gw.callGateway, readConfig: gw.readConfig };
}

// ---- identifiers (same rules as pg_tool/paths.py) ----
const RESERVED = new Set(['con', 'prn', 'aux', 'nul', ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`), ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`)]);
export const FIELD_RE = /^[a-z][a-z0-9_]{0,31}$/;
export const FRAME_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
export const isReservedName = name => RESERVED.has(String(name).split('.')[0].toLowerCase());
export function checkFrameId(id) {
  if (!FRAME_ID_RE.test(id || '') || isReservedName(id)) {
    console.error(`invalid frame id ${JSON.stringify(String(id).slice(0, 80))}: lower-case letters, digits, "_" and "-", at most 64 characters, and not a reserved Windows device name`);
    process.exit(2);
  }
  return id;
}

// ---- the data folder ----
// A ':' anywhere except in a drive letter ("C:" or "\?\C:") is NTFS alternate-data-stream syntax (file.txt:name, file.txt:name:$DATA): never read or written.
export const hasStreamSyntax = p => String(p).replace(/^(?:\\\\[?.]\\)?[A-Za-z]:/, '').includes(':');
export function checkedPath(p, what = 'path') {
  if (hasStreamSyntax(p)) throw new Error(`${what} has a ":" beyond the drive letter (NTFS alternate data streams are never read or written): ${JSON.stringify(String(p).slice(0, 80))}`);
  return p;
}
export function defaultDataDir() {
  if (process.platform === 'win32') return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'Brightline', 'PatentGenius-Data');
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'brightline', 'patent-genius-data');
}
const norm = p => (process.platform === 'win32' ? p.toLowerCase() : p);
export function isInsideInstall(p) {
  const rel = path.relative(norm(installDir), norm(path.resolve(p)));
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}
// The data folder. Refuses (exit 78) a folder inside the install folder, so data can never be written next to the program files.
export function dataDir() {
  const d = path.resolve(checkedPath(process.env.PG_TOOL_DATA_DIR || defaultDataDir(), 'the data folder'));
  if (isInsideInstall(d)) {
    console.error(`refusing to use ${d} as the data folder: it is inside the install folder (${installDir}). Set PG_TOOL_DATA_DIR to a folder outside it.`);
    process.exit(HUB_CONFIG_EXIT);
  }
  return d;
}
export const dataPath = (...parts) => path.join(dataDir(), ...parts);
export const pythonExecutable = () => process.env.PG_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

// ---- document selection (shared by acquire_family.mjs and acquire_supplement.mjs) ----
// Which USPTO document codes to download: --preset <name> (none, office-actions, prosecution, procedural), or --codes A,B,C,
// or else the documents section of scope.json in the data folder. Nothing is downloaded when no document type is selected.
export const NO_DOCUMENT_TYPES_EXIT = 64;
export function documentSelection(argv, what) {
  const positional = []; let preset = null, codes = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--preset') preset = argv[++i];
    else if (argv[i] === '--codes') codes = String(argv[++i] || '').split(',').map(s => s.trim()).filter(Boolean);
    else positional.push(argv[i]);
  }
  const presets = JSON.parse(fs.readFileSync(path.join(installDir, 'pg_tool', 'document_presets.json'), 'utf8'));
  let chosen = null;
  if (codes) chosen = codes;
  else if (preset) {
    if (!Array.isArray(presets[preset])) { console.error(`${what}: unknown preset ${JSON.stringify(preset)}; use one of: ${Object.keys(presets).filter(k => Array.isArray(presets[k])).join(', ')}`); process.exit(2); }
    chosen = presets[preset];
  } else {
    try {
      const scope = JSON.parse(fs.readFileSync(dataPath('scope.json'), 'utf8').replace(/^\uFEFF/, ''));
      const d = scope.documents || {};
      chosen = (d.preset || 'none') === 'custom' ? (d.codes || []) : (presets[d.preset || 'none'] || []);
    } catch { chosen = []; }
  }
  if (!chosen.length) {
    console.error(`${what}: no document types selected. Pass --preset (office-actions, prosecution, procedural) or --codes A,B,C, or set documents.preset in scope.json in your data folder. Nothing was requested.`);
    process.exit(NO_DOCUMENT_TYPES_EXIT);
  }
  return { positional, codes: chosen };
}
