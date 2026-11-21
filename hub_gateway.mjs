// Provider access for the frame scripts: the Patent Research Hub's gateway command, started as a short-lived child.
//   "<Hub install>\Hub.cmd" gateway <route> --json-in     one JSON request on stdin, one JSON document on stdout
// This process never reads keys.env, never holds a provider key and never imports a Hub module for these calls (Hub 0.2.0 or later).
// It mirrors pg_tool/hubgw.py: where the command is found, the exit codes, and the test-only stand-in.
//   PG_HUB_CMD             full path of Hub.cmd (the installer records it)
//   PG_HUB_ROOT            the Hub's install folder (alias PG_TOOL_HUB_ROOT); Hub.cmd inside it
//   PG_HUB_DISCOVER=0      do not look in the Hub installer's default folder
//   PG_HUB_TEST_STUB       tests only: a JSON list that replaces the command; honoured only with PG_ALLOW_TEST_STUB=1 and no provider key variable set
// Exit 0: the provider envelope ({text, status, contentType}); a provider status such as 429 is returned, not thrown.
// Exit 75 (local limit or busy): wait retry_after_seconds and ask again (at most PG_GATEWAY_MAX_WAIT_S in total, default 3600).
// Any other exit throws an Error with .code (not_configured, bad_request, gateway_error, hub_too_old, hub_unavailable, timeout, local_limit).
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export const KEY_VAR = /(API_KEY|_TOKEN|_SECRET|CONSUMER_KEY|_PASSWORD)$/i;
const CALL_TIMEOUT_MS = 900000;
// No gateway child outlives this process: on exit or Ctrl+C the whole tree (Hub.cmd and its node child) is stopped.
const live = new Set();
const killTree = child => { try { if (process.platform === 'win32') spawnSync('taskkill', ['/T', '/F', '/PID', String(child.pid)], { windowsHide: true, stdio: 'ignore' }); else child.kill('SIGKILL'); } catch { /* best effort */ } };
// Handlers are installed when the first child is started, not when this file is imported (an importer's own signal handling is never taken over). On a
// signal the children are stopped; the process is ended only when no other handler for that signal exists, by re-raising the signal with the default action.
let handlersInstalled = false;
function installHandlers() {
  if (handlersInstalled) return; handlersInstalled = true;
  process.on('exit', () => { for (const c of live) killTree(c); });
  for (const sig of ['SIGINT', 'SIGTERM']) {
    const handler = () => {
      for (const c of live) killTree(c);
      if (process.listenerCount(sig) <= 1) { process.removeListener(sig, handler); process.kill(process.pid, sig); }
    };
    process.on(sig, handler);
  }
}
const fail = (code, message, extra = {}) => Object.assign(new Error(message), { code, ...extra });
export const keyVariablesSet = (env = process.env) => Object.keys(env).filter(k => env[k] && KEY_VAR.test(k)).sort();

export function findHub(env = process.env) {
  if (env.PG_HUB_TEST_STUB) {
    let argv = null;
    try { argv = JSON.parse(env.PG_HUB_TEST_STUB); } catch { /* refused below */ }
    if (env.PG_ALLOW_TEST_STUB !== '1' || keyVariablesSet(env).length) throw fail('test_stub_refused', 'PG_HUB_TEST_STUB is a test-only setting: it needs PG_ALLOW_TEST_STUB=1 and no provider key variable in the environment');
    if (!Array.isArray(argv) || !argv.length || !argv.every(a => typeof a === 'string')) throw fail('test_stub_refused', 'PG_HUB_TEST_STUB must be a JSON list of strings');
    return { argv, source: 'PG_HUB_TEST_STUB', stub: true };
  }
  if (env.PG_HUB_CMD) return { argv: [env.PG_HUB_CMD], source: 'PG_HUB_CMD', stub: false };
  const root = env.PG_HUB_ROOT || env.PG_TOOL_HUB_ROOT;
  if (root) return { argv: [path.join(root, 'Hub.cmd')], source: 'PG_HUB_ROOT', stub: false };
  if (process.platform === 'win32' && env.PG_HUB_DISCOVER !== '0' && env.LOCALAPPDATA) {
    const p = path.join(env.LOCALAPPDATA, 'Brightline', 'PatentResearchHub', 'Hub.cmd');
    if (fs.existsSync(p)) return { argv: [p], source: 'default install folder', stub: false };
  }
  return null;
}

function run(hub, args, payload, timeoutMs) {
  const env = { ...process.env };
  if (hub.stub) for (const k of keyVariablesSet(env)) delete env[k];
  let cmd, argv, opts = { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true };
  const first = hub.argv[0];
  if (!hub.stub && process.platform === 'win32' && /\.(cmd|bat)$/i.test(first)) {
    cmd = process.env.ComSpec || 'cmd.exe'; argv = ['/d', '/s', '/c', `""${first}" ${args.join(' ')}"`]; opts.windowsVerbatimArguments = true;
  } else { cmd = first; argv = [...hub.argv.slice(1), ...args]; }
  return new Promise((resolve, reject) => {
    let child; const out = [], err = [];
    try { installHandlers(); child = spawn(cmd, argv, opts); live.add(child); } catch (e) { return reject(fail('hub_unavailable', `the Hub command could not be started (${e.code || e.name})`)); }
    // Killing only Hub.cmd would leave its node child finishing the provider call: stop the whole tree. stdout is collected without a size limit.
    const timer = setTimeout(() => {
      if (process.platform === 'win32') { try { spawn('taskkill', ['/T', '/F', '/PID', String(child.pid)], { windowsHide: true, stdio: 'ignore' }); } catch { /* best effort */ } }
      child.kill();
      reject(fail('timeout', `the Hub gateway did not answer within ${timeoutMs / 1000} s; it was stopped with its child processes. The provider call MAY have happened.`, { mayHaveHappened: true }));
    }, timeoutMs);
    child.on('error', e => { clearTimeout(timer); reject(fail('hub_unavailable', `the Hub command could not be started (${e.code || e.name}); set PG_HUB_CMD to its Hub.cmd`)); });
    child.stdout.on('data', c => out.push(c)); child.stderr.on('data', c => err.push(c));
    child.on('close', code => { live.delete(child); clearTimeout(timer); resolve({ code, stdout: Buffer.concat(out).toString('utf8') }); });
    child.stdin.on('error', () => {});
    child.stdin.end(payload ?? '');
  });
}

const parse = s => { try { const v = JSON.parse(s || ''); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch { return null; } };

function raiseFor(p, doc) {
  const e = doc && doc.error;
  if (!e || typeof e !== 'object') throw fail('hub_too_old', 'The Hub did not answer in the gateway format. It is older than 0.2.0 (or this is not the Patent Research Hub). Install Patent Research Hub 0.2.0 or later.');
  const msg = String(e.message || 'the Hub gateway failed').slice(0, 300);
  if (e.code === 'local_state') throw fail('local_state', msg);
  if (p.code === 75 || e.code === 'local_limit' || e.code === 'local_busy') throw fail('local_limit', msg, { retryAfterSeconds: Number.isFinite(Number(e.retry_after_seconds)) ? Math.max(0, Number(e.retry_after_seconds)) : 60 });
  if (p.code === 2 || e.code === 'not_configured') throw fail('not_configured', msg);
  if (p.code === 64 || e.code === 'bad_request') throw fail('bad_request', msg);
  throw fail('gateway_error', msg);
}

export async function callGateway(route, body) {
  const hub = findHub();
  if (!hub) throw fail('hub_unavailable', 'the Patent Research Hub is not configured: set PG_HUB_CMD to its Hub.cmd (or PG_HUB_ROOT to its folder)');
  const maxWait = Number(process.env.PG_GATEWAY_MAX_WAIT_S || 3600) * 1000;
  let waited = 0;
  for (;;) {
    const p = await run(hub, ['gateway', route, '--json-in'], JSON.stringify(body), CALL_TIMEOUT_MS);
    const doc = parse(p.stdout);
    if (p.code === 0) { if (!doc || !('status' in doc)) throw fail('gateway_error', 'the Hub gateway answered with something that is not a provider envelope'); return doc; }
    try { raiseFor(p, doc); }
    catch (e) {
      if (e.code !== 'local_limit') throw e;
      const ms = Math.round(e.retryAfterSeconds * 1000);
      if (waited + ms > maxWait) throw e;
      waited += ms; console.error(`the Hub asks for a wait of ${e.retryAfterSeconds} s (${e.message.slice(0, 80)})`);
      await new Promise(r => setTimeout(r, ms));
    }
  }
}

export const readConfig = () => ({});
export const hubCommandAvailable = () => { try { return findHub() !== null; } catch { return true; /* a refused stub is reported by the call itself */ } };
