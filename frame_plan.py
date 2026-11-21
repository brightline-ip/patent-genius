"""Generate a frame plan with fixed-length windows (default 7 days) for one field from a CPC expansion file. Short
windows keep each query small enough for stable offset paging. Provider wildcards are derived from the seeds: a subclass
seed gives 'H10B*'; a group seed gives the padded main-group form ('G06N3/02' -> 'G06N???3*', 'G06N20/00' -> 'G06N??20*');
historical prefixes give 'H01L*'. Wildcard hits are post-filtered against the exact official symbol set.

A "field" is a name from your scope.json (inside the frame files it is still called a stratum).
Usage: python frame_plan.py <frame_id> <field> [<from YYYY-MM-DD> <to YYYY-MM-DD, exclusive> [days]] [--out <plan.json>]
                            [--data-dir <folder>] [--expansion <expansions.json>]
Without dates, the filing range and window length come from scope.json in the data folder. Without --expansion, the file
corpus/cpc/<version>/expansions.json named by the scope's cpc_title_list.version is used, else the first expansion file
that defines the field. The plan is written to <data folder>/corpus/frame-plans/<frame_id>.json unless --out is given."""
import argparse, hashlib, json, re, sys
from datetime import date, timedelta
from pathlib import Path
from pg_tool import paths, scope as scope_mod


def _base(base): return Path(base) if base is not None else paths.resolve_data_dir()


def expansion_files(base=None):
    base = _base(base)
    return sorted(p.relative_to(base).as_posix() for p in (base / 'corpus' / 'cpc').glob('*/expansions.json'))


def expansion_file(field, base=None, explicit=None, version=None):
    """Relative path (inside the data folder) of the expansion file to use for the field."""
    base = _base(base).resolve()
    if explicit:
        p = Path(explicit); p = (p if p.is_absolute() else base / p).resolve()
        if base not in p.parents: raise ValueError(f'the expansion file must be inside the data folder ({base}); copy it there first')
        if not p.is_file(): raise FileNotFoundError(f'expansion file not found: {p}')
        if field not in json.loads(p.read_bytes())['strata']: raise KeyError(f'field {field} is not defined in {p.name}')
        return p.relative_to(base).as_posix()
    found = expansion_files(base)
    if version:
        want = f'corpus/cpc/{version}/expansions.json'
        if want in found and field in json.loads((base / want).read_bytes())['strata']: return want
    for f in found:
        if field in json.loads((base / f).read_bytes())['strata']: return f
    raise KeyError(f'field {field} is in no expansion file under corpus/cpc/<version>/expansions.json ({", ".join(found) or "none found"}); build one with cpc_expand.py')


def wildcard(seed):
    if re.fullmatch(r'[A-HY]\d{2}[A-Z]', seed): return seed + '*'
    m = re.fullmatch(r'([A-HY]\d{2}[A-Z])(\d{1,4})/\d+', seed)
    if not m: raise ValueError('bad seed ' + seed)
    return m.group(1) + '?' * (4 - len(m.group(2))) + m.group(2) + '*'


def plan(frame_id, stratum, start, end, days=7, base=None, expansion=None, version=None):
    paths.check_frame_id(frame_id); paths.check_field(stratum)
    base = _base(base)
    exp_file = expansion_file(stratum, base, expansion, version)
    raw = (base / exp_file).read_bytes()
    ex = json.loads(raw); x = ex['strata'][stratum]
    # a main-group prefix ('H04B7/') is already covered by its seed's padded wildcard; '/' is not valid in a query term
    terms = sorted({wildcard(s) for s in x['seeds']} | {h + '*' for h in x.get('historical_prefixes', []) if '/' not in h})
    clause = '(' + ' OR '.join('applicationMetaData.cpcClassificationBag:' + t for t in terms) + ')'
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if not d0 < d1: raise ValueError('empty period')
    windows, queries = [], []
    while d0 < d1:
        e = min(d0 + timedelta(days=days), d1); wid = 'w' + d0.isoformat()
        windows.append({'id': wid, 'from': d0.isoformat(), 'to': e.isoformat(), 'basis': 'filing_date'})
        q = clause + f' AND applicationMetaData.filingDate:[{d0.isoformat()} TO {(e - timedelta(days=1)).isoformat()}]'
        if len(q) > 2000: raise ValueError('query exceeds 2000 characters')
        queries.append({'query_id': f'q-{stratum}-{wid}', 'stratum': stratum, 'window_id': wid, 'q': q})
        d0 = e
    return {'frame_id': frame_id,
            'date_convention': 'Provider applicationMetaData.filingDate (YYYY-MM-DD; for national-stage entries the 371 date). Windows half-open [from, to); ODP ranges are inclusive, so each q ends on the last day of its window.',
            'windows': windows,
            'cpc': {'hierarchy_version': ex['cpc_version'], 'strata': {stratum: {'seed_symbols': x['seeds'], 'expanded_symbols': [],
                    'historical_prefixes': x.get('historical_prefixes', []), 'provider_wildcards': terms,
                    'expansion': {'file': exp_file, 'stratum': stratum, 'sha256': hashlib.sha256(raw).hexdigest()}}}},
            'queries': queries,
            'notes': [f'{days}-day windows keep each query small for stable offset paging.',
                      'Provider wildcards over-reach at main-group or subclass level; rows are kept only if a CPC is in the official expanded set or starts with a historical prefix.',
                      'A record enters the stratum if ANY listed CPC matches; ODP does not distinguish inventive from additional CPCs here.']}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(usage=__doc__, add_help=True)
    ap.add_argument('frame_id'); ap.add_argument('field'); ap.add_argument('rest', nargs='*')
    ap.add_argument('--out'); ap.add_argument('--data-dir'); ap.add_argument('--expansion')
    a = ap.parse_args()
    try:
        base = paths.data_dir_for_write(a.data_dir)
        version = None
        if len(a.rest) >= 2: start, end, days = a.rest[0], a.rest[1], int(a.rest[2]) if len(a.rest) > 2 else 7
        elif not a.rest:
            sc, _ = scope_mod.load(base)
            if a.field not in [f['name'] for f in sc['fields']]: sys.exit(f'field {a.field} is not in {base / "scope.json"}')
            start, end, days = sc['filing']['from'], sc['filing']['to_exclusive'], sc['filing'].get('window_days', 7)
        else: sys.exit('give both <from> and <to>, or neither (then scope.json is used)')
        if not a.expansion:
            try: version = (scope_mod.load(base)[0].get('cpc_title_list') or {}).get('version')
            except (FileNotFoundError, ValueError): version = None
        out = Path(a.out) if a.out else base / 'corpus' / 'frame-plans' / f'{a.frame_id}.json'
        paths.refuse_install_write(out)
        if out.exists(): sys.exit(f'refusing to overwrite {out}')
        p = plan(a.frame_id, a.field, start, end, days, base=base, expansion=a.expansion, version=version)
    except (paths.PathError, KeyError, ValueError, FileNotFoundError) as e:
        sys.exit(str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(p, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'frame_id': p['frame_id'], 'plan': str(out), 'windows': len(p['windows']), 'wildcards': p['cpc']['strata'][a.field]['provider_wildcards']}, indent=1))
