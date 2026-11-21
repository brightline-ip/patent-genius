"""Patent Genius database (schema v2): one SQLite file, built deterministically from the data in YOUR data folder, plus named
reports. The database is a derived artifact: delete it and rebuild. Every table is filled from files listed in `sources`
(path, sha256, rows), so any number traces back to its inputs.

Every input is optional. A missing dataset is recorded in `meta.missing` and the coverage report says "not collected"; a
build on an empty data folder succeeds and produces an empty, valid database. The build writes a NEW file and swaps it into
place when it is complete (with retries, because Windows refuses to replace a file another program has open); readers open
the database read-only, one call at a time, so a build never disturbs a reader and a failed build leaves the old file alone.

Tables (keys in brackets):
  scope [scope_id]                      the scope.json used (its SHA-256 and the full text), if the data folder has one
  frames [frame_id]                     frozen frames: fields, filing-date window, date basis, scope id
  applications [application]            one row per application: dates, art unit, examiner, applicant, status, patent
  memberships [application, field]     which technology field an application belongs to (an application can be in two)
  application_cpc [application, cpc]    all CPC symbols of the application, with position (0 = first listed)
  events [application, seq]             every file-wrapper transaction (date, code)
  oa_paragraphs                         office-action rows as the provider sent them (one per paragraph), labelled: row_kind says whether a
                                        row is a rejection, an objection, an allowance, a cancellation or unlabelled; statute is statute level only
  office_actions [application, oa_document]  per office action: date, code, art unit, examiner (as on the application record NOW), finality,
                                        statute set (strict rule), Alice/Mayo
  oa_outcomes [application, oa_document]  what happened next, derived from events only (pg_tool/oa_outcomes.py): anchored on the action's mailing event (or, failing that, the action event)
                                        with the anchor quality and whether later events were recognised
  event_codes [code]                    every event code seen, how often, the provider's description, and its role in pg_tool/event_codes.json (if any)
  oa_parsed / oa_citations [application, document_id]  rule-parsed full-text office actions and the references found in them
  oa_links                              which full-text document goes with which office_actions row (application + code + date within 3 days)
  collection_runs                       one row per collection run: counts and ids only
  documents [application, document_id] normalized full-text documents (path to page-anchored Markdown)
  ptab_trials / ptab_decisions / ptab_appeals   AIA trials and ex parte appeal decisions, if you collected those files
  skipped_applications                  applications left out of every denominator, with the reason
  sources [path]                        input files with sha256 and row counts; meta: build time, version, steps, missing

Strict rejection rule: a paragraph is a rejection under a statute only when actionTypeCategory == 'rejected' and
legalSectionCode names that statute (statute level only: the provider has no 112 subsections). The provider's hasRej* flags are kept
in oa_paragraphs; only hasRejDP is used for a count, because double patenting has no section code (a rejected row with hasRejDP = 1).
Descriptive data for a registered practitioner; not legal advice. The field names of the rejection rows were seen in a small read-only
probe of the service on 2026-11-20 (docs/COLLECTING.md); the Hub's direct transport has not been run live.

Usage:
  python pg_db.py build [--data-dir D] [--db FILE] [--steps frames,events,oa,documents,ptab,features]
  python pg_db.py reports                       list named reports
  python pg_db.py report <name> [--format md|csv] [--field agri] [--min-n 30] [--years 2012-2013]
  python pg_db.py report coverage               what the database holds and what was not collected
  python pg_db.py compare <feature> | compare-all [--years all|YYYY|YYYY-YYYY]
  python pg_db.py sql "<SELECT ...>"            read-only ad hoc query (SELECT/WITH only)"""
import argparse, ast, collections, csv, datetime, glob, gzip, hashlib, io, json, os, re, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pg_tool import oa_adapter, oa_outcomes, oa_parse, paths, scope as scope_mod, untrusted  # noqa: E402

VERSION = '2.3'
STEPS = ('frames', 'events', 'oa', 'outcomes', 'documents', 'parse', 'ptab', 'features')
STATUTES = [('101', r'\b101\b'), ('102', r'\b102\b'), ('103', r'\b103\b'), ('112', r'\b112\b'), ('dp', r'double patenting')]


def default_db(data_dir=None):
    return paths.resolve_data_dir(data_dir) / 'corpus' / 'pg.db'


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE sources (path TEXT PRIMARY KEY, sha256 TEXT, bytes INTEGER, rows INTEGER, loaded_into TEXT);
CREATE TABLE scope (scope_id TEXT PRIMARY KEY, snapshot TEXT, loaded_at TEXT);
CREATE TABLE frames (frame_id TEXT PRIMARY KEY, fields TEXT, window_from TEXT, window_to TEXT, date_basis TEXT, frozen_at TEXT,
  cpc_sha256 TEXT, eligible INTEGER, derived_from TEXT, scope_id TEXT);
CREATE TABLE applications (application TEXT PRIMARY KEY, filing_date TEXT, filing_year INTEGER, application_type TEXT,
  art_unit TEXT, examiner TEXT, applicant TEXT, status_text TEXT, status_date TEXT, outcome TEXT, publication_number TEXT,
  patent_number TEXT, grant_date TEXT, family_key TEXT, first_cpc TEXT, fields TEXT);
CREATE TABLE memberships (application TEXT, frame_id TEXT, field TEXT, PRIMARY KEY (application, field));
CREATE TABLE application_cpc (application TEXT, cpc TEXT, position INTEGER, PRIMARY KEY (application, cpc));
CREATE TABLE events (application TEXT, seq INTEGER, date TEXT, code TEXT, PRIMARY KEY (application, seq));
CREATE TABLE oa_paragraphs (application TEXT, oa_document TEXT, doc_code TEXT, date TEXT, legal_section TEXT,
  action_type TEXT, statute TEXT, claims TEXT, art_unit TEXT, paragraph_number INTEGER, alice INTEGER, mayo INTEGER, bilski INTEGER,
  myriad INTEGER, allowed_claim INTEGER, cite102_gt1 INTEGER, cite103_gt3 INTEGER, cite103_eq1 INTEGER, cite103_max INTEGER, frame_id TEXT,
  row_kind TEXT, has_rej101 INTEGER, has_rej102 INTEGER, has_rej103 INTEGER, has_rej112 INTEGER, has_rejdp INTEGER);
CREATE TABLE office_actions (application TEXT, oa_document TEXT, doc_code TEXT, date TEXT, art_unit TEXT, examiner TEXT, field TEXT,
  cpc_group TEXT, cpc_subclass TEXT, is_final INTEGER, statute_set TEXT, rej101 INTEGER, rej102 INTEGER, rej103 INTEGER, rej112 INTEGER,
  rejdp INTEGER, alice INTEGER, mayo INTEGER, allowed_claims INTEGER, PRIMARY KEY (application, oa_document));
CREATE TABLE oa_citations (application TEXT, oa_document TEXT, reference TEXT, reference_normalized TEXT, kind TEXT, source TEXT);
CREATE TABLE oa_links (application TEXT, oa_document TEXT, document_id TEXT, day_diff INTEGER, basis TEXT, PRIMARY KEY (application, oa_document));
CREATE TABLE oa_parsed (application TEXT, oa_document TEXT, parser_version TEXT, art_unit TEXT, art_unit_how TEXT, examiner TEXT,
  examiner_how TEXT, mail_date TEXT, mail_date_how TEXT, application_number TEXT, application_number_how TEXT, statute_set TEXT,
  references_json TEXT, parsed_at TEXT, PRIMARY KEY (application, oa_document));
CREATE TABLE oa_outcomes (application TEXT, oa_document TEXT, next_event TEXT, next_event_date TEXT, interview_before_next INTEGER,
  amendment_before_next INTEGER, rce_or_appeal_next INTEGER, application_outcome TEXT, anchor_quality TEXT, anchor_code TEXT, anchor_date TEXT,
  next_event_state TEXT, later_events INTEGER, abandonment_rescinded INTEGER, PRIMARY KEY (application, oa_document));
CREATE TABLE event_codes (code TEXT PRIMARY KEY, events INTEGER, description TEXT, role TEXT, confirmed INTEGER, in_official_list INTEGER);
CREATE TABLE collection_runs (run_id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, scope_id TEXT, requests INTEGER,
  applications INTEGER, status TEXT, note TEXT);
CREATE TABLE documents (application TEXT, document_id TEXT, code TEXT, component TEXT, official_date TEXT, pages INTEGER,
  text_fidelity TEXT, markdown_path TEXT, markdown_sha256 TEXT, PRIMARY KEY (application, document_id));
CREATE TABLE ptab_trials (trial_number TEXT PRIMARY KEY, trial_type TEXT, petition_date TEXT, status TEXT, latest_decision_date TEXT,
  application TEXT, patent_number TEXT, art_unit TEXT, owner TEXT, petitioner TEXT, outcome TEXT, in_frames INTEGER);
CREATE TABLE ptab_decisions (document_id TEXT PRIMARY KEY, trial_number TEXT, category TEXT, decision_type TEXT, outcome TEXT,
  decision_date TEXT, statutes TEXT, document_name TEXT);
CREATE TABLE ptab_appeals (appeal_number TEXT, document_id TEXT, application TEXT, patent_number TEXT, art_unit TEXT,
  decision_date TEXT, outcome TEXT, statutes TEXT, issue_types TEXT, in_frames INTEGER, PRIMARY KEY (appeal_number, document_id));
CREATE TABLE skipped_applications (application TEXT, frame_id TEXT, step TEXT, reason TEXT);
"""
INDEXES = """
CREATE INDEX ev_code ON events(code, date);
CREATE INDEX oap_app ON oa_paragraphs(application, oa_document);
CREATE INDEX oa_date ON office_actions(date);
CREATE INDEX oa_examiner ON office_actions(examiner);
CREATE INDEX oa_au ON office_actions(art_unit);
CREATE INDEX mem_field ON memberships(field);
CREATE INDEX cpc_cpc ON application_cpc(cpc);
CREATE INDEX app_patent ON applications(patent_number);
CREATE INDEX app_year ON applications(filing_year);
CREATE INDEX cit_ref ON oa_citations(reference_normalized);
CREATE INDEX cit_doc ON oa_citations(application, oa_document);
CREATE INDEX lnk_doc ON oa_links(application, document_id);
CREATE INDEX pt_app ON ptab_trials(application);
CREATE INDEX pt_patent ON ptab_trials(patent_number);
CREATE INDEX pa_app ON ptab_appeals(application);
CREATE INDEX sk_app ON skipped_applications(application);
"""


# ------------------------------------------------------------------ small helpers
def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()


def lit(v, default=None):
    """Frame and provider files store some values as Python reprs ('None', "['a']", "{'k': 1}")."""
    if v is None or isinstance(v, (dict, list, int, float, bool)): return v
    s = str(v)
    if s in ('None', ''): return default
    if s[:1] in '[{(' or s in ('True', 'False'):
        try: return ast.literal_eval(s)
        except (ValueError, SyntaxError): return default
    return s


def jsonl(path):
    op = gzip.open if str(path).endswith('.gz') else open
    with op(path, 'rt', encoding='utf-8') as f:
        for line in f:
            if line.strip(): yield json.loads(line)


def outcome_of(status_text, patent):
    s = (status_text or '').lower()
    if patent: return 'granted'
    if 'abandon' in s: return 'abandoned'
    if 'allowance' in s or 'issue fee' in s or 'allowed' in s: return 'allowed_not_yet_granted'
    return 'pending' if s else 'unknown'


def first(r, *names):
    for n in names:
        if r.get(n) is not None: return r[n]
    return None


def as_int(v):
    try: return int(v) if v is not None and str(v) != '' else None
    except (TypeError, ValueError): return None


def atomic_write_text(path, text, attempts=8):
    """Write a whole file at once: a temp file in the same folder, then a swap. A reader sees the old or the new file, never half of one."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp{os.getpid()}')
    tmp.write_text(text, encoding='utf-8', newline='\n')
    swap(tmp, path, attempts)


def swap(tmp, final, attempts=8, pause=0.4):
    """Replace final with tmp, retrying: on Windows a file that another program (or an antivirus scan) has open cannot be replaced right now."""
    for i in range(attempts):
        try: os.replace(tmp, final); return
        except PermissionError:
            if i == attempts - 1: raise
            time.sleep(pause)


def valid_frame_stem(p):
    stem = Path(p).name.split('.')[0]
    return bool(paths.FRAME_ID_RE.match(stem)) and not paths._reserved(stem)


def parse_years(spec):
    """None (all years in the database) or (first, last). Accepts all, YYYY, YYYY-YYYY."""
    if spec in (None, '', 'all'): return None
    m = re.fullmatch(r'(\d{4})(?:-(\d{4}))?', str(spec).strip())
    if not m: sys.exit('--years must be all, YYYY or YYYY-YYYY')
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if a > b: sys.exit('--years: the first year is after the last')
    return a, b


def resolve_years(con, years):
    """(y0, y1) to use in SQL: the given years, or every filing year present in the database."""
    if years: return int(years[0]), int(years[1])
    lo, hi = con.execute('SELECT MIN(filing_year), MAX(filing_year) FROM applications').fetchone()
    return (lo if lo is not None else 0), (hi if hi is not None else 9999)


# ------------------------------------------------------------------ build
class Builder:
    def __init__(self, db, data):
        self.db, self.data = db, Path(data)
        self.con = sqlite3.connect(db)
        self.con.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;')
        self.con.executescript(SCHEMA)
        self.missing, self.status, self.scope_id, self.parse_failed = [], {}, None, 0
        self.skipped = []   # (application, frame_id, step, reason)

    def rel(self, p):
        p = Path(p).resolve()
        try: return p.relative_to(self.data.resolve()).as_posix()
        except ValueError: return p.name

    def source(self, path, rows, table):
        p = Path(path)
        self.con.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)', (self.rel(p), sha256(p), p.stat().st_size, rows, table))

    def many(self, sql, rows, size=50000):
        buf, n = [], 0
        for r in rows:
            buf.append(r); n += 1
            if len(buf) >= size: self.con.executemany(sql, buf); buf.clear()
        if buf: self.con.executemany(sql, buf)
        return n

    def absent(self, name, what):
        self.missing.append({'dataset': name, 'looked_for': what}); return None

    # ---- scope
    def load_scope(self):
        p = self.data / 'scope.json'
        if not p.is_file(): self.absent('scope', 'scope.json'); return
        raw = p.read_bytes()
        self.scope_id = hashlib.sha256(raw).hexdigest()
        self.con.execute('INSERT OR REPLACE INTO scope VALUES (?,?,?)', (self.scope_id, raw.decode('utf-8-sig', errors='replace'), datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')))

    def load_runs(self):
        """collection_runs from the collector's own record (collect/runs.jsonl): counts and ids only. A missing file is not a missing dataset."""
        p = self.data / 'collect' / 'runs.jsonl'
        if not p.is_file(): return
        rows = []
        for line in p.read_text(encoding='utf-8').splitlines():
            try: r = json.loads(line)
            except ValueError: continue
            rows.append((r.get('run_id'), r.get('kind'), r.get('started_at'), r.get('finished_at'), r.get('scope_id'), as_int(r.get('requests')), as_int(r.get('applications')), r.get('status'), r.get('note')))
        self.many('INSERT OR REPLACE INTO collection_runs VALUES (?,?,?,?,?,?,?,?,?)', rows)

    # ---- population
    def frames(self):
        from frame_contracts import load_frame
        files = [p for p in sorted(glob.glob(str(self.data / 'corpus' / 'frames' / '*.json.gz'))) if valid_frame_stem(p)]
        if not files: return self.absent('frames', 'corpus/frames/*.json.gz')
        apps, mem, derived = {}, [], []
        for p in files:
            fid = Path(p).name.split('.')[0]
            f = load_frame(p)
            windows = f.get('windows') or []
            fields_all = sorted((f.get('cpc') or {}).get('strata') or [])
            elig = 0
            parent = (f.get('derived_from') or {}).get('frame_id')
            for r in f.get('rows') or []:
                a = str(r.get('application_number') or '')
                if lit(r.get('eligible')) is not True: continue
                elig += 1
                fd = r.get('filing_date')
                if not re.fullmatch(r'\d{8}', a) or not (isinstance(fd, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', fd)):
                    self.skipped.append((a or None, fid, 'frames', 'application number or filing date missing or malformed')); continue
                fields = sorted(s for s in (r.get('strata') or []) if isinstance(s, str)) or fields_all
                for fld in fields: mem.append((a, fid, fld))
                if a in apps: continue
                cpc = lit(r.get('cpc'), []) or []
                pat = lit(r.get('patent_number'))
                apps[a] = [a, fd, int(fd[:4]), r.get('application_type'), lit(r.get('art_unit')), lit(r.get('examiner')), lit(r.get('applicant')),
                           lit(r.get('status_text')), lit(r.get('status_date')), outcome_of(lit(r.get('status_text')), pat), lit(r.get('publication_number')),
                           str(pat) if pat else None, lit(r.get('grant_date')), lit(r.get('family_key')), cpc[0] if cpc else None, None, cpc]
            cpc_hash = hashlib.sha256(json.dumps(f.get('cpc'), sort_keys=True).encode()).hexdigest()
            self.con.execute('INSERT INTO frames VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (fid, ','.join(fields_all), min((w['from'] for w in windows), default=None), max((w['to'] for w in windows), default=None),
                              ','.join(sorted({w.get('basis') for w in windows if w.get('basis')})) or None, f.get('frozen_at'), cpc_hash, elig, parent, self.scope_id))
            self.source(p, elig, 'frames, applications, memberships, application_cpc')
        fields = {}
        for a, fid, fld in mem: fields.setdefault(a, set()).add(fld)
        for row in apps.values(): row[15] = ','.join(sorted(fields.get(row[0], ())))
        self.many('INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (row[:16] for row in apps.values()))
        self.many('INSERT OR IGNORE INTO memberships VALUES (?,?,?)', mem)
        self.many('INSERT OR IGNORE INTO application_cpc VALUES (?,?,?)', ((a, c, i) for a, row in apps.items() for i, c in enumerate(row[16])))
        return len(apps)

    def events(self):
        files = [p for p in sorted(glob.glob(str(self.data / 'corpus' / 'frame-events' / '*.jsonl.gz'))) if valid_frame_stem(p)]
        if not files: return self.absent('events', 'corpus/frame-events/*.jsonl.gz')
        n = 0
        seen_codes = {}
        descriptions = {}
        for p in files:
            ec = Path(p).parent / (Path(p).name.split('.')[0] + '.event-codes.json')   # written by compact.py: code -> the provider's description
            if ec.is_file():
                try: descriptions.update({untrusted.clean_line(k, 40): untrusted.clean_line(v, 200) for k, v in json.loads(ec.read_text(encoding='utf-8')).items()})
                except ValueError: pass
        for p in files:
            examiners = []
            def gen():
                for r in jsonl(p):
                    a = r['application_number']
                    if r.get('examiner'): examiners.append((r['examiner'], a))
                    for i, (d, c) in enumerate(lit(r.get('events'), []) or []):
                        seen_codes[c] = seen_codes.get(c, 0) + 1
                        yield (a, i, d, c)
            k = self.many('INSERT OR IGNORE INTO events VALUES (?,?,?,?)', gen())
            self.con.executemany('UPDATE applications SET examiner=? WHERE application=? AND examiner IS NULL', examiners)
            self.source(p, k, 'events'); n += k
        official = oa_outcomes.official_codes()
        self.many('INSERT OR REPLACE INTO event_codes VALUES (?,?,?,?,?,?)', ((str(c), k, official.get(str(c)) or descriptions.get(str(c)), oa_outcomes.role_of(c), int(oa_outcomes.is_confirmed(c)), int(str(c) in official)) for c, k in seen_codes.items()))
        return n

    def oa(self):
        files = [p for p in sorted(glob.glob(str(self.data / 'corpus' / 'oa-rejections' / '*.jsonl.gz'))) if valid_frame_stem(p)]
        if not files: return self.absent('oa_rejections', 'corpus/oa-rejections/*.jsonl.gz')
        known = {r[0] for r in self.con.execute('SELECT application FROM applications')}
        n = 0
        for p in files:
            fid = Path(p).name.split('.')[0]
            adapter_id = None
            cm = Path(p).parent / (fid + '.collected.json')   # written by the collector: which adapter produced the rows
            if cm.is_file():
                try: adapter_id = json.loads(cm.read_text(encoding='utf-8')).get('adapter')
                except ValueError: adapter_id = None
            try: adapter = oa_adapter.get(adapter_id)
            except oa_adapter.AdapterError as e:
                self.skipped.append((None, fid, 'oa', f'rows not loaded: {str(e)[:120]}')); continue
            orphans = set()
            def gen():
                for r in jsonl(p):
                    c = adapter.normalise(r); app = c['application']
                    if app not in known:
                        if app: orphans.add(app)
                        continue  # a rejection row for an application that is in none of your frames is not part of any denominator
                    yield (app, c['oa_document'], c['doc_code'], c['date'], c['legal_section'], c['action_type'], c['statute'], c['claims'], c['art_unit'],
                           c['paragraph_number'], c['alice'], c['mayo'], c['bilski'], c['myriad'], c['allowed_claim'], c['cite102_gt1'], c['cite103_gt3'],
                           c['cite103_eq1'], c['cite103_max'], fid, c['row_kind'], c['has_rej101'], c['has_rej102'], c['has_rej103'], c['has_rej112'], c['has_rejdp'])
            k = self.many('INSERT INTO oa_paragraphs VALUES (' + ','.join('?' * 26) + ')', gen())
            self.source(p, k, 'oa_paragraphs, office_actions'); n += k
            for a in sorted(orphans): self.skipped.append((a, fid, 'oa', 'office-action rows exist but the application is in none of your frames'))
            sk = Path(p).parent / (fid + '.skipped.json')   # written by the collector: {"applications": [{"application": "...", "reason": "..."}]}
            if sk.is_file():
                try:
                    for e in json.loads(sk.read_text(encoding='utf-8')).get('applications', []):
                        self.skipped.append((str(e.get('application')) if e.get('application') else None, fid, 'oa', str(e.get('reason') or 'skipped during collection')[:200]))
                except (ValueError, AttributeError): self.skipped.append((None, fid, 'oa', f'{sk.name} is not readable'))
        # Strict rule: a statute counts for an office action only on rows labelled rejected whose legalSectionCode names it (statute level: the
        # provider has no 112 subsections). Double patenting has no section code, so it comes from the provider's hasRejDP flag on a rejected row.
        self.con.execute("""INSERT OR IGNORE INTO office_actions
          SELECT p.application, p.oa_document, MAX(p.doc_code), MIN(p.date), COALESCE(MAX(p.art_unit), a.art_unit), a.examiner,
            (SELECT MIN(field) FROM memberships m WHERE m.application = p.application),
            substr(a.first_cpc, 1, instr(a.first_cpc || '/', '/') - 1), substr(a.first_cpc, 1, 4),
            CASE MAX(p.doc_code) WHEN 'CTFR' THEN 1 WHEN 'CTNF' THEN 0 END,
            rtrim(CASE WHEN MAX(p.action_type='rejected' AND p.statute='101') THEN '101,' ELSE '' END ||
                  CASE WHEN MAX(p.action_type='rejected' AND p.statute='102') THEN '102,' ELSE '' END ||
                  CASE WHEN MAX(p.action_type='rejected' AND p.statute='103') THEN '103,' ELSE '' END ||
                  CASE WHEN MAX(p.action_type='rejected' AND p.statute='112') THEN '112,' ELSE '' END ||
                  CASE WHEN MAX(p.action_type='rejected' AND (p.statute='dp' OR p.has_rejdp=1)) THEN 'dp,' ELSE '' END, ','),
            MAX(p.action_type='rejected' AND p.statute='101'), MAX(p.action_type='rejected' AND p.statute='102'),
            MAX(p.action_type='rejected' AND p.statute='103'), MAX(p.action_type='rejected' AND p.statute='112'),
            MAX(p.action_type='rejected' AND (p.statute='dp' OR p.has_rejdp=1)), MAX(p.alice), MAX(p.mayo), MAX(p.allowed_claim)
          FROM oa_paragraphs p JOIN applications a ON a.application = p.application
          WHERE p.oa_document IS NOT NULL GROUP BY p.application, p.oa_document""")
        return n

    # ---- what happened after each office action (events only)
    def outcomes(self):
        import itertools
        if not self.con.execute('SELECT COUNT(*) FROM office_actions').fetchone()[0]: return None   # nothing to describe; step oa says why
        if not self.con.execute('SELECT COUNT(*) FROM events').fetchone()[0]:
            return self.absent('oa_outcomes', 'events (corpus/frame-events) are needed to say what happened after each office action')
        oas = collections.defaultdict(list)
        for app, doc, date, code in self.con.execute('SELECT application, oa_document, date, doc_code FROM office_actions'): oas[app].append((doc, date, code))
        outcome = dict(self.con.execute('SELECT application, outcome FROM applications'))
        def gen():
            cur = self.con.execute('SELECT application, date, seq, code FROM events ORDER BY application, seq')
            seen = set()
            for app, grp in itertools.groupby(cur, key=lambda r: r[0]):
                evs = [(d, sq, c) for _, d, sq, c in grp]; seen.add(app)
                for doc, date, code in oas.get(app, ()):
                    d = oa_outcomes.derive(evs, date, code, outcome.get(app))
                    if d: yield (app, doc, d['next_event'], d['next_event_date'], d['interview_before_next'], d['amendment_before_next'], d['rce_or_appeal_next'], d['application_outcome'],
                                 d['anchor_quality'], d['anchor_code'], d['anchor_date'], d['next_event_state'], d['later_events'], d['abandonment_rescinded'])
            for app, lst in oas.items():   # applications with no events at all: nothing is known about what followed
                if app in seen: continue
                for doc, date, code in lst: yield (app, doc, None, None, None, None, None, outcome.get(app), 'unanchored', None, None, 'unanchored', None, None)
        rows = list(gen())
        return self.many('INSERT OR REPLACE INTO oa_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)

    # ---- full-text documents
    def documents(self):
        idx = sorted((self.data / 'corpus' / 'normalized').glob('*/index.json'))
        if not idx: return self.absent('documents', 'corpus/normalized/*/index.json')
        n = 0
        for ip in idx:
            try: doc = json.loads(ip.read_text(encoding='utf-8'))
            except ValueError: self.skipped.append((ip.parent.name, None, 'documents', 'index.json is not readable')); continue
            rows = []
            for doc_id, d in (doc.get('documents') or {}).items():
                mp = ip.parent / d['markdown']
                date = fid = None
                if mp.exists():
                    with open(mp, encoding='utf-8') as f: head = f.read(2000)
                    m = re.search(r'^official_date:\s*"?([\d-]{10})', head, re.M); date = m.group(1) if m else None
                    m = re.search(r'^text_fidelity:\s*"?([^"\n]+)', head, re.M); fid = m.group(1) if m else None
                rows.append((doc.get('application'), doc_id, d.get('code'), d.get('component'), date, d.get('pages'),
                             fid or ('OCR' if d.get('ocr_run') else None), self.rel(mp), d.get('markdown_sha256')))
            n += self.many('INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?,?)', rows)
        self.con.execute("INSERT OR REPLACE INTO sources VALUES ('corpus/normalized/*/index.json', NULL, NULL, ?, 'documents')", (n,))
        return n

    # ---- parse the full-text office actions and link them to the provider's rows
    def parse(self):
        rows = self.con.execute("SELECT application, document_id, code, official_date, markdown_path FROM documents WHERE code IN ('CTNF', 'CTFR') ORDER BY application, document_id").fetchall()
        if not rows: return None   # no full-text office actions (step documents says why)
        n, failed, parsed_rows, cites = 0, 0, [], []
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        for app, doc_id, code, date, mdp in rows:
            fp = self.data / mdp if mdp else None
            try:
                text = fp.read_text(encoding='utf-8')
                if text.startswith('---') and '\n---\n' in text: text = text.split('\n---\n', 1)[1]
                res = oa_parse.parse(text, document_id=doc_id, application_number=app, document_code=code, mail_date=date)
            except (OSError, ValueError, AttributeError, TypeError): failed += 1; continue
            h, an = res['header'], res['analysis']
            statutes = sorted({oa_outcomes.statute_level(r['statute']) for r in an['rejections']} - {None})
            refs = [{'document_number': r.get('document_number'), 'kind': r.get('kind'), 'match_basis': r.get('match_basis')} for r in an['references']]
            g = lambda k, f: (h.get(k) or {}).get(f)
            parsed_rows.append((app, doc_id, oa_parse.VERSION, g('art_unit', 'value'), g('art_unit', 'how'), g('examiner', 'value'), g('examiner', 'how'), g('mail_date', 'value'),
                                g('mail_date', 'how'), g('application_number', 'value'), g('application_number', 'how'), ','.join(statutes), json.dumps(refs), stamp))
            for r in an['references']:
                cites.append((app, doc_id, r.get('citation'), r.get('document_number'), r.get('kind'), 'parsed'))
            n += 1
        self.many('INSERT OR REPLACE INTO oa_parsed VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', parsed_rows)
        self.many('INSERT INTO oa_citations VALUES (?,?,?,?,?,?)', cites)
        self.con.execute("""INSERT OR IGNORE INTO oa_links
          SELECT o.application, o.oa_document, d.document_id, CAST(ABS(julianday(d.official_date) - julianday(o.date)) AS INTEGER),
                 'application + document code + date within 3 days'
          FROM office_actions o JOIN documents d ON d.application = o.application AND d.code = o.doc_code
          WHERE d.official_date IS NOT NULL AND o.date IS NOT NULL AND ABS(julianday(d.official_date) - julianday(o.date)) <= 3
          ORDER BY o.application, o.oa_document, ABS(julianday(d.official_date) - julianday(o.date)), d.document_id""")
        self.parse_failed = failed
        return n

    # ---- post-grant and appeals (public data you collected yourself; both optional)
    def ptab(self):
        frame_apps = {r[0] for r in self.con.execute('SELECT application FROM applications')}
        tp = self.data / 'corpus' / 'ptab' / 'trials.jsonl.gz'
        aps = sorted((self.data / 'corpus' / 'ptab').glob('appeals*.jsonl.gz'))
        if not tp.exists() and not aps: return self.absent('ptab', 'corpus/ptab/trials.jsonl.gz, corpus/ptab/appeals*.jsonl.gz')
        trials, n = {}, 0
        if tp.exists():
            dec = []
            for r in jsonl(tp):
                md, po, pe, dd, doc = (lit(r.get(k), {}) or {} for k in ('trialMetaData', 'patentOwnerData', 'regularPetitionerData', 'decisionData', 'documentData'))
                t = r.get('trialNumber'); n += 1
                app = po.get('applicationNumberText')
                cur = trials.get(t)
                row = (t, md.get('trialTypeCode'), md.get('petitionFilingDate'), md.get('trialStatusCategory'), md.get('latestDecisionDate'),
                       app, po.get('patentNumber'), po.get('groupArtUnitNumber'), po.get('realPartyInInterestName'),
                       pe.get('realPartyInInterestName'), dd.get('trialOutcomeCategory'), int(app in frame_apps))
                if not cur or (row[4] or '') >= (cur[4] or ''): trials[t] = row if not cur or row[10] else row[:10] + (cur[10],) + row[11:]
                if doc.get('documentIdentifier'):
                    dec.append((doc['documentIdentifier'], t, r.get('trialDocumentCategory'), dd.get('decisionTypeCategory'),
                                dd.get('trialOutcomeCategory'), dd.get('decisionIssueDate'), json.dumps(dd.get('statuteAndRuleBag') or []),
                                doc.get('documentName')))
            self.many('INSERT OR REPLACE INTO ptab_trials VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', trials.values())
            self.many('INSERT OR IGNORE INTO ptab_decisions VALUES (?,?,?,?,?,?,?,?)', dec)
            self.source(tp, n, 'ptab_trials, ptab_decisions')
        for p in aps:
            def gen():
                for r in jsonl(p):
                    ap, dd, doc = (lit(r.get(k), {}) or {} for k in ('appellantData', 'decisionData', 'documentData'))
                    a = ap.get('applicationNumberText')
                    yield (r.get('appealNumber'), doc.get('documentIdentifier') or '', a, ap.get('patentNumber'), ap.get('groupArtUnitNumber'),
                           dd.get('decisionIssueDate'), dd.get('appealOutcomeCategory'), json.dumps(dd.get('statuteAndRuleBag') or []),
                           json.dumps(dd.get('issueTypeBag') or []), int(a in frame_apps))
            k = self.many('INSERT OR IGNORE INTO ptab_appeals VALUES (?,?,?,?,?,?,?,?,?,?)', gen())
            self.source(p, k, 'ptab_appeals'); n += k
        return n

    def features(self):
        self.con.executemany('INSERT INTO skipped_applications VALUES (?,?,?,?)', self.skipped)
        return build_features(self.con)

    def finish(self, counts):
        self.con.executescript(INDEXES)
        meta = [('version', VERSION), ('built_at', datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')),
                ('counts', json.dumps(counts)), ('step_status', json.dumps(self.status)), ('missing', json.dumps(self.missing)),
                ('scope_id', self.scope_id or ''), ('parse_failed', str(self.parse_failed)), ('strict_rule', 'actionTypeCategory=rejected AND legalSectionCode names the statute')]
        for k, v in meta: self.con.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (k, v))
        self.con.commit(); self.con.execute('ANALYZE'); self.con.close()


def build(db=None, data_dir=None, steps=None, say=print):
    """Build the database into a new file and swap it into place. Returns the report dict (also written next to the database)."""
    data = paths.data_dir_for_write(data_dir)
    db = Path(db) if db else default_db(data_dir)
    paths.refuse_install_write(db)
    steps = list(steps) if steps else list(STEPS)
    bad = [s for s in steps if s not in STEPS]
    if bad: raise ValueError('unknown step(s): ' + ', '.join(bad) + '; steps are ' + ', '.join(STEPS))
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + f'.building{os.getpid()}')
    for stale in db.parent.glob(db.name + '.building*'):
        try: stale.unlink()
        except OSError: pass
    b, counts = Builder(tmp, data), {}
    progress = db.with_name(db.stem + '.progress.json')
    try:
        b.load_scope(); b.load_runs()
        for step in STEPS:
            if step not in steps:
                b.status[step] = 'skipped (not requested)'; continue
            t0 = time.time()
            n = getattr(b, step)()
            b.con.commit()
            counts[step] = n or 0
            b.status[step] = 'loaded' if n is not None else 'not collected'
            say(f'{step}: ' + (f'{counts[step]:,} rows ({time.time() - t0:.0f}s)' if n is not None else 'not collected (nothing found in the data folder)'))
            atomic_write_text(progress, json.dumps({'building': str(tmp.name), 'finished_steps': [s for s in STEPS if s in b.status], 'status': b.status}, indent=1) + '\n')
        b.finish(counts)
    except BaseException:
        try: b.con.close()
        except sqlite3.Error: pass
        try: tmp.unlink()
        except OSError: pass
        raise
    try: swap(tmp, db)
    except OSError:
        try: tmp.unlink()
        except OSError: pass
        raise
    report = {'db': str(db), 'bytes': db.stat().st_size, 'counts': counts, 'status': b.status, 'missing': b.missing, 'skipped_applications': len(b.skipped)}
    atomic_write_text(progress, json.dumps({**report, 'building': None, 'finished': True}, indent=1) + '\n')
    say(f'built {db} ({report["bytes"] / 1e6:.1f} MB); {len(b.missing)} dataset(s) not collected; {len(b.skipped)} application(s) skipped')
    return report


# ------------------------------------------------------------------ per-application features (for comparisons)
# Transaction codes as used for episodes: interviews (EXIN and the other examiner-interview codes), RCE (RCEX),
# AFCP request (RAFC), after-final response (A.NE), notice of appeal (N/AP), appeal brief (AP.B), pre-appeal conference
# result (APCA/APCR/APCP/APCD), restriction (CTRS), petition (PET.), non-final/final actions (CTNF/CTFR), allowance
# mailed (MN/=.). Rejection flags come from office_actions (strict rule).
INTERVIEW_CODES, APPLICANT_INTERVIEW, EXAMINER_INTERVIEW = oa_outcomes.INTERVIEW_CODES, oa_outcomes.APPLICANT_INTERVIEW, oa_outcomes.EXAMINER_INTERVIEW   # from pg_tool/event_codes.json
FEATURES = {  # name -> description (boolean columns of app_features)
    'interview': 'an examiner interview was held (any initiator)', 'interview_applicant': 'an applicant-initiated interview was held',
    'interview_examiner': 'an examiner-initiated interview was held (often the allowance call itself)', 'rce': 'an RCE was filed', 'afcp': 'an AFCP request was filed',
    'after_final_response': 'an after-final response was filed', 'notice_of_appeal': 'a notice of appeal was filed',
    'appeal_brief': 'an appeal brief was filed', 'pre_appeal': 'a pre-appeal conference was requested',
    'restriction': 'a restriction requirement was made', 'petition': 'a petition was filed',
    'final_rejection': 'at least one final rejection', 'multiple_nonfinal': 'two or more non-final rejections',
    'first_action_allowance': 'allowed without any rejection', 'rej101': '101 rejection (strict rule)',
    'rej102': '102 rejection', 'rej103': '103 rejection', 'rej112': '112 rejection', 'rejdp': 'double patenting rejection',
    'alice': 'Alice indicator on a rejection'}


def build_features(con):
    con.executescript(f"""
    DROP TABLE IF EXISTS app_features;
    CREATE TABLE app_features AS
    WITH ev AS (SELECT application,
        MAX(code IN {INTERVIEW_CODES}) interview, MAX(code IN {APPLICANT_INTERVIEW}) interview_applicant,
        MAX(code IN {EXAMINER_INTERVIEW}) interview_examiner, MAX(code='RCEX') rce, MAX(code='RAFC') afcp, MAX(code='A.NE') after_final_response,
        MAX(code='N/AP') notice_of_appeal, MAX(code='AP.B') appeal_brief, MAX(code IN ('APCA','APCR','APCP','APCD')) pre_appeal,
        MAX(code='CTRS') restriction, MAX(code='PET.') petition, SUM(code='CTNF') n_nonfinal, SUM(code='CTFR') n_final,
        MAX(code='MN/=.') allowance_mailed
      FROM events GROUP BY application),
    oa AS (SELECT application, MAX(rej101) rej101, MAX(rej102) rej102, MAX(rej103) rej103, MAX(rej112) rej112, MAX(rejdp) rejdp,
             MAX(alice) alice FROM office_actions GROUP BY application),
    sk AS (SELECT DISTINCT application FROM skipped_applications WHERE application IS NOT NULL)
    SELECT a.application, a.filing_year, a.art_unit, a.examiner, a.outcome,
      CASE WHEN a.outcome IN ('granted','allowed_not_yet_granted') THEN 1 WHEN a.outcome='abandoned' THEN 0 END AS allowed,
      COALESCE(ev.interview,0) interview, COALESCE(ev.interview_applicant,0) interview_applicant, COALESCE(ev.interview_examiner,0) interview_examiner, COALESCE(ev.rce,0) rce, COALESCE(ev.afcp,0) afcp, COALESCE(ev.after_final_response,0) after_final_response,
      COALESCE(ev.notice_of_appeal,0) notice_of_appeal, COALESCE(ev.appeal_brief,0) appeal_brief, COALESCE(ev.pre_appeal,0) pre_appeal,
      COALESCE(ev.restriction,0) restriction, COALESCE(ev.petition,0) petition,
      COALESCE(ev.n_nonfinal,0) n_nonfinal, COALESCE(ev.n_final,0) n_final,
      (COALESCE(ev.n_final,0)>0) final_rejection, (COALESCE(ev.n_nonfinal,0)>=2) multiple_nonfinal,
      (COALESCE(ev.allowance_mailed,0)=1 AND COALESCE(ev.n_nonfinal,0)=0 AND COALESCE(ev.n_final,0)=0) first_action_allowance,
      (oa.application IS NOT NULL) has_oa_data, COALESCE(oa.rej101,0) rej101, COALESCE(oa.rej102,0) rej102, COALESCE(oa.rej103,0) rej103,
      COALESCE(oa.rej112,0) rej112, COALESCE(oa.rejdp,0) rejdp, COALESCE(oa.alice,0) alice,
      (sk.application IS NOT NULL) skipped
    FROM applications a LEFT JOIN ev USING(application) LEFT JOIN oa USING(application) LEFT JOIN sk USING(application);
    CREATE UNIQUE INDEX af_app ON app_features(application);
    CREATE INDEX af_examiner ON app_features(examiner);
    """)
    return con.execute('SELECT COUNT(*) FROM app_features').fetchone()[0]


# ------------------------------------------------------------------ X vs allowance comparisons
GROUPS = {'field': 'm.field', 'filing_year': 'f.filing_year', 'art_unit': 'f.art_unit', 'examiner': 'f.examiner', 'none': "'all'"}


def population_sql(among):
    """'all' | '<feature>' | 'not:<feature>', combinable with '+' (e.g. 'rej101+final_rejection')."""
    if among in ('', 'all'): return '1'
    parts = []
    for p in among.split('+'):
        neg = p.startswith('not:'); name = p[4:] if neg else p
        if name not in FEATURES: sys.exit(f'unknown feature {name}; see: python pg_db.py features-list')
        parts.append(f'f.{name}={0 if neg else 1}')
    return ' AND '.join(parts)


def compare_rows(con, factor, among='all', field='', by='field', years=None, min_n=30, oa_only=None):
    if factor not in FEATURES: sys.exit(f'unknown feature {factor}; see: python pg_db.py features-list')
    if by not in GROUPS: sys.exit(f'--by must be one of {", ".join(GROUPS)}')
    y0, y1 = resolve_years(con, years)
    # rejection-based factors and populations are only defined where office-action rejection data exists
    oa_only = oa_only if oa_only is not None else any(x.removeprefix('not:') in ('rej101', 'rej102', 'rej103', 'rej112', 'rejdp', 'alice')
                                                       for x in [factor] + among.split('+'))
    sql = f"""SELECT {GROUPS[by]} AS grp, f.{factor} AS x, COUNT(*) AS n, SUM(f.allowed) AS k
      FROM app_features f JOIN memberships m USING(application)
      WHERE f.allowed IS NOT NULL AND f.skipped=0 AND f.filing_year BETWEEN :y0 AND :y1 AND ({population_sql(among)})
        AND (:field='' OR m.field=:field) {'AND f.has_oa_data=1' if oa_only else ''}
      GROUP BY grp, x"""
    cells = {}
    for grp, x, n, k in con.execute(sql, {'y0': y0, 'y1': y1, 'field': field}):
        cells.setdefault(grp, {})[x] = (n, k)
    out = []
    for grp, c in cells.items():
        (n1, k1), (n0, k0) = c.get(1, (0, 0)), c.get(0, (0, 0))
        if n1 < min_n or n0 < min_n: continue
        p1, p0 = k1 / n1, k0 / n0
        se = (p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0) ** 0.5
        out.append({'group': grp, 'with_n': n1, 'with_pct': round(100 * p1, 1), 'without_n': n0, 'without_pct': round(100 * p0, 1),
                    'diff_pts': round(100 * (p1 - p0), 1), 'ci95_pts': f'{100 * (p1 - p0 - 1.96 * se):+.1f} to {100 * (p1 - p0 + 1.96 * se):+.1f}'})
    return sorted(out, key=lambda r: (-(r['with_n'] + r['without_n'])))


def caveat(among, years, min_n):
    basis = 'all filing years in the database' if not years else f'filed {years[0]}-{years[1]}'
    return (f'Basis: applications {basis} that reached a final outcome (allowed or granted = allowed; abandoned = not); '
            f'population: {among or "all"}; groups with fewer than {min_n} applications on either side omitted; applications listed in '
            'skipped_applications are excluded. Allowance rate is allowed / (allowed + abandoned). A difference is a correlation, not an effect: '
            'applicants choose when to interview, appeal or file an RCE, and examiners and art units differ. 95% intervals are '
            'normal-approximation intervals for the difference. Descriptive data for a registered practitioner; not legal advice.')


def render_rows(rows, fmt):
    if not rows: return '(no group meets the minimum size)\n'
    cols = list(rows[0])
    if fmt == 'csv':
        out = io.StringIO(); w = csv.DictWriter(out, cols); w.writeheader(); w.writerows(rows); return out.getvalue()
    f = lambda v: f'{v:,}' if isinstance(v, int) and abs(v) >= 10000 else str(v)
    return '\n'.join(['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)] + ['| ' + ' | '.join(f(r[c]) for c in cols) + ' |' for r in rows]) + '\n'


def compare(db, factor, among='all', field='', by='field', years=None, min_n=30, fmt='md'):
    con = connect_ro(db)
    try: rows = compare_rows(con, factor, among, field, by, years, min_n)
    finally: con.close()
    if fmt == 'csv': return render_rows(rows, fmt)
    return f'### Allowance rate with vs without: {FEATURES[factor]}\n\n{render_rows(rows, fmt)}\n{caveat(among, years, min_n)}\n'


def compare_all(db, among='all', field='', years=None, min_n=30, fmt='md'):
    """Every feature against allowance in one population, one row per field, ranked by difference."""
    con, rows = connect_ro(db), []
    try:
        for fac in FEATURES:
            if fac in [p.removeprefix('not:') for p in among.split('+')]: continue
            for r in compare_rows(con, fac, among, field, 'field', years, min_n):
                rows.append({'factor': fac, 'field': r['group'], **{k: v for k, v in r.items() if k != 'group'}})
    finally: con.close()
    rows.sort(key=lambda r: (r['field'], -r['diff_pts']))
    if fmt == 'csv': return render_rows(rows, fmt)
    return f'### What goes with allowance: every factor, population "{among or "all"}"\n\n{render_rows(rows, fmt)}\n{caveat(among, years, min_n)}\n'


# ------------------------------------------------------------------ reports
# Each report: (description, SQL). Parameters: :field ('' = all fields), :min_n, :examiner, :art_unit ('' = all), :y0, :y1.
# Rates use the strict rule. Applications in skipped_applications are left out of every denominator.
REPORTS = {
 'overview': ('Applications per field and filing year, with outcome shares', """
   SELECT m.field, a.filing_year, COUNT(*) AS applications,
     ROUND(100.0*SUM(a.outcome='granted')/COUNT(*),1) AS pct_granted, ROUND(100.0*SUM(a.outcome='abandoned')/COUNT(*),1) AS pct_abandoned,
     ROUND(100.0*SUM(a.outcome IN ('pending','allowed_not_yet_granted'))/COUNT(*),1) AS pct_open
   FROM memberships m JOIN applications a USING(application) WHERE (:field='' OR m.field=:field)
     AND a.application NOT IN (SELECT application FROM skipped_applications WHERE application IS NOT NULL)
     AND a.filing_year BETWEEN :y0 AND :y1
   GROUP BY m.field, a.filing_year ORDER BY m.field, a.filing_year"""),
 'rejections_by_field': ('Share of applications that ever received each rejection (strict rule)', """
   WITH r AS (SELECT application, MAX(rej101) r101, MAX(rej102) r102, MAX(rej103) r103, MAX(rej112) r112, MAX(rejdp) rdp, MAX(alice) alice
              FROM office_actions GROUP BY application)
   SELECT m.field, COUNT(*) AS applications_with_oa_data,
     ROUND(100.0*SUM(r101)/COUNT(*),1) AS pct_101, ROUND(100.0*SUM(alice)/COUNT(*),1) AS pct_alice_flag,
     ROUND(100.0*SUM(r102)/COUNT(*),1) AS pct_102, ROUND(100.0*SUM(r103)/COUNT(*),1) AS pct_103,
     ROUND(100.0*SUM(r112)/COUNT(*),1) AS pct_112, ROUND(100.0*SUM(rdp)/COUNT(*),1) AS pct_dp
   FROM r JOIN memberships m USING(application) JOIN applications a USING(application)
   WHERE (:field='' OR m.field=:field) AND a.filing_year BETWEEN :y0 AND :y1
     AND r.application NOT IN (SELECT application FROM skipped_applications WHERE application IS NOT NULL) GROUP BY m.field"""),
 '101_outcomes': ('Applications with a 101 rejection: eventual outcome, by field and filing year', """
   WITH r AS (SELECT application, MAX(alice) alice FROM office_actions WHERE rej101 GROUP BY application)
   SELECT m.field, a.filing_year, COUNT(*) AS apps_with_101, SUM(r.alice) AS alice_flagged,
     ROUND(100.0*SUM(a.outcome='granted')/COUNT(*),1) AS pct_granted, ROUND(100.0*SUM(a.outcome='abandoned')/COUNT(*),1) AS pct_abandoned,
     ROUND(100.0*SUM(a.outcome IN ('pending','allowed_not_yet_granted'))/COUNT(*),1) AS pct_open
   FROM r JOIN applications a USING(application) JOIN memberships m USING(application)
   WHERE (:field='' OR m.field=:field) AND a.filing_year BETWEEN :y0 AND :y1
     AND r.application NOT IN (SELECT application FROM skipped_applications WHERE application IS NOT NULL)
   GROUP BY m.field, a.filing_year HAVING COUNT(*) >= :min_n ORDER BY m.field, a.filing_year"""),
 '101_art_units': ('101 rejection rate by art unit (applications with OA data); outcome after a 101 rejection among RESOLVED applications only, open ones counted apart', """
   WITH r AS (SELECT application, MAX(rej101) r101 FROM office_actions GROUP BY application)
   SELECT m.field, a.art_unit, COUNT(*) AS applications, SUM(r101) AS with_101, ROUND(100.0*SUM(r101)/COUNT(*),1) AS pct_101,
     SUM(r101 AND a.outcome IN ('granted','allowed_not_yet_granted','abandoned')) AS resolved_after_101,
     SUM(r101 AND a.outcome NOT IN ('granted','allowed_not_yet_granted','abandoned')) AS open_after_101,
     CASE WHEN SUM(r101) >= :min_n AND SUM(r101 AND a.outcome IN ('granted','allowed_not_yet_granted','abandoned')) >= :min_n
          THEN ROUND(100.0*SUM(r101 AND a.outcome IN ('granted','allowed_not_yet_granted'))/SUM(r101 AND a.outcome IN ('granted','allowed_not_yet_granted','abandoned')),1) END AS pct_allowed_of_resolved_after_101
   FROM r JOIN applications a USING(application) JOIN memberships m USING(application)
   WHERE (:field='' OR m.field=:field) AND a.filing_year BETWEEN :y0 AND :y1
     AND r.application NOT IN (SELECT application FROM skipped_applications WHERE application IS NOT NULL)
   GROUP BY m.field, a.art_unit HAVING COUNT(*) >= :min_n ORDER BY pct_101 DESC"""),
 'ptab_appeals_by_field': ('PTAB ex parte appeal decisions on frame applications, by field and outcome', """
   SELECT m.field, p.outcome, COUNT(*) AS decisions FROM ptab_appeals p JOIN memberships m USING(application)
   WHERE (:field='' OR m.field=:field) GROUP BY m.field, p.outcome ORDER BY m.field, decisions DESC"""),
 'ptab_trials_by_field': ('AIA trials (IPR/PGR/CBM) against frame patents, by field, type and status', """
   SELECT m.field, t.trial_type, t.status, COUNT(*) AS trials FROM ptab_trials t JOIN memberships m ON m.application=t.application
   WHERE (:field='' OR m.field=:field) GROUP BY m.field, t.trial_type, t.status ORDER BY m.field, trials DESC"""),
 'examiners': ('Examiners (by art unit): allowance rate vs their art unit, rejection mix, actions; the command line also shows interview, RCE and appeal shares (the MCP tool does not: they are applicant steps per named examiner)', """
   WITH d AS (SELECT f.*, m.field FROM app_features f JOIN memberships m USING(application)
              WHERE f.allowed IS NOT NULL AND f.skipped=0 AND f.filing_year BETWEEN :y0 AND :y1 AND (:field='' OR m.field=:field)
                AND (:art_unit='' OR f.art_unit=:art_unit)),
        au AS (SELECT field, art_unit, AVG(allowed) au_rate FROM d GROUP BY field, art_unit)
   SELECT d.field, d.examiner, d.art_unit, COUNT(*) AS decided, ROUND(100*AVG(d.allowed),1) AS pct_allowed,
     ROUND(100*au.au_rate,1) AS art_unit_pct_allowed, ROUND(100*(AVG(d.allowed)-au.au_rate),1) AS vs_art_unit_pts,
     ROUND(100.0*SUM(d.rej101)/NULLIF(SUM(d.has_oa_data),0),1) AS pct_101, ROUND(100.0*SUM(d.rej103)/NULLIF(SUM(d.has_oa_data),0),1) AS pct_103,
     ROUND(AVG(d.n_nonfinal+d.n_final),2) AS rejections_per_app, ROUND(100*AVG(d.first_action_allowance),1) AS pct_first_action_allowance,
     ROUND(100*AVG(d.interview),1) AS pct_interview, ROUND(100*AVG(d.rce),1) AS pct_rce, ROUND(100*AVG(d.notice_of_appeal),1) AS pct_appeal
   FROM d JOIN au USING(field, art_unit)
   WHERE d.examiner IS NOT NULL AND (:examiner='' OR d.examiner LIKE :examiner)
   GROUP BY d.field, d.examiner, d.art_unit HAVING COUNT(*) >= :min_n ORDER BY decided DESC LIMIT 500"""),
 'examiner_101': ('Examiners with 101 rejections: how often they issue them and how often those applications were allowed', """
   WITH d AS (SELECT f.*, m.field FROM app_features f JOIN memberships m USING(application)
              WHERE f.allowed IS NOT NULL AND f.skipped=0 AND f.has_oa_data=1 AND f.filing_year BETWEEN :y0 AND :y1 AND (:field='' OR m.field=:field)
                AND (:art_unit='' OR f.art_unit=:art_unit) AND f.examiner IS NOT NULL AND (:examiner='' OR f.examiner LIKE :examiner))
   SELECT field, examiner, art_unit, COUNT(*) AS decided, SUM(rej101) AS with_101, ROUND(100.0*SUM(rej101)/COUNT(*),1) AS pct_101,
     ROUND(100.0*SUM(rej101*allowed)/NULLIF(SUM(rej101),0),1) AS pct_allowed_after_101
   FROM d GROUP BY field, examiner, art_unit HAVING SUM(rej101) >= :min_n ORDER BY with_101 DESC LIMIT 500"""),
 'art_units': ('Art units: allowance rate, rejection mix, actions per application, interview and appeal rates', """
   SELECT m.field, f.art_unit, COUNT(*) AS decided, COUNT(DISTINCT f.examiner) AS examiners, ROUND(100*AVG(f.allowed),1) AS pct_allowed,
     ROUND(100.0*SUM(f.rej101)/NULLIF(SUM(f.has_oa_data),0),1) AS pct_101, ROUND(100.0*SUM(f.rej103)/NULLIF(SUM(f.has_oa_data),0),1) AS pct_103,
     ROUND(AVG(f.n_nonfinal+f.n_final),2) AS rejections_per_app, ROUND(100*AVG(f.interview),1) AS pct_interview, ROUND(100*AVG(f.notice_of_appeal),1) AS pct_appeal
   FROM app_features f JOIN memberships m USING(application)
   WHERE f.allowed IS NOT NULL AND f.skipped=0 AND f.filing_year BETWEEN :y0 AND :y1 AND (:field='' OR m.field=:field) AND (:art_unit='' OR f.art_unit=:art_unit)
   GROUP BY m.field, f.art_unit HAVING COUNT(*) >= :min_n ORDER BY decided DESC"""),
 'coverage': ('What the database holds, what was not collected, and the denominators', None),
}


def connect_ro(db):
    """Open read-only, for one call. A build never disturbs a reader and a reader never blocks a build."""
    if not Path(db).exists(): sys.exit(f'{db} not found; run: python pg_cli.py db build')
    con = sqlite3.connect(f'file:{Path(db).resolve().as_posix()}?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    return con


def render(cur, fmt):
    cols = [d[0] for d in cur.description]; rows = cur.fetchall()
    if fmt == 'csv':
        out = io.StringIO(); w = csv.writer(out); w.writerow(cols); w.writerows(rows); return out.getvalue()
    f = lambda v: '' if v is None else (f'{v:,}' if isinstance(v, int) and abs(v) >= 10000 else str(v))  # years stay 2021
    return '\n'.join(['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)] + ['| ' + ' | '.join(f(v) for v in r) + ' |' for r in rows])


def coverage_rows(con):
    """The coverage report as (item, value, detail) rows, computed from the database."""
    meta = dict(con.execute('SELECT key, value FROM meta'))
    status = json.loads(meta.get('step_status') or '{}'); missing = {m['dataset']: m['looked_for'] for m in json.loads(meta.get('missing') or '[]')}
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    rows = [('database', f"schema v{meta.get('version')}", f"built {meta.get('built_at')}")]
    rows.append(('scope', 'present' if q('SELECT COUNT(*) FROM scope') else 'not collected', (meta['scope_id'][:16] if meta.get('scope_id') else 'no scope.json in the data folder')))
    for name, table, step in (('frames', 'applications', 'frames'), ('events', 'events', 'events'), ('office-action rejections', 'oa_paragraphs', 'oa'),
                              ('full-text documents', 'documents', 'documents'), ('PTAB trials', 'ptab_trials', 'ptab'), ('PTAB appeals', 'ptab_appeals', 'ptab')):
        n = q(f'SELECT COUNT(*) FROM {table}')
        key = {'frames': 'frames', 'events': 'events', 'office-action rejections': 'oa_rejections', 'full-text documents': 'documents'}.get(name, 'ptab')
        rows.append((name, f'{n:,} rows' if n else ('not collected' if key in missing else 'none'), missing.get(key, status.get(step, ''))))
    napps = q('SELECT COUNT(*) FROM applications'); skipped = q('SELECT COUNT(DISTINCT application) FROM skipped_applications WHERE application IS NOT NULL')
    rows.append(('applications', f'{napps:,}', f'{skipped:,} skipped (listed in skipped_applications, excluded from denominators)'))
    with_oa = q('SELECT COUNT(DISTINCT application) FROM office_actions')
    rows.append(('applications with office-action data', f'{with_oa:,}', f'{100 * with_oa / napps:.1f}% of applications' if napps else 'no applications'))
    lo, hi = con.execute('SELECT MIN(date), MAX(date) FROM office_actions').fetchone()
    rows.append(('office-action dates', f'{lo} to {hi}' if lo else 'none', 'earliest and latest action date in your data'))
    nact = q('SELECT COUNT(*) FROM office_actions')
    matched = q("""SELECT COUNT(*) FROM office_actions o WHERE EXISTS (SELECT 1 FROM documents d WHERE d.application=o.application AND d.code=o.doc_code
                   AND d.official_date IS NOT NULL AND o.date IS NOT NULL AND ABS(julianday(d.official_date)-julianday(o.date)) <= 3)""") if nact else 0
    rows.append(('office actions with a full-text document', f'{matched:,} of {nact:,}', 'matched on application + document code + date within 3 days' if nact else 'no office actions'))
    for q_, n in con.execute("SELECT COALESCE(anchor_quality, 'not derived'), COUNT(*) FROM oa_outcomes GROUP BY 1 ORDER BY 1"):
        rows.append((f'office actions, event anchor: {q_}', f'{n:,}', 'mail_exact = the mailing event on the record date; mail_near = within 1 day; ct_near = no mailing event, the action event within 14 days before to 1 day after; unanchored = no matching event, so nothing is said about what followed'))
    for st_, n in con.execute("SELECT next_event_state, COUNT(*) FROM oa_outcomes WHERE anchor_quality != 'unanchored' GROUP BY 1 ORDER BY 2 DESC"):
        rows.append((f'after an anchored action: {st_}', f'{n:,}', 'recognised = a later turning point; later_events_none_recognised = later events, none with a known code; no_later_event = nothing after the action yet'))
    for app_, doc_ in con.execute("SELECT application, oa_document FROM oa_outcomes WHERE anchor_quality = 'unanchored' ORDER BY 1, 2 LIMIT 10"):
        rows.append(('unanchored office action', untrusted.clean_line(f'{app_} / {doc_}', 80), 'no mailing event on its date (+-1 day) and no action event within 14 days before to 1 day after (first 10 shown)'))
    for code_, n, desc_, off_ in con.execute("SELECT code, events, description, in_official_list FROM event_codes WHERE role IS NULL ORDER BY events DESC, code LIMIT 40"):
        rows.append((f'event code with no role in pg_tool/event_codes.json: {untrusted.clean_line(code_, 40)}', f'{n:,} events', untrusted.clean_line(desc_ or 'no description', 200) + ('' if off_ else ' [NOT in the official USPTO list: description is from your frame file, if any]')))
    rows.append(('transaction codes', 'checked against the USPTO PatEx 2022 list', "the descriptions are the USPTO's; which codes count as an interview, an abandonment and so on is this tool's own mapping (pg_tool/event_codes.json)"))
    for kind, n in con.execute('SELECT row_kind, COUNT(*) FROM oa_paragraphs GROUP BY 1 ORDER BY 2 DESC'):
        rows.append((f'office-action rows: {kind}', f'{n:,}', 'only rows labelled rejection count as rejections; the others are kept and labelled'))
    linked = q('SELECT COUNT(*) FROM oa_links l JOIN oa_parsed p ON p.application = l.application AND p.oa_document = l.document_id')
    if linked:
        for st in ('101', '102', '103', '112'):
            both, prov, pars = con.execute(f"""SELECT COALESCE(SUM(a AND b), 0), COALESCE(SUM(a AND NOT b), 0), COALESCE(SUM(b AND NOT a), 0) FROM (
              SELECT instr(',' || COALESCE(o.statute_set, '') || ',', ',{st},') > 0 AS a, instr(',' || COALESCE(p.statute_set, '') || ',', ',{st},') > 0 AS b
              FROM oa_links l JOIN office_actions o ON o.application = l.application AND o.oa_document = l.oa_document
              JOIN oa_parsed p ON p.application = l.application AND p.oa_document = l.document_id)""").fetchone()
            rows.append((f'parser vs provider, statute {st}', f'both {both:,}; provider only {prov:,}; parser only {pars:,}', f'over {linked:,} linked office actions (the parser is rule-based and unverified)'))
    for y, n, k in con.execute('SELECT a.filing_year, COUNT(*), COUNT(DISTINCT o.application) FROM applications a LEFT JOIN office_actions o USING(application) GROUP BY a.filing_year ORDER BY a.filing_year'):
        rows.append((f'filing year {y}', f'{n:,} applications', f'{k:,} with office-action data'))
    for reason, n in con.execute("SELECT step || ': ' || reason, COUNT(*) FROM skipped_applications GROUP BY 1 ORDER BY 2 DESC LIMIT 10"):
        rows.append(('skipped', f'{n:,}', reason))
    return rows


def report(db, name, fmt='md', field='', min_n=30, examiner='', art_unit='', years=None):
    if name not in REPORTS: sys.exit(f'unknown report {name}; see: python pg_cli.py db reports')
    desc, sql = REPORTS[name]
    con = connect_ro(db)
    try:
        built = dict(con.execute('SELECT key, value FROM meta'))
        if name == 'coverage':
            rows = [dict(zip(('item', 'value', 'detail'), r)) for r in coverage_rows(con)]
            body = render_rows(rows, fmt)
        else:
            y0, y1 = resolve_years(con, years)
            body = render(con.execute(sql, {'field': field, 'min_n': min_n, 'examiner': examiner, 'art_unit': art_unit, 'y0': y0, 'y1': y1}), fmt)
    finally: con.close()
    if fmt == 'csv': return body
    return (f'### {desc}\n\n{body}\n\nSource: pg.db v{built.get("version")} built {built.get("built_at")}; '
            f'strict rejection rule; groups under {min_n} omitted where the report filters; skipped applications excluded. Descriptive, not legal advice.\n')


COUNT_COLUMN = {'overview': 'applications', 'rejections_by_field': 'applications_with_oa_data', '101_outcomes': 'apps_with_101', '101_art_units': 'applications',
                'ptab_appeals_by_field': 'decisions', 'ptab_trials_by_field': 'trials', 'examiners': 'decided', 'examiner_101': 'with_101', 'art_units': 'decided'}


# Columns the MCP tool never returns. Per NAMED examiner, an applicant action (interview, RCE, appeal) next to an outcome would invite a cause that the data cannot show.
MCP_DROP_COLUMNS = {'examiners': ('pct_interview', 'pct_rce', 'pct_appeal')}
MCP_DESCRIPTIONS = {'examiners': 'Examiners (by art unit): allowance rate vs their art unit, rejection mix, actions'}


def report_data(db, name, field='', min_n=30, examiner='', art_unit='', years=None, max_rows=500, public=False):
    """A named report as data: {'description', 'columns', 'rows', 'suppressed', 'built'}. Rows whose group is smaller than min_n are
    dropped here even for reports whose SQL does not filter (the count column is COUNT_COLUMN[name]). Raises ValueError for an unknown name."""
    if name not in REPORTS: raise ValueError(f'unknown report {name}')
    desc, sql = REPORTS[name]
    con = connect_ro(db)
    try:
        built = dict(con.execute('SELECT key, value FROM meta'))
        if name == 'coverage':
            cols, rows = ['item', 'value', 'detail'], [list(r) for r in coverage_rows(con)]
        else:
            y0, y1 = resolve_years(con, years)
            cur = con.execute(sql, {'field': field, 'min_n': min_n, 'examiner': examiner, 'art_unit': art_unit, 'y0': y0, 'y1': y1})
            cols = [d[0] for d in cur.description]; rows = [list(r) for r in cur.fetchall()]
    finally: con.close()
    if public and name in MCP_DROP_COLUMNS:
        keep = [i for i, c in enumerate(cols) if c not in MCP_DROP_COLUMNS[name]]
        cols = [cols[i] for i in keep]; rows = [[r[i] for i in keep] for r in rows]; desc = MCP_DESCRIPTIONS.get(name, desc)
    suppressed = 0
    col = COUNT_COLUMN.get(name)
    if col in (cols if col else []):
        i = cols.index(col); kept = [r for r in rows if r[i] is not None and r[i] >= min_n]; suppressed = len(rows) - len(kept); rows = kept
    return {'description': desc, 'columns': cols, 'rows': rows[:max_rows], 'truncated': len(rows) > max_rows, 'suppressed': suppressed,
            'built': {'version': built.get('version'), 'built_at': built.get('built_at')}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('cmd', choices=['build', 'reports', 'report', 'sql', 'compare', 'compare-all', 'features-list', 'refresh-features'])
    ap.add_argument('arg', nargs='?')
    ap.add_argument('--data-dir', help='data folder (default: PG_TOOL_DATA_DIR, else the per-user folder)')
    ap.add_argument('--db', help='database file (default: <data folder>/corpus/pg.db)')
    ap.add_argument('--steps', help='build only these steps: ' + ','.join(STEPS))
    ap.add_argument('--format', default='md', choices=['md', 'csv'])
    ap.add_argument('--field', default=''); ap.add_argument('--min-n', type=int, default=30)
    ap.add_argument('--examiner', default='', help="report filter; SQL LIKE pattern, e.g. 'SMITH%%'")
    ap.add_argument('--art-unit', default='')
    ap.add_argument('--among', default='all', help="comparison population: all | <feature> | not:<feature>, joined with +")
    ap.add_argument('--by', default='field', choices=list(GROUPS)); ap.add_argument('--years', default='all', help='all (default: every filing year in the database), YYYY or YYYY-YYYY')
    a = ap.parse_args(argv)
    years = parse_years(a.years)
    try:
        for v, what in ((a.db, '--db'), (a.data_dir, '--data-dir')):
            if v: paths.refuse_stream_syntax(v, what)
        db = Path(a.db) if a.db else default_db(a.data_dir)
        if a.cmd == 'build': build(db, a.data_dir, a.steps.split(',') if a.steps else None)
        elif a.cmd == 'refresh-features':
            con = sqlite3.connect(db)
            try: n = build_features(con); con.commit()
            finally: con.close()
            print(f'app_features: {n:,} rows')
        elif a.cmd == 'reports':
            for k, (d, _) in REPORTS.items(): print(f'{k:24} {d}')
            print(f'{"compare <feature>":24} allowance rate with vs without a feature (--among, --by, --field, --years)')
            print(f'{"compare-all":24} every feature against allowance in one population, ranked')
        elif a.cmd == 'features-list':
            for k, d in FEATURES.items(): print(f'{k:24} {d}')
        elif a.cmd == 'compare': sys.stdout.write(compare(db, a.arg, a.among, a.field, a.by, years, a.min_n, a.format))
        elif a.cmd == 'compare-all': sys.stdout.write(compare_all(db, a.among, a.field, years, a.min_n, a.format))
        elif a.cmd == 'report': sys.stdout.write(report(db, a.arg, a.format, a.field, a.min_n, a.examiner, a.art_unit, years))
        else:
            q = (a.arg or '').strip()
            if not re.match(r'(?is)^(select|with)\b', q): sys.exit('read-only: SELECT or WITH queries only')
            con = connect_ro(db)
            try:
                try: print(render(con.execute(q), a.format))
                except sqlite3.Error as e: sys.exit(f'sql error: {e}')        # a bad column or table is one line, not a traceback
            finally: con.close()
    except (paths.PathError, ValueError) as e: sys.exit(str(e))


if __name__ == '__main__':
    main()
