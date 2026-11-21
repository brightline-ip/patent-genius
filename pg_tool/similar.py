"""The similar-office-action finder: deterministic, offline, read-only. It reads the database you built (python pg_cli.py db build)
and ranks the office actions in it by a published weighted sum of seven features.

  statute_set 0.35   Jaccard overlap of the statutes rejected (101, 102, 103, 112, dp)
  references  0.20   Jaccard overlap of cited references, on NORMALISED DOCUMENT NUMBERS ONLY (a surname is never matched)
  examiner    0.15   same examiner (the examiner NAMED ON THE APPLICATION RECORD NOW; examiners move)
  art_unit    0.10   1 same art unit, 0.5 same work group (first three digits)
  cpc         0.10   1 same main group, 0.5 same subclass
  ground_101  0.05   1 when both are 101 rejections flagged Alice or Mayo by the provider/parser
  finality    0.05   1 when both are final or both non-final
A feature that is missing on either side scores 0 and counts nothing towards `coverage`. Ranking is by coverage tier first
(full, high, partial, thin), then score, then a fixed tie-break. The weights come from pg_tool/similarity.json, optionally
overridden by <data folder>/similarity.json or a request; they are validated and echoed in every result.

The result also holds, for ALL matches at or above `min_score` (not only the listed ones), counts with 95% Wilson intervals:
  outcome_summary         where those cases ended up, one case per patent family, open cases reported separately
  applicant_action_labels what the applicant and the Office did next (interview, amendment, RCE/appeal): descriptions only,
                          kept apart from the outcomes, never cross-tabulated with them, and not about what any argument said
Similar by these features only: not semantic or legal similarity, and no prediction."""
import hashlib, json, math, re, sqlite3
from pathlib import Path
from . import stats as stats_mod, oa_outcomes

CONFIG = Path(__file__).with_name('similarity.json')
COMPONENTS = ('statute_set', 'references', 'examiner', 'art_unit', 'cpc', 'ground_101', 'finality')
STATUTES = ('101', '102', '103', '112', 'dp')
CAVEAT = 'Similar by these features only (statutes, references, examiner, art unit, classification, finality). This is not semantic or legal similarity, and it predicts nothing about your case.'
MIN_DB_VERSION = (2, 3)
EXAMINER_NOTE = 'Examiner as named on the application record now. Examiners change over an application\'s life, so this may not be the examiner who wrote the action.'
OUTCOME_NOTE = ("Where each matched family stood at the freeze date of your frames (see as_of): allowed (granted, or allowed and not yet granted), abandoned, or still open. "
                "Abandoned includes abandonment in favour of a continuation. The unit is one application per patent family. Open cases are right-censored: they are reported separately and never counted as either result.")
SHARE_NOTE = "Shares use resolved families only, so they favour applications that resolved quickly: a case that is still open may end differently. Treat a share as a description of resolved cases, not a rate for new ones."
ACTION_NOTE = ("What the applicant and the Office did next after the matched action, from file-wrapper events: descriptions of steps, not of what any argument said. They are not cross-tabulated with outcomes and show no cause. "
               "Transaction codes are checked against the USPTO PatEx 2022 list (pg_tool/data/uspto_event_codes.csv); which codes count as an interview, an abandonment and so on is this tool's own mapping (pg_tool/event_codes.json). "
               "Only actions anchored on their own mailing event (or, failing that, their own action event) are used. Interview and amendment shares count only actions whose next event is recognised, because for an action with none an interview or an amendment may still come.")


class SimilarError(Exception):
    def __init__(self, code, message): super().__init__(message); self.code = code


# ------------------------------------------------------------------ weights
def _load_default():
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def validate_weights(w, where):
    if not isinstance(w, dict) or not w: raise SimilarError('invalid_input', f'{where}: weights must be an object like {{"examiner": 0.2}}')
    for k, v in w.items():
        if k not in COMPONENTS: raise SimilarError('invalid_input', f'{where}: unknown weight {str(k)[:30]!r}; the weights are {", ".join(COMPONENTS)}')
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or v > 1:
            raise SimilarError('invalid_input', f'{where}: weight {k} must be a number from 0 to 1')
    return {k: float(v) for k, v in w.items()}


def effective_weights(data_dir=None, request_weights=None):
    """The weights in force and where they came from: the shipped defaults, then <data>/similarity.json, then the request. Echoed in results."""
    base = _load_default(); w = dict(base['weights']); source, overridden, sha = 'default', [], None
    if data_dir is not None:
        p = Path(data_dir) / 'similarity.json'
        if p.is_file():
            raw = p.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
            try: doc = json.loads(raw.decode('utf-8-sig'))
            except ValueError: raise SimilarError('invalid_input', f'{p.name} is not valid JSON')
            part = validate_weights(doc.get('weights') if isinstance(doc, dict) else None, p.name)
            w.update(part); overridden += sorted(part); source = str(p)
    if request_weights is not None:
        part = validate_weights(request_weights, 'request weights'); w.update(part); overridden += sorted(part); source = 'request' if source == 'default' else source + ' + request'
    if sum(w.values()) <= 0: raise SimilarError('invalid_input', 'at least one weight must be above 0')
    return {'version': base['version'], 'dated': base['dated'], 'basis': base['basis'], 'source': source, 'overridden': sorted(set(overridden)),
            'user_file_sha256': sha, 'weights': w, 'total': round(sum(w.values()), 6), 'tiers': base['tiers'], 'defaults': base['defaults']}


# ------------------------------------------------------------------ query features
def norm_examiner(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip().upper() or None


def norm_cpc(s):
    c = re.sub(r'\s+', '', str(s or '')).upper()
    if not re.match(r'^[A-HY]\d{2}[A-Z]', c): return None, None
    return c.split('/')[0], c[:4]


def features_from_analysis(analysis, header, facts, context=None):
    """Query features read from a parsed office action (rule-extracted, unverified), each with how it was found."""
    context = context or {}; feat, how = {}, {}
    rej = [r for r in analysis.get('rejections', []) if r.get('statute') != 'other' or r.get('statutory_subsection')]
    from . import oa_outcomes, oa_parse
    st = sorted({oa_outcomes.statute_level(r['statute']) for r in rej} - {None}, key=STATUTES.index)
    if st: feat['statutes'] = st; how['statutes'] = 'rejections recognised in the text (rule-based parser)'
    refs = sorted(oa_parse.matchable_numbers(analysis.get('references')))
    if refs: feat['references'] = refs; how['references'] = 'normalised document numbers of the references the parser found (name-only references are not used)'
    if header.get('examiner'): feat['examiner'] = norm_examiner(header['examiner']['value']); how['examiner'] = header['examiner']['how']
    if header.get('art_unit'): feat['art_unit'] = header['art_unit']['value']; how['art_unit'] = header['art_unit']['how']
    if facts.get('finality') in ('final', 'non_final'): feat['finality'] = facts['finality']; how['finality'] = facts.get('finality_basis')
    return feat, how


def clean_features(f, how, source):
    """Validate explicit features (from a request) and merge them: explicit values win."""
    out, prov = {}, {}
    if f.get('statutes') is not None:
        bad = [s for s in f['statutes'] if s not in STATUTES]
        if bad: raise SimilarError('invalid_input', f'features.statutes: use {", ".join(STATUTES)}')
        out['statutes'] = sorted(set(f['statutes']), key=STATUTES.index); prov['statutes'] = source
    if f.get('references') is not None:
        refs = sorted({str(r).upper() for r in f['references']})
        if any(not re.fullmatch(r'[A-Z]{2}[A-Z]{0,2}\d{4,14}', r) for r in refs): raise SimilarError('invalid_input', 'features.references: use normalised document numbers such as US10000002 or US20190000001')
        out['references'] = refs; prov['references'] = source
    for k in ('alice', 'mayo'):
        if f.get(k) is not None: out[k] = bool(f[k]); prov[k] = source
    if f.get('examiner'): out['examiner'] = norm_examiner(f['examiner']); prov['examiner'] = source
    if f.get('art_unit'): out['art_unit'] = str(f['art_unit']); prov['art_unit'] = source
    if f.get('cpc'): out['cpc'] = f['cpc']; prov['cpc'] = source
    if f.get('finality'): out['finality'] = f['finality']; prov['finality'] = source
    return out, prov


# ------------------------------------------------------------------ scoring
def jaccard(a, b):
    a, b = set(a), set(b); u = a | b
    return len(a & b) / len(u) if u else None


def score_candidate(q, c, weights):
    """Components for one candidate row: {name: {'available', 'value', 'weight', 'detail'}}. Missing on either side = unavailable = scores 0."""
    comp = {}
    def put(name, avail, value=0.0, detail=''):
        comp[name] = {'available': bool(avail), 'value': round(float(value), 4) if avail else 0.0, 'weight': weights[name], 'detail': detail}
    qs, cs = set(q.get('statutes') or []), set(filter(None, (c['statute_set'] or '').split(',')))
    put('statute_set', qs and cs, jaccard(qs, cs) or 0.0, f'query {sorted(qs)} vs candidate {sorted(cs)}')
    both101 = '101' in qs and '101' in cs
    qflag, cflag = bool(q.get('alice') or q.get('mayo')), bool(c['alice'] or c['mayo'])
    put('ground_101', both101, 1.0 if (qflag and cflag) else 0.0, f'Alice/Mayo flagged: query {qflag}, candidate {cflag}')
    qr = set(q.get('references') or [])
    put('references', qr and c['has_refs'], jaccard(qr, c['refs']) or 0.0, f'{len(qr & set(c["refs"]))} of {len(qr | set(c["refs"]))} document numbers shared' if c['has_refs'] else 'candidate has no parsed references')
    qe, ce = norm_examiner(q.get('examiner')), norm_examiner(c['examiner'])
    put('examiner', qe and ce, 1.0 if qe == ce else 0.0, 'same examiner on the application record now' if qe == ce else 'different examiner')
    qa, ca = q.get('art_unit'), c['art_unit']
    v = 1.0 if (qa and ca and qa == ca) else 0.5 if (qa and ca and qa[:3] == str(ca)[:3]) else 0.0
    put('art_unit', qa and ca, v, 'same art unit' if v == 1 else 'same work group' if v == .5 else 'different work group')
    qg, qsub = norm_cpc(q.get('cpc')); cg, csub = c['cpc_group'], c['cpc_subclass']
    v = 1.0 if (qg and cg and qg == cg) else 0.5 if (qsub and csub and qsub == csub) else 0.0
    put('cpc', (qg or qsub) and (cg or csub), v, 'same main group' if v == 1 else 'same subclass' if v == .5 else 'different subclass')
    qf = q.get('finality'); cf = None if c['is_final'] is None else ('final' if c['is_final'] else 'non_final')
    put('finality', qf and cf, 1.0 if qf == cf else 0.0, f'query {qf}, candidate {cf}')
    return comp


def tier_of(coverage, tiers):
    for t in tiers:
        if coverage >= t['min_coverage'] - 1e-9: return t['name']
    return tiers[-1]['name']


def wilson(k, n, z=1.96):
    if n == 0: return None
    p = k / n; d = 1 + z * z / n; centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def share(k, n, min_n):
    if n < min_n: return {'n': n, 'k': k, 'share': None, 'ci95': None, 'note': f'share not shown: fewer than {min_n} cases'}
    return {'n': n, 'k': k, 'share': round(k / n, 4), 'ci95': wilson(k, n)}


# ------------------------------------------------------------------ database
def open_ro(db):
    if not Path(db).is_file(): raise SimilarError('not_found', 'no database yet: build one with  python pg_cli.py db build  (it needs frames and collected office-action rows)')
    con = sqlite3.connect(f'file:{Path(db).resolve().as_posix()}?mode=ro', uri=True); con.execute('PRAGMA query_only=ON'); con.row_factory = sqlite3.Row
    try: v = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    except sqlite3.Error: v = None
    if not v or tuple(int(x) for x in re.findall(r'\d+', str(v[0]))[:3]) < MIN_DB_VERSION:
        con.close(); raise SimilarError('invalid_input', 'the database was built by an older version: rebuild it with  python pg_cli.py db build')
    return con


def outcome_class(o):
    return 'allowed' if o in ('granted', 'allowed_not_yet_granted') else 'abandoned' if o == 'abandoned' else 'open'


def find(db, q, *, weights, exclude_applications=(), filters=None, require=(), limit=20, min_score=0.5, include_outcomes=True, min_n=5, pool_cap=20000):
    """Rank office actions in the database against the query features `q`. Returns the result data dict (see module doc)."""
    filters = filters or {}
    min_n, floor_warning = stats_mod.clamp_min_n(min_n, 5)
    warnings = [floor_warning] if floor_warning else []
    con = open_ro(db)
    try:
        w = weights['weights']; tiers = weights['tiers']; total = sum(w.values())
        for r in require:
            if r not in COMPONENTS: raise SimilarError('invalid_input', f'require: unknown component {r!r}')
        fam = {}
        ex = {a for a in exclude_applications if a}
        keys = {r[0] for a in ex for r in con.execute('SELECT family_key FROM applications WHERE application=?', (a,)) if r[0]}
        excluded = set(ex)
        for k in keys: excluded |= {r[0] for r in con.execute('SELECT application FROM applications WHERE family_key=?', (k,))}
        clauses, params = ["o.doc_code IN ('CTNF','CTFR')", 'o.application NOT IN (SELECT application FROM skipped_applications WHERE application IS NOT NULL)'], {}
        qs = q.get('statutes') or []
        if qs: clauses.append('(' + ' OR '.join({'101': 'o.rej101', '102': 'o.rej102', '103': 'o.rej103', '112': 'o.rej112', 'dp': 'o.rejdp'}[s] + '=1' for s in qs) + ')')
        if filters.get('field'): clauses.append('EXISTS (SELECT 1 FROM memberships m WHERE m.application=o.application AND m.field=:field)'); params['field'] = filters['field']
        if filters.get('filing_year_from') is not None: clauses.append('a.filing_year >= :y0'); params['y0'] = filters['filing_year_from']
        if filters.get('filing_year_to') is not None: clauses.append('a.filing_year <= :y1'); params['y1'] = filters['filing_year_to']
        if filters.get('finality'): clauses.append('o.is_final = :fin'); params['fin'] = 1 if filters['finality'] == 'final' else 0
        sql = f"""SELECT o.application, o.oa_document, o.date, o.doc_code, o.art_unit, o.examiner, o.field, o.cpc_group, o.cpc_subclass, o.is_final, o.statute_set,
            o.rej101, o.alice, o.mayo, a.outcome, a.filing_year, a.family_key, a.status_text,
            oc.next_event, oc.next_event_date, oc.interview_before_next, oc.amendment_before_next, oc.rce_or_appeal_next, (oc.application IS NOT NULL) AS has_outcome,
            oc.anchor_quality, oc.next_event_state, oc.later_events,
            (SELECT MIN(f.frozen_at) FROM memberships m JOIN frames f ON f.frame_id=m.frame_id WHERE m.application=o.application) AS frozen_at,
            EXISTS (SELECT 1 FROM oa_links l JOIN oa_parsed p ON p.application=l.application AND p.oa_document=l.document_id
                    WHERE l.application=o.application AND l.oa_document=o.oa_document) AS has_refs,
            (SELECT group_concat(DISTINCT c.reference_normalized) FROM oa_links l JOIN oa_citations c ON c.application=l.application AND c.oa_document=l.document_id
                    WHERE l.application=o.application AND l.oa_document=o.oa_document AND c.reference_normalized IS NOT NULL) AS refs
          FROM office_actions o JOIN applications a ON a.application=o.application
          LEFT JOIN oa_outcomes oc ON oc.application=o.application AND oc.oa_document=o.oa_document
          WHERE {' AND '.join(clauses)} ORDER BY o.application, o.oa_document LIMIT {int(pool_cap) + 1}"""
        rows = con.execute(sql, params).fetchall()
        capped = len(rows) > pool_cap; rows = rows[:pool_cap]
        stats = {'office_actions_in_database': con.execute('SELECT COUNT(*) FROM office_actions').fetchone()[0],
                 'earliest_action_date': con.execute('SELECT MIN(date) FROM office_actions').fetchone()[0], 'latest_action_date': con.execute('SELECT MAX(date) FROM office_actions').fetchone()[0],
                 'skipped_applications_left_out': con.execute('SELECT COUNT(DISTINCT application) FROM skipped_applications WHERE application IS NOT NULL').fetchone()[0],
                 'frames_frozen': [con.execute('SELECT MIN(frozen_at) FROM frames').fetchone()[0], con.execute('SELECT MAX(frozen_at) FROM frames').fetchone()[0]],
                 'actions_with_parsed_references': con.execute('SELECT COUNT(*) FROM oa_links l JOIN oa_parsed p ON p.application=l.application AND p.oa_document=l.document_id').fetchone()[0]}
    finally: con.close()
    scored, in_pool = [], 0
    for r in rows:
        if r['application'] in excluded: continue
        in_pool += 1
        c = dict(r); c['refs'] = sorted(filter(None, (r['refs'] or '').split(','))); c['has_refs'] = bool(r['has_refs'])
        comp = score_candidate(q, c, w)
        if any(not comp[x]['available'] or comp[x]['value'] <= 0 for x in require): continue
        s = sum(v['weight'] * v['value'] for v in comp.values()) / total
        cov = sum(v['weight'] for v in comp.values() if v['available']) / total
        if s + 1e-9 < min_score: continue
        scored.append((c, comp, round(s, 4), round(cov, 4), tier_of(cov, tiers)))
    order = {t['name']: i for i, t in enumerate(tiers)}
    scored.sort(key=lambda t: (order[t[4]], -t[2], -t[3], t[0]['date'] or '', t[0]['application'], t[0]['oa_document'] or ''))
    # one case per family for the summaries: the best-scoring matched action of each family
    best = {}
    for t in scored:
        k = t[0]['family_key'] or ('app:' + t[0]['application'])
        if k not in best: best[k] = t
    reps = list(best.values())
    if capped: warnings.append(f'the candidate pool was capped at {pool_cap:,} office actions: the summaries below describe the capped pool, not every match. Narrow the query (filters) or raise the cap.')
    cls = [outcome_class(t[0]['outcome']) for t in reps]
    resolved = cls.count('allowed') + cls.count('abandoned')
    frozen = [str(t[0]['frozen_at'])[:10] for t in reps if t[0]['frozen_at']] or [str(x)[:10] for x in stats['frames_frozen'] if x]   # the freeze dates of the matched families' frames
    outcome_summary = {'matched_actions': len(scored), 'matched_families': len(reps), 'min_score': min_score, 'note': OUTCOME_NOTE, 'resolution_bias_note': SHARE_NOTE,
                       'as_of': max(frozen) if frozen else None, 'as_of_from': min(frozen) if frozen else None, 'as_of_to': max(frozen) if frozen else None, 'unit': 'one application per patent family',
                       'allowed': share(cls.count('allowed'), resolved, min_n), 'abandoned': share(cls.count('abandoned'), resolved, min_n),
                       'resolved_families': resolved, 'open_families': cls.count('open'), 'share_basis': 'share of resolved families (allowed + abandoned); open families are not in the denominator'}
    anchored = [t for t in reps if t[0]['has_outcome'] and t[0]['anchor_quality'] in oa_outcomes.ANCHORED]
    nexts = [t for t in anchored if t[0]['next_event']]
    count = lambda state: sum(1 for t in anchored if t[0]['next_event_state'] == state)
    labels = {'note': ACTION_NOTE, 'families_with_anchored_event_data': len(anchored), 'families_unanchored_or_without_event_data': len(reps) - len(anchored), 'min_n': min_n,
              'share_basis': 'actions whose next event is recognised (a later turning point is on record): for an action with none, an interview or an amendment may still come, so it is not in a denominator',
              'actions_with_a_recognised_next_event': len(nexts),
              'interview_before_next': share(sum(1 for t in nexts if t[0]['interview_before_next']), len(nexts), min_n),
              'amendment_before_next': share(sum(1 for t in nexts if t[0]['amendment_before_next']), len(nexts), min_n),
              'rce_or_appeal_next': share(sum(1 for t in nexts if t[0]['rce_or_appeal_next']), len(nexts), min_n),
              'no_later_event': count('no_later_event'), 'later_events_none_recognised': count('later_events_none_recognised'),
              'next_event_counts': dict(sorted(((e, sum(1 for t in nexts if t[0]['next_event'] == e)) for e in {t[0]['next_event'] for t in nexts}), key=lambda kv: (-kv[1], kv[0])))}
    out = []
    for c, comp, s, cov, tier in scored[:limit]:
        item = {'application': c['application'], 'oa_document': c['oa_document'], 'date': c['date'], 'document_code': c['doc_code'], 'field': c['field'], 'art_unit': c['art_unit'],
                'examiner': c['examiner'], 'examiner_label': 'examiner named on the application record now', 'statutes': sorted(filter(None, (c['statute_set'] or '').split(','))),
                'final': None if c['is_final'] is None else bool(c['is_final']), 'score': s, 'coverage': cov, 'tier': tier, 'components': comp}
        if include_outcomes:
            item['outcome'] = {'application_outcome': c['outcome'], 'class': outcome_class(c['outcome']), 'open': outcome_class(c['outcome']) == 'open', 'filing_year': c['filing_year'],
                               **({'next_event': c['next_event'], 'next_event_date': c['next_event_date'], 'interview_before_next': c['interview_before_next'],
                                   'amendment_before_next': c['amendment_before_next'], 'rce_or_appeal_next': c['rce_or_appeal_next'], 'anchor_quality': c['anchor_quality'],
                                   'next_event_state': c['next_event_state']} if (c['has_outcome'] and c['anchor_quality'] in oa_outcomes.ANCHORED) else {'event_data': 'not available or the action could not be anchored on its own event'})}
        out.append(item)
    coverage = {**stats, 'coverage_starts': stats['earliest_action_date'],
                'coverage_note': 'Office actions before the earliest date shown are not in your data, so a family\'s earlier history may be missing (left truncation).',
                'candidates_considered': in_pool, 'pool_capped_at': pool_cap if capped else None,
                'excluded_applications': len(excluded), 'examiner_note': EXAMINER_NOTE}
    return {'caveat': CAVEAT, 'weights': {k: weights[k] for k in ('version', 'dated', 'source', 'overridden', 'user_file_sha256', 'weights', 'total', 'basis')},
            'tiers': tiers, 'min_score': min_score, 'candidates': out, 'candidates_listed': len(out), 'candidates_matching': len(scored), 'outcome_summary': outcome_summary,
            'applicant_action_labels': labels, 'coverage': coverage, 'min_n': min_n, 'warnings': warnings}


def record_features(db, app):
    """What the database holds about one application that helps the match: art unit, examiner (as on the record now) and first CPC symbol."""
    con = open_ro(db)
    try: r = con.execute('SELECT art_unit, examiner, first_cpc FROM applications WHERE application=?', (app,)).fetchone()
    finally: con.close()
    return {'art_unit': r[0], 'examiner': norm_examiner(r[1]), 'cpc': r[2]} if r else {}
