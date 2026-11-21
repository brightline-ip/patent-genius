"""The only way Patent Genius reaches a provider: it starts the Patent Research Hub's gateway command as a short-lived child.

    "<Hub install>\\Hub.cmd" gateway <route> --json-in        one JSON request on stdin, one JSON document on stdout
    "<Hub install>\\Hub.cmd" gateway --limits                 the Hub's effective limits (reads configuration only)

Patent Genius never reads keys.env, never holds a provider key and never imports a Hub module for these calls: the Hub's
launcher loads the keys into the child's environment and the child exits when the call is done. Exit codes (Hub 0.2.0):
0 ok (a provider status such as 404 or 429 is RETURNED in the envelope), 1 failed, 2 not_configured (key missing),
64 bad request, 75 local limit or local busy (wait `retry_after_seconds`, then retry). A non-zero exit that carries no
`error` object is how an older Hub answers an unknown option: that is reported as `hub_too_old`.

Where the Hub's command is found, in this order:
  1. PG_HUB_CMD                      full path of Hub.cmd (the installer records it here)
  2. PG_HUB_ROOT (alias PG_TOOL_HUB_ROOT)  the Hub's install folder; Hub.cmd inside it
  3. the Hub installer's default folder (set PG_HUB_DISCOVER=0 to switch this look-up off)
A test-only stand-in (PG_HUB_TEST_STUB, a JSON list that replaces the command) works only when PG_ALLOW_TEST_STUB=1 and
no provider key variable is set in the environment, so it can never run next to real keys."""
import atexit, json, os, re, subprocess
from pathlib import Path

ROUTES_OA = 'oa-rejections'
MIN_HUB_VERSION = (0, 2, 0)
CALL_TIMEOUT_S = 900
KEY_VAR = re.compile(r'(API_KEY|_TOKEN|_SECRET|CONSUMER_KEY|_PASSWORD)$', re.I)
OLD_HUB = 'The Hub did not answer in the gateway format. It is older than 0.2.0 (or this is not the Patent Research Hub). Install Patent Research Hub 0.2.0 or later (it must support "gateway --limits").'


class HubGatewayError(Exception):
    """Any failure to use the gateway. `code` is one of: hub_unavailable, hub_too_old, not_configured, bad_request, local_limit,
    local_busy, local_state, timeout, gateway_error, test_stub_refused. retry_after_seconds is set for the two local codes."""
    def __init__(self, code, message, retry_after_seconds=None, detail=None):
        super().__init__(message); self.code, self.retry_after_seconds, self.detail = code, retry_after_seconds, detail or {}


def key_variables_set(env=None):
    return sorted(k for k, v in (env if env is not None else os.environ).items() if v and KEY_VAR.search(k))


def _default_install_cmd(env):
    if env.get('PG_HUB_DISCOVER') == '0': return None
    if os.name == 'nt':
        base = env.get('LOCALAPPDATA')
        return Path(base) / 'Brightline' / 'PatentResearchHub' / 'Hub.cmd' if base else None
    return None


def find_hub(env=None):
    """-> {'argv': [...], 'source': ..., 'stub': bool} or None. Looks only at the environment and at file names; starts nothing."""
    env = env if env is not None else os.environ
    stub = env.get('PG_HUB_TEST_STUB')
    if stub:
        if env.get('PG_ALLOW_TEST_STUB') != '1' or key_variables_set(env):
            raise HubGatewayError('test_stub_refused', 'PG_HUB_TEST_STUB is a test-only setting: it needs PG_ALLOW_TEST_STUB=1 and no provider key variable in the environment, so it can never stand in for the Hub next to real keys')
        try: argv = json.loads(stub)
        except ValueError: argv = None
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise HubGatewayError('test_stub_refused', 'PG_HUB_TEST_STUB must be a JSON list of strings')
        return {'argv': argv, 'source': 'PG_HUB_TEST_STUB', 'stub': True}
    explicit = env.get('PG_HUB_CMD')
    if explicit: return {'argv': [explicit], 'source': 'PG_HUB_CMD', 'stub': False, 'exists': Path(explicit).is_file()}
    root = env.get('PG_HUB_ROOT') or env.get('PG_TOOL_HUB_ROOT')
    if root:
        for name in ('Hub.cmd', 'hub.cmd'):
            p = Path(root) / name
            if p.is_file(): return {'argv': [str(p)], 'source': 'PG_HUB_ROOT', 'stub': False, 'exists': True}
        return {'argv': [str(Path(root) / 'Hub.cmd')], 'source': 'PG_HUB_ROOT', 'stub': False, 'exists': False}
    d = _default_install_cmd(env)
    if d is not None and d.is_file(): return {'argv': [str(d)], 'source': 'default install folder', 'stub': False, 'exists': True}
    return None


def _run(hub, args, payload, timeout, env):
    child_env = dict(env if env is not None else os.environ)
    if hub['stub']:
        for k in key_variables_set(child_env): child_env.pop(k, None)
    # Popen, not run(): Hub.cmd starts a node child, and killing only the .cmd process would leave that child finishing the provider
    # call. On a timeout the whole process tree is stopped. stdout is read to the end (a document answer can be tens of MB): no size limit.
    kw = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    try:
        proc = subprocess.Popen(hub['argv'] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', env=child_env, **kw)
    except OSError as e:
        raise HubGatewayError('hub_unavailable', f'the Hub command could not be started ({type(e).__name__}); check PG_HUB_CMD or run: python pg_cli.py doctor')
    _LIVE.add(proc)
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try: proc.communicate(timeout=5)
        except Exception: pass
        raise HubGatewayError('timeout', f'the Hub gateway did not answer within {timeout} s; it was stopped with its child processes. The provider call MAY have happened.', detail={'may_have_happened': True})
    except BaseException:           # Ctrl+C or anything else: the whole tree goes, so no call is left finishing in the background
        kill_tree(proc); _reap(proc); raise
    finally: _LIVE.discard(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _reap(proc):
    try: proc.wait(timeout=5)
    except Exception: pass
    for f in (proc.stdin, proc.stdout, proc.stderr):
        try: f.close()
        except Exception: pass


_LIVE = set()


def _kill_all_live():
    """At interpreter exit (also after an unhandled signal-turned-exception): no gateway child outlives this process."""
    for proc in list(_LIVE):
        if proc.poll() is None: kill_tree(proc)


atexit.register(_kill_all_live)


def kill_tree(proc):
    """Stop a process and everything it started (Hub.cmd -> node)."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try: proc.kill()
    except Exception: pass


def _parse(stdout):
    try:
        v = json.loads(stdout or '')
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def _need(env=None):
    hub = find_hub(env)
    if hub is None: raise HubGatewayError('hub_unavailable', 'the Patent Research Hub is not configured: set PG_HUB_CMD to its Hub.cmd (or PG_HUB_ROOT to its folder). Run: python pg_cli.py doctor')
    if not hub['stub'] and not hub.get('exists', True): raise HubGatewayError('hub_unavailable', f"the Hub command does not exist: {hub['argv'][0]}")
    return hub


def _raise_for(p, doc):
    err = doc.get('error') if doc else None
    if not isinstance(err, dict):
        raise HubGatewayError('hub_too_old', OLD_HUB, detail={'exit': p.returncode})
    code = str(err.get('code') or 'error'); msg = str(err.get('message') or 'the Hub gateway failed')[:300]
    detail = {k: err[k] for k in ('missing', 'register', 'terms', 'provider', 'limit') if k in err}
    if code == 'local_state': raise HubGatewayError('local_state', msg)   # the Hub could not read or save its limiter state: a stop, not a retry
    if p.returncode == 75 or code in ('local_limit', 'local_busy'):
        try: wait = float(err.get('retry_after_seconds'))
        except (TypeError, ValueError): wait = 60.0
        raise HubGatewayError(code if code in ('local_limit', 'local_busy') else 'local_limit', msg, retry_after_seconds=max(0.0, wait), detail=detail)
    if p.returncode == 2 or code == 'not_configured': raise HubGatewayError('not_configured', msg, detail=detail)
    if p.returncode == 64 or code == 'bad_request': raise HubGatewayError('bad_request', msg)
    raise HubGatewayError('gateway_error', msg)


def call(route, body, timeout=CALL_TIMEOUT_S, env=None):
    """One provider call through the Hub gateway. Returns the provider envelope ({text, status, contentType, ...}); a provider
    status such as 404 or 429 is in the envelope and is the caller's business. Raises HubGatewayError for everything else."""
    hub = _need(env)
    payload = json.dumps(body, separators=(',', ':'))
    p = _run(hub, ['gateway', route, '--json-in'], payload, timeout, env)
    doc = _parse(p.stdout)
    if p.returncode == 0:
        if doc is None or 'status' not in doc: raise HubGatewayError('gateway_error', 'the Hub gateway answered with something that is not a provider envelope')
        return doc
    _raise_for(p, doc)


def limits(timeout=60, env=None):
    """The Hub's effective limits (`gateway --limits`): makes no request and reads no key. Raises hub_too_old for an older Hub."""
    hub = _need(env)
    p = _run(hub, ['gateway', '--limits'], None, timeout, env)
    doc = _parse(p.stdout)
    if p.returncode != 0 or doc is None or 'hub_version' not in doc:
        if p.returncode != 0: _raise_for(p, doc)
        raise HubGatewayError('hub_too_old', OLD_HUB)
    ver = version_tuple(doc.get('hub_version'))
    if ver is not None and ver < MIN_HUB_VERSION:
        raise HubGatewayError('hub_too_old', f"Hub version {doc.get('hub_version')} is older than 0.2.0. Install Patent Research Hub 0.2.0 or later.")
    doc['_source'] = hub['source']
    return doc


def version_tuple(v):
    m = re.match(r'(\d+)\.(\d+)\.(\d+)', str(v or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def uspto_limits(lim):
    """(requests per hour, per-request interval seconds) that bound a USPTO run, from a `gateway --limits` document."""
    prov = (lim.get('providers') or {}).get('uspto') or {}
    cap = int(prov.get('max_calls_per_hour', prov.get('max_requests_per_hour')) or 0)   # Hub 0.2.0 final: max_calls_per_hour
    interval = max(float(prov.get('min_interval_ms') or 0), 1.0)
    by_interval = 3600000.0 / interval
    return (min(cap, by_interval) if cap > 0 else by_interval), interval / 1000.0
