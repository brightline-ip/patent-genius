"""Expand stratum seed symbols through the official CPC Title List hierarchy.
Title List lines: symbol \\t level \\t title. Subclass lines have an empty level; main groups are
level 0; subgroups carry their dot depth. A seed's descendants are the following lines of the
same subclass whose level is deeper than the seed's, up to the next line at the same or shallower
level. A subclass seed expands to every group in that subclass.
Usage: python cpc_expand.py <CPCTitleList.zip> <version> <source_url> <seeds.json | scope.json> <out.json>
The seeds file is either {"field": [seeds]} / {"field": {"seeds": [...], "historical_prefixes": [...]}} (see examples/seeds.example.json)
or a scope.json, whose fields are used. Download the CPC Title List yourself (see docs/FRAMES.md); this program never downloads it."""
import hashlib, json, re, sys, zipfile
from datetime import datetime, timezone
from pathlib import Path
from pg_tool import paths, scope as scope_mod

SUBCLASS = re.compile(r'[A-HY]\d{2}[A-Z]$')
GROUP = re.compile(r'([A-HY]\d{2}[A-Z])\d{1,4}/\d+$')


def parse(lines):
    """-> {subclass: [(symbol, level), ...]} in scheme order (groups only)."""
    out = {}
    for line in lines:
        parts = line.rstrip('\r\n').split('\t')
        if len(parts) < 2: continue
        sym, lvl = parts[0].strip(), parts[1].strip()
        m = GROUP.match(sym)
        if m and lvl.isdigit(): out.setdefault(m.group(1), []).append((sym, int(lvl)))
    return out


def expand(scheme, seed):
    if SUBCLASS.match(seed):
        if seed not in scheme: raise KeyError(f'subclass {seed} not in scheme')
        return [s for s, _ in scheme[seed]]
    m = GROUP.match(seed)
    if not m or m.group(1) not in scheme: raise KeyError(f'symbol {seed} not in scheme')
    rows = scheme[m.group(1)]
    idx = next((i for i, (s, _) in enumerate(rows) if s == seed), None)
    if idx is None: raise KeyError(f'symbol {seed} not in scheme')
    level, out = rows[idx][1], [seed]
    for s, l in rows[idx + 1:]:
        if l <= level: break
        out.append(s)
    return out


def build(zip_path, version, url, seeds):
    raw = Path(zip_path).read_bytes()
    with zipfile.ZipFile(zip_path) as z:
        need = {s[0] for spec in seeds.values() for s in (spec if isinstance(spec, list) else spec['seeds'] + spec.get('historical_prefixes', []))}
        lines = []
        for n in z.namelist():
            m = re.match(r'cpc-section-([A-HY])_', n)
            if m and m.group(1) in need: lines += z.read(n).decode('utf-8').splitlines()
    scheme = parse(lines)
    strata = {}
    for name, spec in seeds.items():
        # spec: [seeds] or {"seeds": [...], "historical_prefixes": [...]} for symbols retired from the current scheme
        ss, hist = (spec, []) if isinstance(spec, list) else (spec['seeds'], spec.get('historical_prefixes', []))
        for h in hist:
            if h in scheme: raise ValueError(f'{h} is still in the {version} scheme; expand it as a seed instead')
        per_seed = {s: expand(scheme, s) for s in ss}
        symbols = sorted({x for v in per_seed.values() for x in v})
        strata[name] = {'seeds': ss, 'historical_prefixes': hist, 'symbol_count': len(symbols), 'per_seed_counts': {s: len(v) for s, v in per_seed.items()}, 'symbols': symbols}
    return {'cpc_version': version, 'source': {'url': url, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'expanded_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'note': 'archive deleted after expansion (docs/RETENTION.md); re-download and compare sha256'},
            'rule': __doc__.split('Usage')[0].strip(), 'strata': strata}


if __name__ == '__main__':
    zp, version, url, seeds_path, out = sys.argv[1:6]
    zp, seeds_path = paths.checked_or_exit(zp, 'the CPC zip path'), paths.checked_or_exit(seeds_path, 'the seeds path')
    out = paths.checked_or_exit(out, 'the output path')
    try: paths.refuse_install_write(out)
    except paths.PathError as e: sys.exit(str(e))
    if out.exists(): sys.exit(f'refusing to overwrite {out}')
    seeds = json.loads(Path(seeds_path).read_text(encoding='utf-8-sig'))
    if isinstance(seeds, dict) and 'fields' in seeds:
        errs = scope_mod.problems(seeds)
        if errs: sys.exit('scope is not valid:\n  ' + '\n  '.join(errs))
        seeds = scope_mod.seeds_from_scope(seeds)
    for name in seeds:
        try: paths.check_field(name)
        except paths.PathError as e: sys.exit(f'{name}: {e}')
    res = build(zp, version, url, seeds)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k: {'symbols': v['symbol_count'], 'per_seed': v['per_seed_counts']} for k, v in res['strata'].items()}, indent=1))
