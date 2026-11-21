"""Schema v3 prosecution-record contracts: pages, extraction runs, event pairing,
rounds and claim versions. Structural only; nothing here establishes that a
pairing is correct, that OCR text is accurate, or that a round is legally complete."""
import re

PAGE_STATUSES = ('native_text', 'ocr_ok', 'ocr_low_confidence', 'empty', 'failed', 'not_attempted', 'excluded_with_reason')
USABLE_PAGE = ('native_text', 'ocr_ok', 'excluded_with_reason')
EVENT_KINDS = ('action', 'reply_component', 'interview', 'rce', 'restriction', 'election', 'appeal', 'allowance', 'grant', 'abandonment', 'status_observation', 'other')
PAIRING_TYPES = ('reply_identifies_action', 'date_consistency', 'examiner_reference', 'bundle_membership', 'analyst_inference')
CLAIM_STATUSES = ('pending', 'amended', 'cancelled', 'withdrawn', 'elected', 'nonelected', 'allowed', 'granted', 'new')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
HEX64 = re.compile('[0-9a-f]{64}')


def document_errors(d, err):
    """Per-document v3 rules. `err` collects messages. Returns the run id whose text is current, if any."""
    norm = d.get('normalization') if isinstance(d.get('normalization'), dict) else {}
    if 'quality' in norm and not isinstance(norm['quality'], list):
        err('normalization quality notes must be array')
    if d.get('lane') == 'public_exemplar' and not isinstance(d.get('application_id'), str):
        err('public document requires application_id')
    runs = d.get('extraction_runs', [])
    if not isinstance(runs, list):
        err('extraction_runs must be array'); runs = []
    run_ids = {}
    for r in runs:
        if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r.get(k) for k in ('run_id', 'extractor', 'version', 'text_sha256')):
            err('extraction run needs run_id, extractor, version, text_sha256'); continue
        if r['run_id'] in run_ids: err('duplicate extraction run id')
        run_ids[r['run_id']] = r
    for r in run_ids.values():
        sup = r.get('supersedes_run_id')
        if sup is not None and sup not in run_ids: err('supersedes_run_id refers to unknown run')
    current = None
    if d.get('status') == 'ready' and run_ids:
        matching = [r for r in run_ids.values() if r['text_sha256'] == d.get('sha256')]
        if not matching: err('ready document text hash matches no extraction run')
        else: current = matching[0]['run_id']
    pages = d.get('pages', [])
    if not isinstance(pages, list):
        err('pages must be array'); pages = []
    seen = set()
    for p in pages:
        if not isinstance(p, dict) or type(p.get('page')) is not int or p['page'] < 1: err('page record needs integer page >= 1'); continue
        if p['page'] in seen: err('duplicate page record')
        seen.add(p['page'])
        if p.get('status') not in PAGE_STATUSES: err('invalid page status')
        if p.get('status') == 'excluded_with_reason' and not p.get('reason'): err('excluded page needs reason')
        if p.get('run_id') is not None and p['run_id'] not in run_ids: err('page run_id refers to unknown extraction run')
    if d.get('status') == 'ready' and pages and any(p.get('status') not in USABLE_PAGE for p in pages if isinstance(p, dict)):
        err('ready document has pages that are not usable')
    for u in d.get('units', []) if isinstance(d.get('units'), list) else []:
        if not isinstance(u, dict): continue
        if current and u.get('run_id') != current: err('unit run_id must name the current extraction run')
        if u.get('page') is not None and (type(u['page']) is not int or u['page'] not in seen): err('unit page not in page records')
    return current


def _parse(s):
    return s[:10] if isinstance(s, str) and DATE.fullmatch(s[:10]) else None


def event_errors(events, doc_ids, err):
    by_id = {e.get('id'): e for e in events if isinstance(e, dict)}
    for e in events:
        if not isinstance(e, dict): continue
        if e.get('event_kind') is not None and e['event_kind'] not in EVENT_KINDS: err('invalid event_kind')
        for k in ('mailing_date', 'receipt_date'):
            if e.get(k) is not None and not _parse(e[k]): err('event ' + k + ' must be YYYY-MM-DD')
        target = e.get('responds_to')
        if target is None: continue
        t = by_id.get(target)
        if t is None: err('responds_to refers to unknown event'); continue
        if t.get('application_id') != e.get('application_id'): err('responds_to crosses applications')
        ev = e.get('pairing_evidence')
        if not isinstance(ev, list) or not ev: err('responds_to requires pairing_evidence'); continue
        for p in ev:
            if not isinstance(p, dict) or p.get('type') not in PAIRING_TYPES: err('invalid pairing evidence type'); continue
            if p.get('document_id') not in doc_ids: err('pairing evidence document unknown')
        if all(p.get('type') in ('date_consistency', 'analyst_inference') for p in ev if isinstance(p, dict)) and e.get('review_status') != 'reviewed':
            err('pairing by date/inference alone must be marked reviewed')
        m, r = _parse(t.get('mailing_date') or t.get('date')), _parse(e.get('receipt_date') or e.get('date'))
        if m and r and r < m: err('reply dated before the action it answers')


def round_errors(rounds, events, err):
    by_id = {e.get('id'): e for e in events if isinstance(e, dict)}
    seen = set()
    for r in rounds:
        if not isinstance(r, dict): err('round object required'); continue
        if not r.get('id') or r['id'] in seen: err('missing/duplicate round id')
        seen.add(r.get('id'))
        a = by_id.get(r.get('action_event'))
        if a is None: err('round action_event unknown'); continue
        replies = r.get('reply_events')
        if not isinstance(replies, list): err('round reply_events must be array'); replies = []
        for x in replies:
            e = by_id.get(x)
            if e is None or e.get('responds_to') != a.get('id'): err('round reply does not respond to its action')
        c = r.get('checklist')
        if not isinstance(c, dict) or not all(isinstance(c.get(k), list) for k in ('expected', 'present', 'missing')):
            err('round checklist needs expected/present/missing arrays'); continue
        if set(c['present']) | set(c['missing']) != set(c['expected']): err('round checklist present+missing must equal expected')
        if r.get('complete') is True and (c['missing'] or r.get('review_status') != 'reviewed'):
            err('round cannot be complete with missing items or without review')


def claim_version_errors(versions, doc_ids, events, err):
    ids = {}
    event_ids = {e.get('id') for e in events if isinstance(e, dict)}
    for v in versions:
        if not isinstance(v, dict): err('claim version object required'); continue
        vid = v.get('version_id')
        if not isinstance(vid, str) or vid in ids: err('missing/duplicate claim version id')
        ids[vid] = v
        if v.get('document_id') not in doc_ids: err('claim version document unknown')
        if not isinstance(v.get('application_id'), str): err('claim version application_id required')
        for ev in v.get('pending_at_event_ids', []) if isinstance(v.get('pending_at_event_ids'), list) else [None]:
            if ev not in event_ids: err('claim version pending_at event unknown')
        claims = v.get('claims')
        if not isinstance(claims, list) or not claims: err('claim version needs claims'); continue
        numbers = set()
        for c in claims:
            if not isinstance(c, dict) or type(c.get('number')) is not int: err('claim number must be integer'); continue
            if c['number'] in numbers: err('duplicate claim number in version')
            numbers.add(c['number'])
            if c.get('status') not in CLAIM_STATUSES: err('invalid claim version status')
            if c.get('text_sha256') is not None and not HEX64.fullmatch(str(c['text_sha256'])): err('claim text_sha256 malformed')
        for c in claims:
            if not isinstance(c, dict): continue
            deps = c.get('depends_on', [])
            if not isinstance(deps, list): err('claim depends_on must be array'); continue
            for dnum in deps:
                if dnum == c.get('number') or dnum not in numbers: err('claim depends on missing or self claim')
    for v in ids.values():
        sup = v.get('supersedes_version_id')
        if sup is not None and sup not in ids: err('supersedes_version_id unknown')
        rm = v.get('renumbering_map')
        if rm is not None and (not isinstance(rm, dict) or sup is None): err('renumbering_map requires supersedes_version_id and object form')
