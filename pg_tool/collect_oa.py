"""Collect office-action rejection rows for the applications in YOUR frames, through the Patent Research Hub's gateway command.

  python pg_cli.py collect plan      what a run would do: applications, requests, time, disk (makes no request)
  python pg_cli.py collect run --yes   collect (resumable); stops on a cap, a limit, an error or an outage and can be started again
  python pg_cli.py collect status    progress per frame and the run history
  python pg_cli.py collect probe     two small requests: the field list and, optionally, one application of yours

Rules this module keeps:
  * Only application numbers from the frames in your data folder are ever sent (eligible rows, de-duplicated, capped by the
    scope's limits.max_applications). Nothing read from a document you gave an agent can start a collection.
  * Requests go to the Hub's `oa-rejections` route as short-lived `Hub.cmd gateway` children (pg_tool/hubgw.py). No key is read here.
  * Exit 75 from the Hub (a local limit) means: wait `retry_after_seconds`, then go on. A provider 429 means: wait at least
    5 seconds, then back off. Any other 4xx stops the run. Three failures in a row (an outage) stop the run.
  * Progress is saved after every batch, in files that are replaced whole, so a run can be stopped at any moment and resumed.
  * Rows are kept exactly as the provider sent them, in corpus/oa-rejections/<frame>.jsonl.gz; the file appears only when a
    frame is complete. Rows that are not rejections (actionTypeCategory other than 'rejected') are kept and labelled by the loader.
  * collection_runs records hold counts and ids only: no application number, no text."""
import collections, datetime, gzip, hashlib, json, os, threading, time, uuid
from pathlib import Path
from . import fsutil, hubgw, oa_adapter, paths

OUT_DIR = ('corpus', 'oa-rejections')
WORK_DIR = ('collect', 'oa-rejections')
RUNS_FILE = ('collect', 'runs.jsonl')
LOCK_FILE = ('locks', 'collect-oa.lock')
BATCH_SIZE = 50
MIN_WAIT_AFTER_429 = 5.0
MAX_429_IN_A_ROW = 5
MAX_FAILURES_IN_A_ROW = 3
MAX_PAGES_PER_BATCH = 5000
MIN_LOCAL_WAIT_S = 1.0             # a Hub that says "retry in 0 s" is not obeyed literally: at least this long
MAX_LOCAL_WAITS_IN_A_ROW = 100     # then the run stops instead of asking the Hub again and again
STALE_LOCK_S = max(2 * hubgw.CALL_TIMEOUT_S, 1800)   # longer than one gateway call can take
CRASHED_LOCK_S = 60                # a lock whose writer is gone and that nobody refreshed for this long is a crash
HEARTBEAT_S = 15
DEFAULT_MAX_WAIT_S = 3600.0
# Assumptions behind the estimates (not measured at scale: a two-application probe saw 43 and 64 paragraph rows).
ASSUMED_ROWS_PER_APPLICATION = 30
ASSUMED_BYTES_PER_ROW = 700
ASSUMED_GZIP_RATIO = 8

EXIT = {'complete': 0, 'incomplete': 3, 'failed': 1, 'not_configured': 2, 'hub': 78, 'usage': 64}


class Stop(Exception):
    """The run cannot go on. `reason` is a short code; `resumable` says whether starting again can finish the job."""
    def __init__(self, reason, message, resumable=True, kind='incomplete'):
        super().__init__(message); self.reason, self.resumable, self.kind = reason, resumable, kind


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------ plan
def _frame_files(data):
    import glob
    return [p for p in sorted(glob.glob(str(Path(data) / 'corpus' / 'frames' / '*.json.gz')))
            if paths.FRAME_ID_RE.match(Path(p).name.split('.')[0]) and not paths._reserved(Path(p).name)]


def build_plan(data_dir, scope, only_frames=None, batch_size=BATCH_SIZE):
    """The applications to collect, per frame, from the eligible rows of your own frames. A number that is not eight digits is
    listed as skipped; applications beyond limits.max_applications are listed as skipped (not collected) and never sent."""
    data = paths.resolve_data_dir(data_dir)
    import sys
    if str(paths.INSTALL_DIR) not in sys.path: sys.path.insert(0, str(paths.INSTALL_DIR))
    from frame_contracts import load_frame
    cap = (scope.get('limits') or {}).get('max_applications')
    seen, frames, total = set(), [], 0
    for p in _frame_files(data):
        fid = Path(p).name.split('.')[0]
        if only_frames and fid not in only_frames: continue
        apps, skipped = [], []
        for r in load_frame(p).get('rows') or []:
            if r.get('eligible') is not True and str(r.get('eligible')) != 'True': continue
            a = str(r.get('application_number') or '')
            if a in seen: continue
            if not (len(a) == 8 and a.isdigit()):
                skipped.append({'application': a or None, 'reason': 'application number is not eight digits'}); continue
            seen.add(a); apps.append(a)
        apps.sort()
        if cap is not None and total + len(apps) > cap:
            keep = max(0, cap - total)
            skipped += [{'application': a, 'reason': 'beyond limits.max_applications in scope.json (not collected)'} for a in apps[keep:]]
            apps = apps[:keep]
        total += len(apps)
        batches = [apps[i:i + batch_size] for i in range(0, len(apps), batch_size)]
        sha = hashlib.sha256(json.dumps({'frame': fid, 'apps': apps, 'batch': batch_size}).encode()).hexdigest()
        frames.append({'frame_id': fid, 'applications': apps, 'batches': batches, 'skipped': skipped, 'plan_sha256': sha})
    if only_frames:
        missing = sorted(set(only_frames) - {f['frame_id'] for f in frames})
        if missing: raise ValueError('no such frame in your data folder: ' + ', '.join(missing))
    return {'frames': frames, 'applications': total, 'batch_size': batch_size, 'cap': cap}


def estimate(plan, limits_doc=None, adapter=None, max_requests=None):
    """Requests, time and disk for a plan. The time uses the Hub's own limits (gateway --limits) when given. Assumptions are named."""
    adapter = adapter or oa_adapter.get()
    pages = 0
    for f in plan['frames']:
        for b in f['batches']: pages += max(1, -(-len(b) * ASSUMED_ROWS_PER_APPLICATION // adapter.page_rows))
    rows = plan['applications'] * ASSUMED_ROWS_PER_APPLICATION
    est = {'applications': plan['applications'], 'frames': len(plan['frames']), 'batches': sum(len(f['batches']) for f in plan['frames']),
           'requests_estimated': pages, 'rows_estimated': rows,
           'disk_bytes_compressed_estimated': rows * ASSUMED_BYTES_PER_ROW // ASSUMED_GZIP_RATIO,
           'disk_bytes_working_estimated': rows * ASSUMED_BYTES_PER_ROW,
           'assumptions': [f'about {ASSUMED_ROWS_PER_APPLICATION} paragraph rows per application ({adapter.page_rows} rows per request), unmeasured at scale',
                           f'about {ASSUMED_BYTES_PER_ROW} bytes per row before compression and a gzip ratio of about {ASSUMED_GZIP_RATIO}',
                           'the real count of requests is known only as the run goes: it depends on how many rows each application has'],
           'requests_cap_this_run': max_requests}
    if limits_doc:
        per_hour, interval_s = hubgw.uspto_limits(limits_doc)
        n = min(pages, max_requests) if max_requests else pages
        est.update(hub_version=limits_doc.get('hub_version'), requests_per_hour_limit=round(per_hour, 1), seconds_between_requests=round(interval_s, 3),
                   hours_estimated=round(n / per_hour, 2) if per_hour else None,
                   time_note="Time divides the requests by the lower of the Hub's hourly cap and its request spacing (gateway --limits). The cap is shared with every other Hub use of the same key, and the USPTO allows one call at a time per key.")
    else:
        est.update(hub_version=None, hours_estimated=None, time_note='Time is not estimated: the Hub gateway was not reachable (run: python pg_cli.py doctor).')
    return est


# ------------------------------------------------------------------ lock and run records
class RunLock:
    """One collector at a time per data folder (the USPTO allows one call at a time per key). The lock file records the process id and a
    random token. It is refreshed by a heartbeat thread while the run lives (a gateway call can take many minutes). It is taken over when the
    process that wrote it is gone and nobody refreshed it for CRASHED_LOCK_S seconds (a crash), or when nobody refreshed it for `stale_s` seconds and
    the recorded process is not running. The takeover renames the old lock to a name only this run uses and checks what it renamed before
    deleting it, so two runs cannot both take over. A run whose lock was replaced or removed sets `lost` and stops at its next request.
    release() removes only a lock this run wrote."""
    def __init__(self, path, stale_s=STALE_LOCK_S, heartbeat_s=HEARTBEAT_S, crashed_s=CRASHED_LOCK_S):
        self.path, self.stale_s, self.heartbeat_s, self.crashed_s, self.held, self.token = Path(path), stale_s, heartbeat_s, crashed_s, False, uuid.uuid4().hex
        self._stop, self._thread, self.lost = threading.Event(), None, False

    @staticmethod
    def _read_file(path):
        """(info dict, exists). A lock that cannot be read counts as existing with no information."""
        try: return json.loads(Path(path).read_text(encoding='utf-8')), True
        except FileNotFoundError: return {}, False
        except (OSError, ValueError): return {}, True

    def _read(self): return self._read_file(self.path)[0]

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as f: f.write(json.dumps({'pid': os.getpid(), 'token': self.token, 'since': now_iso()}))
                self.held = True; self.lost = False; self._start_heartbeat(); return
            except FileExistsError:
                try: age = time.time() - self.path.stat().st_mtime
                except OSError: continue
                info = self._read(); pid = info.get('pid') if isinstance(info.get('pid'), int) else None
                where = f'(lock file {self.path})'
                if pid is not None and not fsutil.pid_alive(pid) and age >= self.crashed_s: pass            # the writer is gone: a crash
                elif age < self.stale_s:
                    raise Stop('locked', f'another collection is running in this data folder, refreshed {int(age)} s ago {where}. If it crashed, start again in about {int(self.crashed_s)} s, or delete that file.', resumable=True, kind='failed')
                elif pid is not None and fsutil.pid_alive(pid):
                    raise Stop('locked', f'the lock has not been refreshed for {int(age)} s, but process {pid} that wrote it is still running {where}. If you are sure it is not a collection, delete that file and start again.', resumable=True, kind='failed')
                self._take_over(info, where)                                                                    # then try to create the lock again
        raise Stop('locked', f'could not take the collection lock (lock file {self.path})', kind='failed')

    def _take_over(self, judged, where):
        """Remove a lock judged dead. Only one run at a time may do this (an exclusive guard file); under the guard the lock is looked at again and
        renamed to a name only this run uses, and what was renamed is compared with what was judged: a lock that was replaced in the meantime is
        put back, never deleted."""
        guard = self.path.with_name(self.path.name + '.takeover')
        try: os.close(os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            try:
                if time.time() - guard.stat().st_mtime > 120: guard.unlink()                                    # a guard left by a run that died mid-takeover
            except OSError: pass
            raise Stop('locked', f'another collection is taking over the lock at this moment {where}; start again in a minute', resumable=True, kind='failed')
        except OSError as e: raise Stop('locked', f'could not take the collection lock ({type(e).__name__}) {where}', kind='failed')
        try:
            now = self._read_file(self.path)[0]
            if not self.path.exists(): return                                                                   # already gone: the caller creates its own
            if now.get('token') != judged.get('token') or now.get('pid') != judged.get('pid'):
                raise Stop('locked', f'the lock was replaced while it was being checked {where}', resumable=True, kind='failed')
            aside = self.path.with_name(self.path.name + f'.taken-{self.token[:12]}')
            try: os.replace(self.path, aside)
            except FileNotFoundError: return
            except OSError as e: raise Stop('locked', f'could not take over the lock ({type(e).__name__}) {where}', kind='failed')
            got = self._read_file(aside)[0]
            if got.get('token') != judged.get('token') or got.get('pid') != judged.get('pid'):                  # not the lock that was judged: put it back
                try: os.link(aside, self.path)
                except OSError: pass
                try: os.unlink(aside)
                except OSError: pass
                raise Stop('locked', f'another collection took the lock at the same moment {where}', resumable=True, kind='failed')
            try: os.unlink(aside)
            except OSError: pass
        finally:
            try: os.unlink(guard)
            except OSError: pass

    def _start_heartbeat(self):
        def beat():
            while not self._stop.wait(self.heartbeat_s): self.touch()
        self._thread = threading.Thread(target=beat, name='collect-lock-heartbeat', daemon=True); self._thread.start()

    def touch(self):
        """Refresh the lock; if it is gone or belongs to another run, set `lost`."""
        if not self.held: return
        info, exists = self._read_file(self.path)
        if not exists or (info and info.get('token') != self.token): self.lost = True; return
        if info.get('token') == self.token:
            try: os.utime(self.path)
            except OSError: pass

    def check(self):
        """touch(), and Stop when the lock was lost: this run must not go on collecting beside another."""
        self.touch()
        if self.lost: raise Stop('lock_lost', f'the collection lock was removed or taken over by another run (lock file {self.path}); this run stops so two collections never run together', resumable=True, kind='failed')

    def release(self):
        self._stop.set()
        if self.held:
            if self._read().get('token') == self.token:
                try: self.path.unlink()
                except OSError: pass
            self.held = False


def record_run(data, rec):
    p = Path(data).joinpath(*RUNS_FILE); p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(rec, separators=(',', ':')) + '\n'); f.flush(); os.fsync(f.fileno())


def read_runs(data):
    p = Path(data).joinpath(*RUNS_FILE)
    out = []
    if p.is_file():
        for line in p.read_text(encoding='utf-8').splitlines():
            try: out.append(json.loads(line))
            except ValueError: pass   # a half-written last line after a crash is skipped
    return out


# ------------------------------------------------------------------ the request loop
class Collector:
    def __init__(self, data, adapter, gateway=hubgw, sleep=time.sleep, say=print, max_requests=None, max_wait_s=DEFAULT_MAX_WAIT_S, lock=None):
        self.data, self.adapter, self.gw, self._sleep, self.say = Path(data), adapter, gateway, sleep, say
        self.max_requests, self.max_wait_s, self.lock = max_requests, max_wait_s, lock
        self.requests = 0

    def wait(self, seconds, why):
        seconds = max(0.0, float(seconds))
        self.say(f'waiting {seconds:.0f} s ({why})')
        left = seconds
        while left > 0:
            step = min(left, 30.0); self._sleep(step); left -= step
            if self.lock: self.lock.check()

    def request(self, body):
        """One answered 200 envelope, after every wait and retry the rules call for; Stop for anything else."""
        failures = rate429 = local_waits = 0
        while True:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise Stop('request_cap', f'the request cap for this run ({self.max_requests}) was reached')
            if self.lock: self.lock.check()
            try:
                env = self.gw.call(self.adapter.route, body)
                self.requests += 1
            except hubgw.HubGatewayError as e:
                if e.code in ('local_limit', 'local_busy'):         # nothing was sent to the provider: wait, then continue
                    wait = max(MIN_LOCAL_WAIT_S, e.retry_after_seconds if e.retry_after_seconds is not None else 60.0)
                    local_waits += 1
                    if local_waits > MAX_LOCAL_WAITS_IN_A_ROW: raise Stop('limit_wait_loop', f'the Hub asked for {local_waits - 1} waits in a row without letting a call through; stopping. Progress is saved. Start again later.')
                    if wait > self.max_wait_s: raise Stop('limit_wait_too_long', f'the Hub asks for a wait of {wait:.0f} s (more than the {self.max_wait_s:.0f} s this run allows); start the run again later')
                    self.wait(wait, 'the Hub limit: ' + e.code); continue
                if e.code == 'not_configured': raise Stop('not_configured', str(e) + ' Nothing was sent.', resumable=True, kind='not_configured')
                if e.code in ('hub_too_old', 'hub_unavailable', 'test_stub_refused'): raise Stop(e.code, str(e), resumable=True, kind='hub')
                if e.code == 'local_state': raise Stop('local_state', str(e) + ' Check the Hub limiter folder; nothing is retried.', resumable=True, kind='failed')
                if e.code == 'bad_request': raise Stop('bad_request', str(e), resumable=False, kind='failed')
                self.requests += 1
                failures += 1
                if failures >= MAX_FAILURES_IN_A_ROW: raise Stop('outage', f'{failures} failures in a row ({e.code}); stopping. Progress is saved.')
                self.wait(2 ** failures * 2, f'retry after a gateway failure ({e.code})'); continue
            st = env.get('status')
            if st == 200: return env
            if st == 429:
                rate429 += 1
                if rate429 > MAX_429_IN_A_ROW: raise Stop('rate_limited', 'the provider answered 429 repeatedly; stopping. Progress is saved. Start again later.')
                try: ra = float(env.get('retryAfterSeconds') or 0)
                except (TypeError, ValueError): ra = 0.0
                self.wait(max(MIN_WAIT_AFTER_429, ra, min(MIN_WAIT_AFTER_429 * 2 ** (rate429 - 1), 300.0)), 'the provider said 429; never retried sooner than 5 s'); continue
            if isinstance(st, int) and 400 <= st < 500:
                raise Stop('client_error', f'the provider answered {st}; a request error is not retried. Stopping.', resumable=False, kind='failed')
            failures += 1
            if failures >= MAX_FAILURES_IN_A_ROW: raise Stop('outage', f'{failures} failures in a row (status {st}); stopping. Progress is saved.')
            self.wait(2 ** failures * 2, f'retry after status {st}')

    def collect_batch(self, apps):
        """(rows, numFound) for one batch of applications: every page, or Stop. A batch that did not arrive in full is never returned: when the paging
        ends with fewer rows than the service said exist (or hits the page cap) the run stops with bad_response."""
        rows, start, pages, found, first_found = [], 0, 0, 0, 0
        while True:
            env = self.request(self.adapter.request(apps, start, self.adapter.page_rows))
            try: found, docs = self.adapter.parse_page(env)
            except oa_adapter.AdapterError as e: raise Stop('bad_response', f'unexpected response ({e}); stopping', resumable=False, kind='failed')
            if pages and found != first_found:
                raise Stop('bad_response', f'the service reported {first_found:,} rows on the first page of this batch and {found:,} on page {pages + 1}; the count must not change within one batch, so nothing from it is saved. Start again later.', resumable=True, kind='failed')
            if not pages: first_found = found
            rows += docs; start += len(docs); pages += 1
            if not docs or start >= found: break
            if pages >= MAX_PAGES_PER_BATCH or start > 1000000: break
        if start < found:
            capped = pages >= MAX_PAGES_PER_BATCH or start > 1000000
            raise Stop('bad_response', f'the service reported {found:,} rows for this batch but only {start:,} arrived ({pages} page(s)); stopping so a short batch is never saved as complete. ' + ('The page limit was reached: starting again would stop at the same place, so use a smaller batch size.' if capped else 'Progress is kept; start again later.'), resumable=not capped, kind='failed')
        return rows, found


# ------------------------------------------------------------------ per frame
def _paths(data, fid):
    out = Path(data).joinpath(*OUT_DIR); work = Path(data).joinpath(*WORK_DIR)
    return {'out': out / f'{fid}.jsonl.gz', 'skipped': out / f'{fid}.skipped.json', 'collected': out / f'{fid}.collected.json',
            'part': work / f'{fid}.part.jsonl', 'progress': work / f'{fid}.progress.json'}


def read_progress(p):
    try: return json.loads(Path(p).read_text(encoding='utf-8'))
    except (OSError, ValueError): return None


def _finalise(data, fid, f, prog, adapter, run_id):
    P = _paths(data, fid)
    kinds, dates, with_rows = collections.Counter(), [], set()
    n = 0
    tmp = P['out'].with_name(P['out'].name + f'.tmp{os.getpid()}')
    P['out'].parent.mkdir(parents=True, exist_ok=True)
    with open(P['part'], 'rb') as src, open(tmp, 'wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0) as gz:
        for line in src:
            if not line.strip(): continue
            gz.write(line if line.endswith(b'\n') else line + b'\n'); n += 1
            c = adapter.normalise(json.loads(line))
            kinds[c['row_kind']] += 1
            if c['application']: with_rows.add(c['application'])
            if c['date']: dates.append(c['date'])
        raw.flush(); os.fsync(raw.fileno())
    fsutil.replace_with_retry(tmp, P['out'])
    without = sorted(set(f['applications']) - with_rows)
    fsutil.atomic_write_text(P['skipped'], json.dumps({'applications': f['skipped']}, indent=1) + '\n')
    fsutil.atomic_write_text(P['collected'], json.dumps({
        'frame_id': fid, 'adapter': adapter.id, 'collected_at': now_iso(), 'run_id': run_id, 'plan_sha256': f['plan_sha256'],
        'applications_requested': len(f['applications']), 'applications_with_rows': len(with_rows), 'applications_without_rows': len(without),
        'applications_without_rows_list': without, 'rows': n, 'row_kinds': dict(sorted(kinds.items())), 'requests': prog.get('requests', 0),
        'earliest_submission_date': min(dates) if dates else None, 'latest_submission_date': max(dates) if dates else None,
        'note': 'Rows are exactly as the provider sent them. Only rows whose actionTypeCategory is rejected are rejections; the others are kept and labelled. '
                'An application with no rows may have no action in the source yet or before its coverage starts: that is not a finding.'}, indent=1) + '\n')
    prog.update(status='finished', finished_at=now_iso(), rows=n)
    fsutil.atomic_write_text(P['progress'], json.dumps(prog, indent=1) + '\n')
    try: P['part'].unlink()
    except OSError: pass
    return {'rows': n, 'applications_with_rows': len(with_rows), 'applications_without_rows': len(without)}


def collect_frame(col, data, f, run_id, restart=False):
    fid = f['frame_id']; P = _paths(data, fid); adapter = col.adapter
    P['part'].parent.mkdir(parents=True, exist_ok=True)
    prog = read_progress(P['progress'])
    if prog and prog.get('status') == 'finished' and P['out'].is_file() and not restart:
        if prog.get('plan_sha256') == f['plan_sha256']:
            col.say(f'{fid}: already collected ({prog.get("rows", 0):,} rows); use --restart to collect it again')
            return {'status': 'done', 'skipped_existing': True}
        raise Stop('plan_changed', f'{fid}: the frame or the scope changed since it was collected. Use --restart to collect it again.', resumable=False, kind='failed')
    fresh = restart or prog is None or prog.get('status') == 'finished'
    if not fresh and prog.get('plan_sha256') != f['plan_sha256']:
        raise Stop('plan_changed', f'{fid}: the frame or the scope changed since the saved progress. Use --restart to begin this frame again.', resumable=False, kind='failed')
    if fresh:
        for k in ('part', 'progress'):
            try: P[k].unlink()
            except OSError: pass
        prog = {'frame_id': fid, 'plan_sha256': f['plan_sha256'], 'adapter': adapter.id, 'batches_total': len(f['batches']), 'next_batch': 0,
                'part_bytes': 0, 'rows': 0, 'requests': 0, 'status': 'running', 'started_at': now_iso()}
        P['part'].write_bytes(b'')
    else:
        size = P['part'].stat().st_size if P['part'].exists() else 0
        if size < prog['part_bytes']: raise Stop('part_missing', f'{fid}: the saved rows file is shorter than its progress record; use --restart', resumable=False, kind='failed')
        with open(P['part'], 'r+b') as fh: fh.truncate(prog['part_bytes'])   # drop whatever a crash left after the last saved batch
        col.say(f'{fid}: resuming at batch {prog["next_batch"] + 1} of {prog["batches_total"]}')
    base_requests, requests_before = col.requests, prog.get('requests', 0)
    try:
        for bi in range(prog['next_batch'], len(f['batches'])):
            rows, found = col.collect_batch(f['batches'][bi])
            with open(P['part'], 'ab') as fh:
                for r in rows: fh.write(json.dumps(r, separators=(',', ':'), ensure_ascii=False).encode('utf-8') + b'\n')
                fh.flush(); os.fsync(fh.fileno()); size = fh.tell()
            prog.setdefault('batches_found', {})[str(bi)] = found   # numFound as the service reported it for this batch
            prog.update(next_batch=bi + 1, part_bytes=size, rows=prog['rows'] + len(rows), requests=requests_before + (col.requests - base_requests), status='running', updated_at=now_iso())
            fsutil.atomic_write_text(P['progress'], json.dumps(prog, indent=1) + '\n')
            col.say(f'{fid}: batch {bi + 1}/{len(f["batches"])} done, {prog["rows"]:,} rows, {col.requests} requests this run')
    except Stop as s:
        prog.update(status='stopped', stop_reason=s.reason, updated_at=now_iso(), requests=requests_before + (col.requests - base_requests))
        fsutil.atomic_write_text(P['progress'], json.dumps(prog, indent=1) + '\n')
        raise
    res = _finalise(data, fid, f, prog, adapter, run_id)
    col.say(f'{fid}: complete. {res["rows"]:,} rows; {res["applications_with_rows"]:,} applications with rows, {res["applications_without_rows"]:,} without')
    return {'status': 'done', **res}


# ------------------------------------------------------------------ run
def run(data_dir, scope, *, scope_sha=None, frames=None, max_requests=None, restart=False, max_wait_s=DEFAULT_MAX_WAIT_S, say=print,
        gateway=hubgw, sleep=time.sleep, adapter_id=None, batch_size=BATCH_SIZE):
    """Collect. Returns {'status', 'exit', 'reason', 'message', 'requests', ...}; never raises for an expected stop."""
    data = paths.data_dir_for_write(data_dir)
    adapter = oa_adapter.get(adapter_id)
    cap_scope = (scope.get('limits') or {}).get('max_requests_per_run')
    cap = min([c for c in (max_requests, cap_scope) if c is not None], default=None)
    plan = build_plan(data, scope, set(frames) if frames else None, batch_size)
    run_id = uuid.uuid4().hex[:12]; started = now_iso()
    lock = RunLock(data.joinpath(*LOCK_FILE)); col = Collector(data, adapter, gateway, sleep, say, cap, max_wait_s, lock)
    result = {'run_id': run_id, 'status': 'complete', 'reason': None, 'message': None, 'exit': EXIT['complete'], 'frames': {}}
    try:
        lock.acquire()
        try: lim = gateway.limits()
        except hubgw.HubGatewayError as e:
            raise Stop(e.code, str(e), kind='hub' if e.code in ('hub_too_old', 'hub_unavailable', 'test_stub_refused') else 'failed')
        say(f"Hub {lim.get('hub_version')} (limits read; no key used). {plan['applications']:,} applications in {len(plan['frames'])} frame(s).")
        for f in plan['frames']:
            if not f['applications'] and not f['skipped']: continue
            try: result['frames'][f['frame_id']] = collect_frame(col, data, f, run_id, restart)
            except Stop as s:
                result['frames'][f['frame_id']] = {'status': 'stopped', 'reason': s.reason}; raise
    except Stop as s:
        result.update(status='stopped', reason=s.reason, message=str(s), exit=EXIT.get(s.kind, EXIT['failed']), resumable=s.resumable)
    finally:
        lock.release()
    result['requests'] = col.requests
    record_run(data, {'run_id': run_id, 'kind': 'oa-rejections', 'started_at': started, 'finished_at': now_iso(), 'scope_id': scope_sha,
                      'requests': col.requests, 'applications': plan['applications'], 'status': result['status'] if result['status'] == 'complete' else 'stopped: ' + str(result['reason']),
                      'note': f"adapter {adapter.id}; frames {len(plan['frames'])}; request cap {cap if cap is not None else 'none'}"})
    return result


# ------------------------------------------------------------------ status and probe
def status(data_dir):
    data = paths.resolve_data_dir(data_dir)
    work = data.joinpath(*WORK_DIR); out = data.joinpath(*OUT_DIR)
    frames = []
    for pf in sorted(work.glob('*.progress.json')) if work.is_dir() else []:
        pr = read_progress(pf)
        if not pr: continue
        frames.append({'frame_id': pr.get('frame_id'), 'status': pr.get('status'), 'batches_done': pr.get('next_batch'), 'batches_total': pr.get('batches_total'),
                       'rows': pr.get('rows'), 'requests': pr.get('requests'), 'stop_reason': pr.get('stop_reason'), 'updated_at': pr.get('updated_at') or pr.get('finished_at'),
                       'output': (out / f"{pr.get('frame_id')}.jsonl.gz").is_file()})
    lockp = data.joinpath(*LOCK_FILE)
    return {'data_dir': str(data), 'frames': frames, 'runs': read_runs(data)[-10:], 'lock_present': lockp.is_file()}


def probe(data_dir, application=None, gateway=hubgw, say=print):
    """Two small requests at most: the provider's field list, and (only for an application that is in your frames) its first rows."""
    data = paths.resolve_data_dir(data_dir); adapter = oa_adapter.get()
    out = {'requests': 0, 'adapter': adapter.id}
    lim = gateway.limits(); out['hub_version'] = lim.get('hub_version')
    env = gateway.call(adapter.route, adapter.fields_request()); out['requests'] += 1
    out['fields_status'] = env.get('status')
    if env.get('status') == 200:
        names = adapter.parse_fields(env); low = {n.lower() for n in names}
        out['fields_count'] = len(names); out['fields_missing_that_this_tool_reads'] = [n for n in adapter.expected_fields if n.lower() not in low]
    if application:
        plan = build_plan(data, {'limits': {}}); known = {a for f in plan['frames'] for a in f['applications']}
        if application not in known:
            out['application'] = 'not queried: that application is not in your frames (only applications from your own frames are ever sent)'
        else:
            env = gateway.call(adapter.route, adapter.request([application], 0, 20)); out['requests'] += 1
            out['application_status'] = env.get('status')
            if env.get('status') == 200:
                found, docs = adapter.parse_page(env); rows = [adapter.normalise(d) for d in docs]
                out['application_result'] = {'rows_in_service': found, 'rows_returned': len(docs), 'row_kinds': dict(collections.Counter(r['row_kind'] for r in rows)),
                                             'statutes_seen': sorted({r['statute'] for r in rows if r['statute']}), 'earliest': min((r['date'] for r in rows if r['date']), default=None),
                                             'latest': max((r['date'] for r in rows if r['date']), default=None)}
    record_run(paths.data_dir_for_write(data_dir), {'run_id': uuid.uuid4().hex[:12], 'kind': 'probe', 'started_at': now_iso(), 'finished_at': now_iso(), 'scope_id': None,
                                                    'requests': out['requests'], 'applications': 1 if application else 0, 'status': 'complete', 'note': f'adapter {adapter.id}'})
    return out
