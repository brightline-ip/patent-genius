"""Read-only access to Patent Genius's local public data (the data folder: --data-dir, PG_TOOL_DATA_DIR, or the per-user default; see pg_tool/paths.py). Nothing here writes to disk; results are cached in process
and invalidated when a source file's size or modification time changes. Missing or pruned files degrade to
'partial' with a warning (the document pipeline prunes originals while this service may be reading)."""
import ast, collections, glob, gzip, hashlib, json, os, sys
from pathlib import Path
from . import paths, stats as stats_mod

ROOT = paths.INSTALL_DIR  # install directory: templates/, schemas/, examples/
DATA = paths.resolve_data_dir()  # data folder: scope.json, corpus/ ... (optional, user-built)
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

_cache = {}
STRICT_RULE = ("A rejection counts only when an office-action row has legalSectionCode equal to the statute AND "
               "actionTypeCategory == 'rejected' (strict rule). The provider hasRej* flags also fire on "
               "statute-quotation paragraphs and overcount, especially 102.")
TREND_CAVEATS = [STRICT_RULE,
                 'CPC group is the first CPC listed in the frame row, not guaranteed to be the primary classification.',
                 'Only finished OA-rejection frames are included; see data.frames for which.',
                 'Descriptive statistics; correlations, not causes; not legal advice.']


def rel(p): return Path(p).resolve().relative_to(DATA).as_posix() if Path(p).resolve().is_relative_to(DATA) else str(p)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''): h.update(b)
    return h.hexdigest()


def _sig(paths):
    out = []
    for p in sorted(paths):
        try: st = os.stat(p); out.append((str(p), st.st_size, st.st_mtime_ns))
        except OSError: out.append((str(p), None, None))
    return tuple(out)


def cached(name, paths, build):
    sig = _sig(paths)
    hit = _cache.get(name)
    if hit and hit[0] == sig: return hit[1]
    val = build(); _cache[name] = (sig, val); return val


# ---------- frames ----------
def frame_paths():
    """Frame files in corpus/frames/. The frame id is the file stem; stems that are not valid frame ids are ignored."""
    return [p for p in sorted(glob.glob(str(DATA / 'corpus/frames/*.json.gz'))) if paths.FRAME_ID_RE.match(Path(p).name.split('.')[0]) and not paths._reserved(Path(p).name)]


def row_fields(frame, row):
    """The technology fields of one frame row, from the row itself (its 'strata' list), falling back to the frame's own field list."""
    f = [s for s in (row.get('strata') or []) if isinstance(s, str)]
    return sorted(f) or sorted((frame.get('cpc') or {}).get('strata') or [])


def frames():
    """application_number -> slim frame row (eligible rows only). Field comes from the row's own metadata, the filing year from its filing_date."""
    def build():
        from frame_contracts import load_frame
        apps = {}
        for p in frame_paths():
            fid = Path(p).name.split('.')[0]
            fr = load_frame(p)
            for r in fr['rows']:
                if not r.get('eligible'): continue
                fields = row_fields(fr, r)
                apps.setdefault(r['application_number'], {
                    'application_number': r['application_number'], 'frame': fid, 'stratum': fields[0] if fields else None, 'field': fields[0] if fields else None, 'fields': fields,
                    'filing_year': str(r.get('filing_date') or '')[:4] or None,
                    'filing_date': r.get('filing_date'), 'art_unit': r.get('art_unit'), 'cpc': (r.get('cpc') or [])[:8],
                    'applicant': r.get('applicant'), 'publication_number': r.get('publication_number'), 'patent_number': r.get('patent_number'),
                    'grant_date': r.get('grant_date'), 'status_text': r.get('status_text'), 'status_date': r.get('status_date')})
        return apps
    return cached('frames', frame_paths(), build)


def patent_to_app():
    return cached('pat2app', frame_paths(), lambda: {r['patent_number']: a for a, r in frames().items() if r.get('patent_number')})


# ---------- office-action trends (strict rule) ----------
_FLAGS = ['hasRej101', 'hasRej102', 'hasRej103', 'hasRej112', 'hasRejDP']
_SECTION = {'hasRej101': {'101'}, 'hasRej102': {'102'}, 'hasRej103': {'103'}, 'hasRej112': {'112'}, 'hasRejDP': {'nonstatutory double patenting', 'statutory double patenting'}}
_IND = ['aliceIndicator', 'mayoIndicator']


def _truthy(v): return v in (1, True, '1', 'true', 'True')


def load_oa():
    """Strict rule: a rejection counts only when a row has legalSectionCode equal to the statute AND actionTypeCategory == 'rejected'.
    Reads corpus/oa-rejections/<frame>.jsonl.gz joined to corpus/frames/<frame>.json.gz. Returns ({(field, app, doc): action}, {(field, app): application})."""
    from frame_contracts import load_frame
    oas, apps = {}, {}
    for gz in sorted(oa_paths()):
        frame_id = Path(gz).name.split('.')[0]
        fp = DATA / f'corpus/frames/{frame_id}.json.gz'
        if not fp.exists(): continue
        fr = load_frame(str(fp))
        for r in fr['rows']:
            if r.get('eligible'):
                cpc = (r.get('cpc') or [''])[0]
                fields = row_fields(fr, r); stratum = fields[0] if fields else '(none)'
                apps[(stratum, r['application_number'])] = {'stratum': stratum, 'filing_year': str(r.get('filing_date') or '')[:4] or None, 'art_unit': r.get('art_unit'),
                    'cpc_group': cpc.split('/')[0] if cpc else '(none)', 'cpc_subclass': cpc[:4] if cpc else '(none)'}
        by_app = {a: s for (s, a) in apps}
        with gzip.open(gz, 'rt', encoding='utf-8') as fh:
            for line in fh:
                d = json.loads(line); a = d.get('patentApplicationNumber')
                if a not in by_app: continue
                stratum = by_app[a]; k = (stratum, a, d.get('obsoleteDocumentIdentifier'))
                o = oas.setdefault(k, {'type': d.get('legacyDocumentCodeIdentifier'), 'date': (d.get('submissionDate') or '')[:10], **{f: False for f in _FLAGS + _IND}})
                rejected = d.get('actionTypeCategory') == 'rejected'
                for f in _FLAGS:
                    if rejected and (d.get('legalSectionCode') in _SECTION[f] or (f == 'hasRejDP' and _truthy(d.get('hasRejDP')))): o[f] = True   # double patenting has no section code: the provider flag on a rejected row
                for f in _IND:
                    if _truthy(d.get(f)): o[f] = True
    return oas, apps
def oa_paths(): return [p for p in glob.glob(str(DATA / 'corpus/oa-rejections/*.jsonl.gz')) if paths.FRAME_ID_RE.match(Path(p).name.split('.')[0])]


FLAGS = ['hasRej101', 'hasRej102', 'hasRej103', 'hasRej112', 'hasRejDP']
IND = ['aliceIndicator', 'mayoIndicator']
COL = {'hasRej101': '101', 'hasRej102': '102', 'hasRej103': '103', 'hasRej112': '112', 'hasRejDP': 'double_patenting', 'aliceIndicator': 'alice', 'mayoIndicator': 'mayo'}


def oa_data():
    def build():
        oas, apps = load_oa()
        per_app = {}
        by_app_oas = collections.defaultdict(list)
        for (s, a, doc), o in oas.items(): by_app_oas[(s, a)].append((doc, o))
        for k, lst in by_app_oas.items():
            lst.sort(key=lambda x: x[1]['date'])
            p = {f: any(o[f] for _, o in lst) for f in FLAGS + IND}
            p.update(apps[k]); p['n_oa'] = len(lst); p['first101'] = lst[0][1]['hasRej101']
            p['actions'] = [{'document': d, 'type': o['type'], 'date': o['date'], 'rejections': [COL[f] for f in FLAGS if o[f]]} for d, o in lst]
            per_app[k] = p
        oa_rows = [{**apps[(s, a)], **o, 'oa_year': o['date'][:4], 'oa_kind': o['type']} for (s, a, _), o in oas.items()]
        frames_done = sorted(Path(g).name.split('.')[0] for g in oa_paths())
        return {'apps': per_app, 'oas': oa_rows, 'frames': frames_done}
    return cached('oa', oa_paths(), build)


def prosecution_for(app):
    d = oa_data()
    for (s, a), p in d['apps'].items():
        if a == app: return p
    return None


def coverage():
    """What the office-action data actually covers, computed from the files: applications with at least one action per filing year,
    and the earliest and latest action dates. Nothing here is a fixed claim about the provider's coverage."""
    d = oa_data(); fr = frames()
    by_year = collections.defaultdict(lambda: [0, 0])
    with_oa = {a for (_, a) in d['apps']}
    for a, r in fr.items():
        y = r.get('filing_year') or '(none)'
        by_year[y][0] += 1; by_year[y][1] += int(a in with_oa)
    dates = sorted(o['date'] for o in d['oas'] if o.get('date'))
    return {'frame_applications': len(fr), 'applications_with_oa': len(with_oa), 'earliest_oa_date': dates[0] if dates else None, 'latest_oa_date': dates[-1] if dates else None,
            'by_filing_year': {y: {'applications': v[0], 'with_oa_data': v[1]} for y, v in sorted(by_year.items())}}


def coverage_caveat(cov=None):
    cov = cov or coverage()
    if not cov['applications_with_oa']:
        return 'No office-action rejection data is present in your data folder, so there are no rates to report. Collect it first.'
    lo, hi = cov['earliest_oa_date'], cov['latest_oa_date']
    low = [y for y, v in cov['by_filing_year'].items() if v['applications'] and v['with_oa_data'] / v['applications'] < 0.5]
    txt = (f"Office-action rows in your data run from {lo} to {hi}; {cov['applications_with_oa']:,} of {cov['frame_applications']:,} frame applications have at least one row. "
           'Rates describe only those applications.')
    if low: txt += ' Filing years where under half of the frame applications have office-action rows (earlier actions may be missing from the source, or not collected): ' + ', '.join(low) + '.'
    return txt


DEFAULT_MIN_N = {'stratum': 5, 'filing_year': 5, 'cpc_group': 200, 'cpc_subclass': 200, 'art_unit': 200, 'oa_year': 100, 'oa_kind': 100}


def trends(group_by, filters=None, min_n=None, groups=None, limit=50):
    filters = filters or {}
    d = oa_data()
    unit = 'office_action' if group_by in ('oa_year', 'oa_kind') else 'application'
    rows = d['oas'] if unit == 'office_action' else list(d['apps'].values())

    def keep(r):
        if filters.get('stratum') and r['stratum'] != filters['stratum']: return False
        if filters.get('art_unit') and r.get('art_unit') != filters['art_unit']: return False
        if filters.get('filing_year') and r['filing_year'] != str(filters['filing_year']): return False
        if filters.get('cpc_prefix') and not (r.get('cpc_group') or '').startswith(filters['cpc_prefix'].split('/')[0]): return False
        if filters.get('oa_kind') and unit == 'office_action' and r.get('oa_kind') != filters['oa_kind']: return False
        return True
    g = collections.defaultdict(list)
    for r in rows:
        if keep(r): g[str(r.get(group_by) or '(none)')].append(r)
    mn, floor_warning = stats_mod.clamp_min_n(min_n, DEFAULT_MIN_N[group_by])   # never below the floor of 5
    out, suppressed = [], 0
    items = sorted(g.items(), key=lambda kv: kv[0] if group_by in ('filing_year', 'oa_year') else -len(kv[1]))
    for k, v in items:
        if groups and k not in groups: continue
        if len(v) < mn: suppressed += 1; continue
        counts = {COL[f]: sum(1 for x in v if x[f]) for f in FLAGS + IND}
        out.append({'group': k, 'n': len(v), 'counts': counts, 'rates': {c: round(n / len(v), 4) for c, n in counts.items()}})
    return {'floor_warning': floor_warning, 'unit': unit, 'rule': STRICT_RULE, 'frames': d['frames'], 'min_n': mn, 'coverage': coverage(),
            'denominator_note': ('applications with at least one office action in the finished frames' if unit == 'application' else 'office actions in the finished frames') + '; rates = share that received the rejection at least once',
            'rows': out[:limit], 'suppressed_groups': suppressed, 'truncated': len(out) > limit}


# ---------- post-grant ----------
def _jsonl_gz(p):
    with gzip.open(p, 'rt', encoding='utf-8') as fh:
        for line in fh:
            if line.strip(): yield json.loads(line)


def trials_index():
    p = DATA / 'corpus/ptab/trials.jsonl.gz'

    def build():
        by_pat, by_app = collections.defaultdict(list), collections.defaultdict(list)
        for r in _jsonl_gz(p):
            po = r.get('patentOwnerData') or {}; dd = r.get('documentData') or {}; md = r.get('trialMetaData') or {}; dec = r.get('decisionData') or {}
            slim = {'trial': r.get('trialNumber'), 'type': md.get('trialTypeCode'), 'status': md.get('trialStatusCategory'), 'petition_filing_date': md.get('petitionFilingDate'),
                    'document': dd.get('documentTitleText') or dd.get('documentTypeDescriptionText'), 'document_date': dd.get('documentFilingDate'),
                    'document_identifier': dd.get('documentIdentifier'), 'outcome': dec.get('trialOutcomeCategory'), 'grounds': dec.get('issueTypeBag') or [],
                    'petitioner': (r.get('regularPetitionerData') or {}).get('realPartyInInterestName'), 'patent_owner': po.get('realPartyInInterestName')}
            if po.get('patentNumber'): by_pat[po['patentNumber']].append(slim)
            if po.get('applicationNumberText'): by_app[po['applicationNumberText']].append(slim)
        return {'by_patent': dict(by_pat), 'by_application': dict(by_app)}
    return cached('trials', [p], build) if p.exists() else None


def appeals_index():
    """Ex parte appeal decisions from the file you collected (it may be a partial download; absence of a record is not absence of an appeal)."""
    p = DATA / 'corpus/ptab/appeals.part.jsonl'

    def build():
        by_app = collections.defaultdict(list)
        with open(p, encoding='utf-8') as fh:
            for line in fh:
                try: r = json.loads(line)
                except ValueError: continue
                a = (r.get('appellantData') or {}).get('applicationNumberText')
                if not a: continue
                dd = r.get('documentData') or {}; dec = r.get('decisionData') or {}
                by_app[a].append({'appeal': r.get('appealNumber'), 'document_date': dd.get('documentFilingDate'), 'document_identifier': dd.get('documentIdentifier'),
                                  'outcome': dec.get('appealOutcomeCategory') or dec.get('decisionTypeCategory'), 'statutes': dec.get('statuteAndRuleBag') or []})
        return dict(by_app)
    return cached('appeals', [p], build) if p.exists() else None


def cafc_index():
    p = DATA / 'corpus/cafc/excerpts.jsonl.gz'

    def build():
        by_pat = collections.defaultdict(list)
        for r in _jsonl_gz(p):
            try: pats = r['patent_numbers'] if isinstance(r['patent_numbers'], list) else ast.literal_eval(r.get('patent_numbers') or '[]')
            except (ValueError, SyntaxError): pats = []
            slim = {'case_name': r.get('case_name'), 'docket_number': r.get('docket_number'), 'date_filed': r.get('date_filed'), 'status': r.get('status'),
                    'package_id': r.get('package_id'), 'granule_id': r.get('granule_id'), 'pdf_sha256': r.get('pdf_sha256'),
                    'source': f"https://www.govinfo.gov/app/details/{r.get('package_id')}" if r.get('package_id') else None}
            for pn in pats: by_pat[str(pn)].append(slim)
        return dict(by_pat)
    return cached('cafc', [p], build) if p.exists() else None


def family_index():
    p = DATA / 'corpus/cafc/cited-apps.jsonl'

    def build():
        by_pat, by_app = {}, {}
        with open(p, encoding='utf-8') as fh:
            for line in fh:
                if not line.strip(): continue
                r = json.loads(line); by_pat[str(r.get('patent'))] = r; by_app[str(r.get('application'))] = r
        return {'by_patent': by_pat, 'by_application': by_app}
    return cached('family', [p], build) if p.exists() else None


# ---------- normalized file-wrapper documents ----------
def app_dir(app): return DATA / 'corpus/normalized' / app


def documents(app):
    d = app_dir(app); idx = d / 'index.json'
    if not idx.exists(): return None
    docs = json.loads(idx.read_text(encoding='utf-8'))['documents']
    out = []
    for doc_id, e in docs.items():
        meta = front_matter(d / e['markdown']) if (d / e['markdown']).exists() else {}
        out.append({'document_id': doc_id, 'code': e.get('code') or meta.get('document_code'), 'component': e.get('component'),
                    'official_date': meta.get('official_date'), 'text_fidelity': meta.get('text_fidelity'), 'markdown_sha256': e.get('markdown_sha256'),
                    'present': (d / e['markdown']).exists()})
    return sorted(out, key=lambda x: (x['official_date'] or '', x['document_id']))


def front_matter(path):
    t = Path(path).read_text(encoding='utf-8')
    if not t.startswith('---'): return {}
    head = t.split('\n---\n', 1)[0]
    meta = {}
    for line in head.splitlines()[1:]:
        if ':' in line:
            k, v = line.split(':', 1); v = v.strip()
            try: meta[k.strip()] = json.loads(v) if v[:1] in '"[{' else v
            except ValueError: meta[k.strip()] = v
    return meta


def document_text(app, doc_id):
    """(metadata, body text) for one normalized document, primary part only; None if absent."""
    d = app_dir(app); idx = d / 'index.json'
    if not idx.exists(): return None
    e = json.loads(idx.read_text(encoding='utf-8'))['documents'].get(doc_id)
    if not e: return None
    prim = next((p for p in e.get('parts', []) if p.get('role') == 'primary'), {'file': e['markdown']})
    p = d / prim['file']
    if not p.exists(): return None
    t = p.read_text(encoding='utf-8')
    meta = front_matter(p); meta.setdefault('document_code', e.get('code'))
    meta['index_code'] = e.get('code'); meta['ocr_run'] = e.get('ocr_run')
    if not meta.get('official_date'):  # the per-application file-wrapper inventory carries the official date
        inv = DATA / 'public-originals' / app / 'inventory.json'
        if inv.exists():
            row = next((x for x in json.loads(inv.read_text(encoding='utf-8')).get('documents', []) if x.get('document_id') == doc_id), None)
            if row and row.get('official_date'): meta['official_date'] = row['official_date'][:10]
    return meta, t.split('\n---\n', 1)[-1] if t.startswith('---') else t
