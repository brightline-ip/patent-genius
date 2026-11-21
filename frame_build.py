"""Build a frozen frame (corpus/frame.schema.json) from a frame plan plus the raw pages that
frame_search.mjs retained. Re-verifies every page hash, post-filters provider wildcard matches
on the record's real CPC values at a group boundary, deduplicates by application number and
derives a continuity-based family key. Selection is left to a later step (method 'none').
Usage: python frame_build.py <plan.json> [<out.json>] [--data-dir <folder>]
The raw pages are read from <data folder>/public-originals/frames/. Without <out.json> the frame is written to
<data folder>/corpus/frames/<frame_id>.json.gz."""
import hashlib, json, re, sys
from datetime import datetime, timezone
from pathlib import Path
from frame_contracts import frame_errors, dump_frame
from pg_tool import paths
PREFIX = re.compile(r'[A-HY]\d{2}[A-Z](\d{1,4}/)?$')  # subclass, or subclass+main group ending at '/'


def compact_cpc(value):
    """'G06N   3/08' -> 'G06N3/08' (ODP right-aligns the group in a 4-character field)."""
    return re.sub(r'\s+', '', str(value or '')).upper()


def cpc_in(cpc, prefixes):
    return any(cpc.startswith(p) and (p.endswith('/') or len(cpc) == len(p) or cpc[len(p)].isdigit()) for p in prefixes)


def family_of(rec, app):
    parents = [p for p in rec.get('parentContinuityBag') or [] if isinstance(p, dict) and p.get('parentApplicationNumberText')]
    if not parents: return app, 'self (no parentContinuityBag entry)'
    p = min(parents, key=lambda x: (x.get('parentApplicationFilingDate') or '9999', x['parentApplicationNumberText']))
    return p['parentApplicationNumberText'], 'earliest-filed parent listed in parentContinuityBag (' + str(p.get('claimParentageTypeCode')) + '); single level, not a DOCDB family'


def build(plan, base=None):
    errors = []
    base = Path(base) if base is not None else paths.resolve_data_dir()
    try: paths.check_frame_id(plan.get('frame_id'))
    except paths.PathError as e: return {'funnel': {}}, [str(e)]
    strata = plan['cpc']['strata']
    match = {}
    for name, s in strata.items():
        if s.get('expansion'):
            # exact official symbol set from cpc_expand.py, bound by file hash; retired symbols only via historical_prefixes
            e = s['expansion']; raw = (base / e['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != e['sha256']: errors.append(f'stratum {name}: expansion file hash mismatch'); continue
            x = json.loads(raw)['strata'][e['stratum']]
            historical = x.get('historical_prefixes', [])
            # Prefix-only definitions have no scheme-expanded exact symbols. Record their
            # effective prefixes here so the frozen-frame contract remains self-contained.
            s['expanded_symbols'], s['scheme_source_sha256'] = x['symbols'] or historical, json.loads(raw)['source']['sha256']
            sym, hist = set(x['symbols']), tuple(historical)
            match[name] = lambda cpcs, sym=sym, hist=hist: any(c in sym or (hist and c.startswith(hist)) for c in cpcs)
            continue
        for p in s.get('match_prefixes', []):
            if not PREFIX.match(p): errors.append(f'stratum {name}: prefix {p!r} must be a subclass or end at a main-group "/"')
        if not s.get('match_prefixes'): errors.append(f'stratum {name}: match_prefixes or expansion required')
        match[name] = lambda cpcs, s=s: cpc_in_any(cpcs, s)
    if errors: return {'funnel': {}}, errors
    windows = {w['id']: w for w in plan['windows']}
    queries, rows, exclusions, candidates = [], {}, [], 0
    for qp in plan['queries']:
        qdir = base / 'public-originals' / 'frames' / plan['frame_id'] / qp['query_id']
        rc = json.loads((qdir / 'receipt.json').read_text(encoding='utf-8'))
        if rc.get('error'): errors.append(f"{qp['query_id']}: harvest error recorded: {rc['error']}")
        w = windows[qp['window_id']]
        for page in rc['pages']:
            raw = (qdir / Path(page['raw_path']).name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != page['response_sha256']: errors.append(f"{qp['query_id']} offset {page['offset']}: page hash mismatch"); continue
            for rec in json.loads(raw).get('patentFileWrapperDataBag') or []:
                candidates += 1
                m = rec.get('applicationMetaData') or {}
                app = str(rec.get('applicationNumberText') or '')
                cpcs = sorted({compact_cpc(c) for c in m.get('cpcClassificationBag') or [] if compact_cpc(c)})
                if app in rows:
                    r = rows[app]
                    if qp['query_id'] not in r['query_ids']: r['query_ids'].append(qp['query_id'])
                    if qp['stratum'] not in r['strata'] and match[qp['stratum']](cpcs): r['strata'].append(qp['stratum'])
                    continue
                fd = m.get('filingDate')
                if not (isinstance(fd, str) and w['from'] <= fd < w['to']):
                    # the frame contract bars out-of-window rows, so these stay in exclusions only
                    exclusions.append({'application_number': app, 'query_id': qp['query_id'], 'reason': 'filing_date outside window (' + str(fd) + ')'})
                    continue
                fam, basis = family_of(rec, app)
                r = {'application_number': app, 'filing_date': fd, 'cpc': cpcs, 'strata': [qp['stratum']], 'query_ids': [qp['query_id']],
                     'status_text': m.get('applicationStatusDescriptionText'), 'status_date': m.get('applicationStatusDate'),
                     'publication_number': m.get('earliestPublicationNumber'), 'publication_date': m.get('earliestPublicationDate'),
                     'patent_number': m.get('patentNumber'), 'grant_date': m.get('grantDate'), 'art_unit': m.get('groupArtUnitNumber'),
                     'applicant': m.get('firstApplicantName'), 'application_type': m.get('applicationTypeCode'), 'examiner': m.get('examinerNameText'),
                     'family_key': fam, 'family_basis': basis, 'source': 'frame'}
                reason = None
                if m.get('applicationTypeCode') != 'UTL': reason = 'not a utility application (' + str(m.get('applicationTypeCode')) + ')'
                elif not match[qp['stratum']](cpcs): reason = 'provider wildcard matched but no CPC passes the stratum post-filter (group-boundary prefix or official symbol set)'
                r['eligible'] = reason is None
                r['exclusion_reason'] = reason
                if reason: exclusions.append({'application_number': app, 'reason': reason})
                rows[app] = r
        queries.append({'query_id': qp['query_id'], 'stratum': qp['stratum'], 'window_id': qp['window_id'], 'route': rc['route'], 'q': rc['q'],
                        'body': {'q': rc['q'], 'pagination': {'limit': rc['page_limit']}}, 'retrieved_at': rc['retrieved_at'],
                        'provider_count': rc['provider_count'], 'max_rows': rc['max_rows'], 'truncated': rc['truncated'],
                        'pages': [{k: p[k] for k in ('offset', 'limit', 'rows', 'response_sha256', 'raw_path')} for p in rc['pages']]})
    rows = sorted(rows.values(), key=lambda r: r['application_number'])
    bad_dates = [r for r in rows if not (isinstance(r['filing_date'], str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['filing_date']))]
    if bad_dates: errors.append(f'{len(bad_dates)} rows lack a provider filing date')
    eligible = [r for r in rows if r['eligible']]
    notes = list(plan.get('notes', []))
    notes.append('Coverage unknown: results are descriptive for the selected sample only.')
    for q in queries:
        if q['truncated']: notes.append(f"Query {q['query_id']} truncated at {sum(p['rows'] for p in q['pages'])} of {q['provider_count']} provider rows; provider sort order is unverified, so the retrieved subset is not a random sample of the query.")
    frame = {'frame_version': 1, 'frame_id': plan['frame_id'], 'frozen_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'date_convention': plan['date_convention'], 'windows': plan['windows'],
             'cpc': {'hierarchy_version': plan['cpc']['hierarchy_version'], 'strata': strata},
             'queries': queries,
             'eligibility': {'rules': ['applicationTypeCode == UTL', 'provider filingDate inside the query window [from, to)',
                                       'at least one record CPC passes the stratum match_prefixes at a group boundary (whitespace-collapsed)'],
                             'family_definition': 'earliest-filed parent in parentContinuityBag, else the application itself; single-level US continuity, not a DOCDB family',
                             'dedup_rule': 'by applicationNumberText; later queries add query_ids and strata'},
             'rows': rows, 'exclusions': exclusions,
             'funnel': {'candidates': candidates, 'deduplicated': len(rows), 'eligible': len(eligible), 'excluded': len(rows) - len(eligible),
                        'families': len({r['family_key'] for r in eligible if r['family_key']}), 'selected': 0},
             'selection': {'method': 'none', 'selected': []}, 'frame_coverage': None, 'notes': notes}
    return frame, errors + frame_errors(frame)


def cpc_in_any(cpcs, stratum):
    return any(cpc_in(c, stratum['match_prefixes']) for c in cpcs)


if __name__ == '__main__':
    args = sys.argv[1:]
    data_dir = None
    if '--data-dir' in args: i = args.index('--data-dir'); data_dir = args[i + 1]; del args[i:i + 2]
    if not args: sys.exit(__doc__)
    try: base = paths.data_dir_for_write(data_dir)
    except paths.PathError as e: sys.exit(str(e))
    plan_path = paths.checked_or_exit(args[0], 'the plan path')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    try:
        out = Path(args[1]) if len(args) > 1 else base / 'corpus' / 'frames' / (paths.check_frame_id(plan.get('frame_id')) + '.json.gz')
        paths.refuse_install_write(out)
    except paths.PathError as e: sys.exit(str(e))
    if out.exists(): sys.exit(f'refusing to overwrite {out}; frames are frozen, use a new frame_id')
    frame, errs = build(plan, base)
    if not errs: dump_frame(frame, out)
    print(json.dumps({'status': 'FRAME_BUILT' if not errs else 'FRAME_NOT_WRITTEN', 'errors': errs[:50], 'funnel': frame['funnel']}, indent=1))
    sys.exit(1 if errs else 0)
