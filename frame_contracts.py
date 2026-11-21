"""Structural contract for a frozen sampling frame (corpus/frame.schema.json).
Checks identity, window membership, deduplication, funnel arithmetic, stratum
membership and selection bookkeeping. It cannot verify provider coverage,
CPC expansion correctness or eligibility judgements."""
import gzip, json, re, sys
from pathlib import Path


def load_frame(path):
    """Read a frame/plan JSON file; '.gz' files are gzip-compressed (frames are large and stored compressed)."""
    from pg_tool import paths
    p = paths.checked_path(path, 'the frame path'); raw = p.read_bytes()
    return json.loads(gzip.decompress(raw) if p.suffix == '.gz' else raw)


def dump_frame(obj, path):
    """Write JSON; gzip with a fixed mtime when the path ends in '.gz' so identical content gives identical bytes."""
    p = Path(path); text = json.dumps(obj, separators=(',', ':')) if p.suffix == '.gz' else json.dumps(obj, indent=1)
    data = (text + '\n').encode('utf-8')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(data, mtime=0) if p.suffix == '.gz' else data)

DATE = re.compile(r'\d{4}-\d{2}-\d{2}$')
APP = re.compile(r'\d{8}$')


def frame_errors(f):
    errors = []
    err = errors.append
    if not isinstance(f, dict): return ['frame must be object']
    if f.get('frame_version') != 1: err('frame_version must be 1')
    for k in ('frame_id', 'frozen_at', 'date_convention'):
        if not isinstance(f.get(k), str) or not f[k]: err(k + ' required')
    windows = {}
    for w in f.get('windows', []) if isinstance(f.get('windows'), list) else [None]:
        if not isinstance(w, dict) or not all(isinstance(w.get(k), str) for k in ('id', 'from', 'to', 'basis')): err('window needs id/from/to/basis'); continue
        if not DATE.match(w['from']) or not DATE.match(w['to']) or w['from'] >= w['to']: err('window dates invalid or empty')
        if w['id'] in windows: err('duplicate window id')
        windows[w['id']] = w
    if not windows: err('at least one window required')
    for a in windows.values():
        for b in windows.values():
            if a is not b and a['basis'] == b['basis'] and a['from'] < b['to'] and b['from'] < a['to']: err('windows overlap: ' + a['id'] + '/' + b['id'])
    cpc = f.get('cpc') if isinstance(f.get('cpc'), dict) else {}
    if not isinstance(cpc.get('hierarchy_version'), str): err('cpc hierarchy_version required')
    strata = cpc.get('strata') if isinstance(cpc.get('strata'), dict) else {}
    if not strata: err('cpc strata required')
    for name, s in strata.items():
        if not isinstance(s, dict) or not isinstance(s.get('expanded_symbols'), list) or not s['expanded_symbols']: err('stratum ' + name + ' needs expanded_symbols')
    queries = {}
    for q in f.get('queries', []) if isinstance(f.get('queries'), list) else [None]:
        if not isinstance(q, dict) or not isinstance(q.get('query_id'), str): err('query needs query_id'); continue
        if q['query_id'] in queries: err('duplicate query id')
        queries[q['query_id']] = q
        if q.get('stratum') not in strata: err('query stratum unknown: ' + str(q.get('stratum')))
        if q.get('window_id') not in windows: err('query window unknown')
        if not isinstance(q.get('q'), str) or not q['q']: err('query text required')
        pages = q.get('pages')
        if not isinstance(pages, list): err('query pages required'); continue
        for p in pages:
            if not isinstance(p, dict) or not re.fullmatch('[0-9a-f]{64}', str(p.get('response_sha256', ''))): err('page response_sha256 required')
            if type(p.get('rows')) is not int or type(p.get('offset')) is not int or type(p.get('limit')) is not int: err('page offset/limit/rows must be integers')
        if q.get('provider_count') is not None and isinstance(pages, list) and all(isinstance(p, dict) for p in pages):
            got = sum(p.get('rows', 0) for p in pages if type(p.get('rows')) is int)
            if got < q['provider_count'] and q.get('truncated') is not True: err('query ' + q['query_id'] + ' retrieved fewer rows than provider_count without truncated=true')
    rows = f.get('rows') if isinstance(f.get('rows'), list) else []
    seen = set(); eligible = 0; excluded_rows = 0; families = set(); seed_rows = 0
    for r in rows:
        if not isinstance(r, dict): err('row must be object'); continue
        a = str(r.get('application_number', ''))
        if not APP.match(a): err('row application_number must be 8 digits')
        if a in seen: err('duplicate application row ' + a)
        seen.add(a)
        fd = r.get('filing_date')
        if not isinstance(fd, str) or not DATE.match(fd): err(a + ': filing_date required'); fd = None
        if not isinstance(r.get('cpc'), list): err(a + ': cpc must be array')
        st = r.get('strata')
        if not isinstance(st, list) or not st: err(a + ': strata required')
        else:
            for s in st:
                if s not in strata: err(a + ': unknown stratum ' + str(s))
        qids = r.get('query_ids')
        if not isinstance(qids, list) or not qids: err(a + ': query_ids required')
        else:
            for qid in qids:
                q = queries.get(qid)
                if q is None: err(a + ': unknown query ' + str(qid)); continue
                w = windows.get(q.get('window_id'))
                if fd and w and w['basis'] == 'filing_date' and not (w['from'] <= fd < w['to']): err(a + ': filing_date outside window ' + w['id'])
        if r.get('source') == 'seed_panel':
            seed_rows += 1
            if not r.get('seed_reason'): err(a + ': seed_panel row needs seed_reason')
        if r.get('eligible') is True:
            eligible += 1
            if r.get('family_key'): families.add(r['family_key'])
        elif r.get('eligible') is False:
            excluded_rows += 1
            if not r.get('exclusion_reason'): err(a + ': excluded row needs exclusion_reason')
        else: err(a + ': eligible must be boolean')
    fun = f.get('funnel') if isinstance(f.get('funnel'), dict) else {}
    for k in ('candidates', 'deduplicated', 'eligible', 'excluded', 'families', 'selected'):
        if type(fun.get(k)) is not int: err('funnel.' + k + ' must be integer')
    if all(type(fun.get(k)) is int for k in ('candidates', 'deduplicated', 'eligible', 'excluded', 'families', 'selected')):
        if fun['deduplicated'] != len(rows): err('funnel.deduplicated must equal row count')
        if fun['candidates'] < fun['deduplicated']: err('funnel.candidates cannot be below deduplicated')
        if fun['eligible'] != eligible or fun['excluded'] != excluded_rows: err('funnel eligible/excluded disagree with rows')
        if fun['families'] != len(families): err('funnel.families disagrees with distinct eligible family keys')
    sel = f.get('selection') if isinstance(f.get('selection'), dict) else {}
    if sel.get('method') not in ('none', 'stratified_random_by_family', 'curated_panel'): err('selection.method invalid')
    chosen = sel.get('selected') if isinstance(sel.get('selected'), list) else []
    if sel.get('method') == 'stratified_random_by_family' and (not sel.get('rng') or sel.get('seed') is None): err('random selection needs rng and seed')
    by_app = {str(r.get('application_number')): r for r in rows if isinstance(r, dict)}
    for c in chosen:
        if not isinstance(c, dict): err('selection entry must be object'); continue
        if c.get('stratum') not in strata: err('selection stratum unknown')
        p = c.get('selection_probability')
        if not (isinstance(p, (int, float)) and 0 < p <= 1): err('selection_probability must be in (0,1]')
        for a in c.get('application_numbers', []) if isinstance(c.get('application_numbers'), list) else [None]:
            r = by_app.get(str(a))
            if r is None: err('selected application not in rows: ' + str(a)); continue
            if r.get('eligible') is not True: err('selected application not eligible: ' + str(a))
            if r.get('family_key') != c.get('family_key'): err('selected application family mismatch: ' + str(a))
            if r.get('source') == 'seed_panel' and sel.get('method') != 'curated_panel': err('seed_panel row cannot enter a random selection: ' + str(a))
    if type(fun.get('selected')) is int and fun['selected'] != len(chosen): err('funnel.selected disagrees with selection list')
    if f.get('frame_coverage') is None and not any('descriptive' in n.lower() for n in f.get('notes', []) if isinstance(n, str)):
        err('frame_coverage unknown: notes must state that results are descriptive for the selected sample')
    return errors


if __name__ == '__main__':
    data = load_frame(sys.argv[1])
    errs = frame_errors(data)
    print(json.dumps({'status': 'FRAME_STRUCTURALLY_VALID' if not errs else 'FRAME_INVALID', 'errors': errs}, indent=2))
    sys.exit(1 if errs else 0)
