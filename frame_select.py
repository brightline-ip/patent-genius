"""Stratified random selection by family over a frozen frame. Writes a NEW frame (new frame_id,
new frozen_at) because frames are frozen; the source frame is never modified.
Each stratum: eligible non-seed rows grouped by family_key, families sorted by key, then
random.Random(seed).sample(families, min(target, n)). selection_probability = k / n.
Usage: python frame_select.py <frame.json> <new_frame_id> <seed:int> <stratum=target>... --out <file>"""
import copy, json, random, sys
from datetime import datetime, timezone
from pathlib import Path
from frame_contracts import frame_errors, load_frame, dump_frame
from pg_tool import paths

RNG = 'python random.Random(seed).sample over family keys sorted ascending, one independent draw per stratum in sorted stratum order'


def select(frame, new_id, seed, targets):
    if frame_errors(frame): raise ValueError('source frame is not structurally valid')
    paths.check_frame_id(new_id)
    if frame['selection'].get('method') != 'none' or frame['selection'].get('selected'): raise ValueError('source frame already carries a selection')
    unknown = set(targets) - set(frame['cpc']['strata'])
    if unknown: raise ValueError('unknown strata: ' + ', '.join(sorted(unknown)))
    out = copy.deepcopy(frame)
    rng = random.Random(seed)
    chosen = []
    for stratum in sorted(targets):
        fams = {}
        for r in frame['rows']:
            if r['eligible'] and r.get('source') != 'seed_panel' and stratum in r['strata'] and r.get('family_key'):
                fams.setdefault(r['family_key'], []).append(r['application_number'])
        keys = sorted(fams)
        k = min(int(targets[stratum]), len(keys))
        for fam in rng.sample(keys, k):
            chosen.append({'family_key': fam, 'application_numbers': sorted(fams[fam]), 'stratum': stratum,
                           'selection_probability': k / len(keys), 'stratum_families': len(keys)})
    out['frame_id'] = new_id
    out['frozen_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out['derived_from'] = {'frame_id': frame['frame_id'], 'frozen_at': frame['frozen_at']}
    out['selection'] = {'method': 'stratified_random_by_family', 'rng': RNG, 'seed': seed, 'targets_per_stratum': dict(sorted(targets.items())), 'selected': chosen}
    out['funnel']['selected'] = len(chosen)
    out['notes'] = out['notes'] + ['Selection is equal-probability among families within a stratum; families, not applications, are the sampling unit.']
    errs = frame_errors(out)
    if errs: raise ValueError('selected frame invalid: ' + '; '.join(errs[:5]))
    return out


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--out' not in args or len(args) < 6: sys.exit(__doc__)
    out_path = Path(args[args.index('--out') + 1]); args = args[:args.index('--out')]
    src, new_id, seed, pairs = paths.checked_or_exit(args[0], 'the source frame path'), args[1], int(args[2]), args[3:]
    try: paths.refuse_install_write(out_path); paths.check_frame_id(new_id)
    except paths.PathError as e: sys.exit(str(e))
    if out_path.exists(): sys.exit(f'refusing to overwrite {out_path}')
    targets = dict((p.split('=')[0], int(p.split('=')[1])) for p in pairs)
    frame = select(load_frame(src), new_id, seed, targets)
    dump_frame(frame, out_path)
    print(json.dumps({'status': 'SELECTED', 'frame_id': new_id, 'funnel': frame['funnel'],
                      'per_stratum': {s: sum(1 for c in frame['selection']['selected'] if c['stratum'] == s) for s in targets}}, indent=1))
