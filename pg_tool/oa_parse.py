"""Deterministic office-action parser. Input: office-action text. Output: a dict shaped by
schemas/office-action-analysis.schema.json (rule_extracted, never human-verified) plus parser annotations.

What it recognises: USPTO form-paragraph sentences ("Claim(s) ... is/are rejected under 35 U.S.C. ...", "... objected
to ...", "... allowed"), the PTOL-326 summary lines ("Claim(s) 1-20 is/are rejected"), section headings
("Claim Rejections - 35 USC 103", the same heading rule used to label sample rounds), finality and AIA statements, and the
shortened statutory period as written. It does not compute deadlines, judge merit, resolve references or read
examiner reasoning. Every finding keeps character offsets into the input text. Rejection sentences inside
"Response to Arguments" or containing "withdrawn" are reported as annotations, not as live grounds.

Version 1.1 adds (a) header facts (art unit, examiner, mail date, application number), each with HOW it was found and where,
and (b) reference normalisation to document numbers: a citation with a patent or publication number becomes a normalised
number (US10000002, US20190000001, WO2018000003); a citation that is only a name stays as written and is never used for
matching. Header facts and references read from caller text are returned to the caller and used for nothing else here: no
provider or network query and no write is made from them (local read-only database queries only)."""
import datetime, hashlib, re

from . import untrusted

PARSER = 'pg_tool.oa_parse'
VERSION = '1.1.0'

_SP = r'[\s ]+'
_CLAIMS = r'(?P<claims>Claims?(?:\(s\))?' + _SP + r'\d(?:[\d\s,;&\-–—]|\band\b|\bor\b|\bto\b|\bthrough\b)*?)(?:' + _SP + r'of' + _SP + r'the' + _SP + r'(?:instant|present|subject|current)' + _SP + r'application)?'
REJ = re.compile(_CLAIMS + r'\s*' + r'(?:is|are|stands?|remains?|is/are)' + _SP + r'(?:also\s+|further\s+|again\s+|now\s+)?(?P<prov>provisionally\s+)?rejected\s+(?P<rest>(?:under|on\s+the\s+ground)[\s\S]{0,700}?)(?:(?<!\bal)(?<!\b[A-Z])(?<!\bNo)(?<!\bPat)(?<!\bPub)(?<!\bAppl)(?=\.\s*(?:\n|$)|\.\s+(?=[A-Z\[]))|$)', re.I)
OBJ = re.compile(_CLAIMS + r'\s*' + r'(?:is|are|is/are)' + _SP + r'objected\s+to\b(?P<rest>[\s\S]{0,400}?)(?:(?<!\bal)(?<!\b[A-Z])(?<!\bNo)(?<!\bPat)(?<!\bPub)(?<!\bAppl)(?=\.\s*(?:\n|$)|\.\s+(?=[A-Z\[]))|$)', re.I)
ALLOWED = re.compile(_CLAIMS + r'\s*' + r'(?:is|are)' + _SP + r'allowed\b', re.I)
ALLOWABLE = re.compile(_CLAIMS + r'\s*' + r'(?:is|are|would\s+be|would\s+also\s+be)' + _SP + r'allowable\s+(?P<cond>(?:if|upon|once|when)[^.]{0,250})', re.I)
ELECTION_ACK = re.compile(r'election[^.]{0,250}?(?:is|are|was|were)\s+(?:hereby\s+)?acknowledged', re.I)
SUMMARY = re.compile(r'Claim\(s\)' + _SP + r'(?P<claims>[\d][\d\s,\-–and]*?)' + _SP + r'is/are' + _SP + r'(?P<what>pending in the application|rejected|objected to|allowed|withdrawn from consideration)', re.I)
OTHER_OBJ = [('drawing', re.compile(r'\bThe\s+drawings?\s+(?:are|is)\s+objected\s+to\b|\bcorrected\s+drawings?\b[^.]{0,120}?\bare\s+required\b', re.I)),
             ('specification', re.compile(r'\bThe\s+(?:specification|disclosure)\s+is\s+objected\s+to\b', re.I)),
             ('abstract', re.compile(r'\bThe\s+abstract[^.]{0,80}?objected\s+to\b', re.I)),
             ('title', re.compile(r'\bThe\s+title\s+of\s+the\s+invention\s+is\s+not\s+descriptive\b', re.I)),
             ('oath_declaration', re.compile(r'\b(?:oath|declaration)\s+is\s+(?:defective|objected\s+to)\b', re.I))]
RESTRICT = [('restriction', re.compile(r'Restriction\s+to\s+one\s+of\s+the\s+following\s+inventions\s+is\s+required', re.I)),
            ('election_of_species', re.compile(r'(?:patentably\s+distinct\s+species|election\s+of\s+species\s+(?:is\s+)?required|requires?\s+an?\s+election\s+of\s+(?:a\s+)?(?:single\s+)?species)', re.I))]
GROUP = re.compile(r'^[ \t]*(?:Group[ \t]+)?(?P<label>[IVX]{1,5})\.?,?[ \t]+Claims?[ \t]+(?P<claims>[\d][\d \t,\-–and]*?),?[ \t]+(?:is\s+|are\s+)?drawn[ \t]+to[ \t]+(?P<desc>[^\n]{0,200})', re.I | re.M)
FINAL = re.compile(r'THIS\s+ACTION\s+IS\s+MADE\s+FINAL', re.I)
NONFINAL = re.compile(r'\bThis\s+(?:action|Office\s+action)\s+is\s+(?:made\s+)?non-?\s?final\b', re.I)
AIA = re.compile(r'examined\s+under\s+the\s+first\s+inventor\s+to\s+file\s+provisions\s+of\s+the\s+AIA', re.I)
PREAIA = re.compile(r'examined\s+under\s+the\s+pre-AIA\s+first\s+to\s+invent\s+provisions', re.I)
PERIOD = re.compile(r'SHORTENED\s+STATUTORY\s+PERIOD\s+FOR\s+REPLY[^.]{0,80}?SET\s+TO\s+EXPIRE\s+(?P<n>\d|ONE|TWO|THREE|FOUR|FIVE|SIX)\s*\(?\d?\)?\s+MONTHS?', re.I)
WORDS = {'ONE': 1, 'TWO': 2, 'THREE': 3, 'FOUR': 4, 'FIVE': 5, 'SIX': 6}
HEAD = re.compile(r'^[ \t]*(?:#+[ \t]*)?(?P<h>Claim Rejections?[^\n]{0,60}|Response to Arguments?|Allowable Subject Matter|Double Patenting|Claim Objections?|Specification|Drawings|Election/Restrictions?|Conclusion)[ \t]*$', re.I | re.M)
STATUTE = re.compile(r'35' + _SP + r'U\.?\s?S\.?\s?C\.?' + r'[\s §�&?]*' + r'(?P<s>10[123]|112)(?P<sub>(?:\s?\([a-f]\))?(?:\s?\(\d\))?)(?P<pre>[^.;]{0,40}?(?:first|second|fourth|sixth)\s+paragraph)?', re.I)
PRE_AIA_SUB = {'first': 'a', 'second': 'b', 'fourth': 'd', 'sixth': 'f'}


def expand_claims(s):
    """'1-4, 6-8, 11 and 13' -> [1, 2, 3, 4, 6, 7, 8, 11, 13]. Caps ranges at 500 claims."""
    s = re.sub(r'(?i)claims?(?:\(s\))?', ' ', s or '')
    out = set()
    for m in re.finditer(r'(\d+)\s*(?:[-–—]|through|to)\s*(\d+)|(\d+)', s):
        if m.group(3): out.add(int(m.group(3)))
        else:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b and b - a < 500: out.update(range(a, b + 1))
    return sorted(x for x in out if 1 <= x <= 1000)


def _section_at(heads, pos):
    cur = None
    for start, name in heads:
        if start > pos: break
        cur = name
    return cur


def _ref_kind(s):
    if re.search(r'\b(?:WO|EP|JP|CN|KR|DE)\s?[\d/]', s): return 'foreign'
    if re.search(r'\b(?:19|20)\d{2}\s*/\s*\d{6,7}\b', s): return 'us_publication'
    if re.search(r'\bU\.?S\.?\s*(?:Pat(?:ent)?\.?\s*(?:No\.?)?\s*)?\d{1,2},?\d{3},?\d{3}\b|\b\d{1,2},\d{3},\d{3}\b', s): return 'us_patent'
    if re.search(r'\b(?:WO|EP|JP|CN|KR|DE)\s?[\d/]', s): return 'foreign'
    if re.search(r'admitted\s+prior\s+art|\bAAPA\b', s, re.I): return 'applicant_admitted_prior_art'
    return 'unclear'


def _refs(rest):
    m = re.search(r'(?:anticipated\s+by|unpatentable\s+over|obvious\s+over)\s+(?P<r>[\s\S]+)$', rest, re.I)
    if not m: return []
    parts = re.split(r',?\s+(?:and\s+)?(?:further\s+)?in\s+view\s+of\s+|,?\s+as\s+evidenced\s+by\s+|;\s+', m.group('r'))
    cleaned = (untrusted.clean_line(re.sub(r'\s+', ' ', p).strip(' ,.'), 160) for p in parts if p.strip(' ,.'))   # control, bidi and invisible characters never reach an id
    return [c for c in cleaned if c][:8]


def _classify(rest):
    """-> (statute enum, subsection text or None, ground, pattern_id)."""
    low = re.sub(r'\s+', ' ', rest.lower())
    if 'double patenting' in low or 'obviousness-type' in low:
        nonstat = 'nonstatutory' in low or 'obviousness-type' in low
        return ('double_patenting_nonstatutory' if nonstat else 'double_patenting_statutory', None, 'double_patenting', 'dp')
    m = STATUTE.search(rest)
    if not m: return ('other', None, None, 'unknown_basis')
    s, sub = m.group('s'), re.sub(r'\s', '', m.group('sub') or '')
    if s == '101' and 'same invention' in low: return ('double_patenting_statutory', None, 'double_patenting', 'dp_101')
    if s == '112':
        letter = (re.match(r'\(([a-f])\)', sub) or [None, None])[1]
        if not letter and m.group('pre'): letter = PRE_AIA_SUB[re.search(r'(first|second|fourth|sixth)', m.group('pre'), re.I).group(1).lower()]
        if not letter:
            letter = 'b' if 'indefinite' in low else 'a' if ('written description' in low or 'enabl' in low or 'new matter' in low) else 'd' if ('dependent form' in low or 'further limit' in low) else None
        ground = ('indefinite' if letter == 'b' else 'written_description' if letter == 'a' and 'written description' in low else
                  'enablement' if letter == 'a' and 'enabl' in low else 'improper_dependency' if letter == 'd' else 'other')
        return (f'112({letter})' if letter else 'other', f'112({letter})' if letter else '112 (subsection not stated)', ground, f'112{letter or "?"}')
    if s == '101': return ('101', '101', 'judicial_exception' if re.search(r'abstract idea|judicial exception|law of nature|natural phenomenon|mental process', low) else 'statutory_category_or_unstated', '101')
    if s == '102':
        subtxt = sub or ('pre-AIA ' + re.search(r'102\s*\(([a-g])\)', rest).group(1) if re.search(r'102\s*\(([a-g])\)', rest) else '')
        return ('102', f'102{sub}' if sub else (subtxt or None), 'anticipated', '102')
    return ('103', f'103{sub}' if sub else None, 'obvious', '103')


# ---------------------------------------------------------------- header facts
_AU = re.compile(r'\bArt\s+Unit\b[ \t]*[:#]?[ \t]*(?P<v>\d{4})\b', re.I)
_APPNO = re.compile(r'\b(?:Application|Appl\.?|Serial)(?:/Control)?[ \t]*(?:No\.?|Number|#)[ \t]*[:.]?[ \t]*(?P<v>\d{2}[ \t]*/[ \t]*\d{3}[ \t]*,?[ \t]*\d{3}|\d{8})\b', re.I)
_MAIL = re.compile(r'\b(?P<label>Notification\s+Date|Date\s+Mailed|Mail(?:ing)?\s+Date|Date\s+of\s+Mailing|Mailed)\b[ \t]*[:.]?[ \t]*'
                   r'(?P<v>\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.?[ \t]+\d{1,2},?[ \t]+\d{4})', re.I)
_NAME = r"[A-Za-z][A-Za-z.'\- ]{1,48}?(?:,[ \t]*[A-Za-z][A-Za-z.'\- ]{0,30}?)?"
_EXAMINER_LABEL = re.compile(r'(?<![A-Za-z])Examiner(?:[ \t]+Name)?[ \t]*:[ \t]*(?P<v>' + _NAME + r')(?=[ \t]{2,}|[ \t]*\(|[ \t]*[\r\n]|[ \t]*$|[ \t]+(?:Art[ \t]+Unit|Notification|Date|Application)\b)', re.I | re.M)
_SIGNATURE = re.compile(r"/(?P<v>[A-Za-z][A-Za-z.'\-, ]{2,48}?)/[ \t]*\r?\n?[ \t]*(?P<role>(?:Primary|Supervisory|Assistant)?[ \t]*(?:Patent[ \t]+)?Examiner)\b", re.I)
HEADER_SCAN = 8000   # label patterns are looked for in this many characters at the start; signature blocks anywhere
_MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}


def _iso_date(s):
    """'01/15/2026' | '2026-01-15' | 'January 15, 2026' -> '2026-01-15', or None when it is not a real date."""
    s = (s or '').strip()
    try:
        m = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
        if m: return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        m = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})', s)
        if m: return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = re.fullmatch(r'([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})', s)
        if m and m.group(1)[:3].lower() in _MONTHS: return datetime.date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))).isoformat()
    except ValueError: return None
    return None


def extract_header(text):
    """Header facts of an office action, each with how it was found: {'art_unit': {...}, 'examiner': {...}, 'mail_date': {...},
    'application_number': {...}}. A fact that is not found is absent. When the text gives several different values the first is
    returned and the others are listed under 'other_values'. Nothing here is verified against any record."""
    text = text or ''
    head = text[:HEADER_SCAN]
    out = {}

    def add(found, value, how, span): found.append({'value': value, 'how': how, 'offsets': [span[0], span[1]]})

    def finish(name, found):
        if not found: return
        f = dict(found[0]); others = []
        for x in found[1:]:
            if x['value'] != f['value'] and x['value'] not in others: others.append(x['value'])
        if others: f['other_values'] = others[:5]
        out[name] = f

    found = []
    for m in _AU.finditer(head): add(found, m.group('v'), 'label "Art Unit" in the first %d characters' % HEADER_SCAN, m.span('v'))
    for m in _SIGNATURE.finditer(text):
        am = _AU.search(text, m.end(), m.end() + 80)
        if am and not re.match(r'(?i)supervisory', m.group('role')): add(found, am.group('v'), 'signature block ("Examiner, Art Unit")', am.span('v'))
    finish('art_unit', found)

    found = []
    for m in _APPNO.finditer(head):
        digits = re.sub(r'\D', '', m.group('v'))
        if len(digits) == 8: add(found, digits, 'label "Application No." in the first %d characters' % HEADER_SCAN, m.span('v'))
    finish('application_number', found)

    found = []
    for m in _MAIL.finditer(head):
        iso = _iso_date(m.group('v'))
        if iso: add(found, iso, 'label "%s" in the first %d characters' % (re.sub(r'\s+', ' ', m.group('label')).title(), HEADER_SCAN), m.span('v'))
    finish('mail_date', found)

    found = []
    for m in _EXAMINER_LABEL.finditer(head):
        if re.search(r'(?i)supervisory\s+(?:patent\s+)?$', head[max(0, m.start() - 24):m.start()]): continue
        name = re.sub(r'\s+', ' ', m.group('v')).strip(' ,.')
        if len(name) >= 3 and not re.fullmatch(r'(?i)name|none|unknown|n/a', name): add(found, name, 'label "Examiner:" in the first %d characters' % HEADER_SCAN, m.span('v'))
    for m in _SIGNATURE.finditer(text):
        if re.match(r'(?i)supervisory|assistant', m.group('role')): continue
        add(found, re.sub(r'\s+', ' ', m.group('v')).strip(' ,.'), 'signature block ("/NAME/ Examiner")', m.span('v'))
    finish('examiner', found)
    return out


# ---------------------------------------------------------------- reference normalisation
_US_PUB = re.compile(r'\b(?:U\.?S\.?[ \t]*)?(?P<y>(?:19|20)\d{2})[ \t]*/[ \t]*(?P<n>\d{7})\b', re.I)
_US_PUB_COMPACT = re.compile(r'\bU\.?S\.?[ \t-]*(?P<y>20\d{2})(?P<n>\d{7})\b', re.I)
_US_PAT = re.compile(r'\bU\.?S\.?[ \t]*(?:Pat(?:ent)?\.?[ \t]*)?(?:No\.?[ \t]*)?(?P<pre>RE|D|PP)?[ \t]*(?P<n>\d{1,2}(?:,\d{3}){2}|\d{7,8}|\d{1,2},?\d{3})\b', re.I)
_US_PAT_BARE = re.compile(r'(?<![\d/,.])(?P<n>\d{1,2},\d{3},\d{3})(?![\d,/])')
_FOREIGN = re.compile(r'\b(?P<cc>WO|EP|JP|CN|KR|DE|GB|FR|CA|TW|RU|AU)[ \t-]*(?P<n>\d[\d \t/.\-,]{3,22}\d)(?![\d/])', re.I)


def normalize_reference(citation):
    """Normalise one cited reference to a document number so references can be compared.
    Returns {'document_number': 'US10000002' | None, 'country': ..., 'kind': ..., 'how': ..., 'matchable': bool}.
    Rules: country code + digits only, no punctuation, no kind code (A1, B2). A US publication is US + year + 7 digits.
    A citation with no number in it (a surname such as 'Smith') is returned as matchable False with document_number None:
    it is kept as written and must never be used for matching."""
    s = re.sub(r'\s+', ' ', citation or '').strip()
    m = _US_PUB.search(s) or _US_PUB_COMPACT.search(s)
    if m: return {'document_number': 'US' + m.group('y') + m.group('n'), 'country': 'US', 'kind': 'us_publication', 'how': 'US publication number (year/serial)', 'matchable': True}
    f = _FOREIGN.search(s)
    if f:
        digits = re.sub(r'\D', '', f.group('n'))
        if len(digits) >= 5: return {'document_number': f.group('cc').upper() + digits, 'country': f.group('cc').upper(), 'kind': 'foreign', 'how': f'{f.group("cc").upper()} document number', 'matchable': True}
    m = _US_PAT.search(s)
    if m:
        digits, pre = re.sub(r'\D', '', m.group('n')), (m.group('pre') or '').upper()
        if (pre == 'RE' and 4 <= len(digits) <= 6) or (not pre and len(digits) in (7, 8)) or (pre in ('D', 'PP') and 5 <= len(digits) <= 7):
            return {'document_number': 'US' + pre + digits, 'country': 'US', 'kind': 'us_patent', 'how': 'US patent number', 'matchable': True}
    m = _US_PAT_BARE.search(s)
    if m: return {'document_number': 'US' + re.sub(r'\D', '', m.group('n')), 'country': 'US', 'kind': 'us_patent', 'how': 'bare US patent number (n,nnn,nnn)', 'matchable': True}
    kind = 'applicant_admitted_prior_art' if re.search(r'admitted\s+prior\s+art|\bAAPA\b', s, re.I) else 'unclear'
    return {'document_number': None, 'country': None, 'kind': kind, 'how': 'no document number in the citation; name only (kept as written, never used for matching)', 'matchable': False}


def matchable_numbers(references):
    """The set of normalised document numbers in a list of analysis references. Name-only references contribute nothing."""
    return {r['document_number'] for r in references or [] if r.get('document_number') and r.get('match_basis') == 'document_number'}


def parse(text, *, document_id=None, application_number=None, document_code=None, mail_date=None, ocr=None, now=None):
    text = text or ''
    heads = [(m.start(), m.group('h').strip()) for m in HEAD.finditer(text)]
    rejections, objections, refs, ann, ignored = [], [], {}, {}, []
    seen = {}

    def ref_ids(strings):
        ids = []
        for i, s in enumerate(strings):
            rid = (re.match(r"[A-Za-z][A-Za-z'\-]+", s) or [s[:20]])[0]
            if rid in refs and refs[rid]['citation'] != s: rid = f'{rid}-{len(refs) + 1}'
            nr = normalize_reference(s)
            refs.setdefault(rid, {'id': rid, 'citation': s, 'kind': _ref_kind(s), 'role': 'primary' if i == 0 else 'secondary',
                                  'match_basis': 'document_number' if nr['matchable'] else 'name_only', **({'document_number': nr['document_number']} if nr['matchable'] else {})})
            ids.append(rid)
        return ids

    for m in REJ.finditer(text):
        section = _section_at(heads, m.start()) or ''
        sentence = m.group(0)
        statute, subsection, ground, pid = _classify(m.group('rest'))
        claims = expand_claims(m.group('claims'))
        status = 'withdrawn' if re.search(r'\bwithdrawn\b', sentence, re.I) else 'recap' if re.match(r'Response to Arguments?', section, re.I) else 'maintained_or_new'
        if status != 'maintained_or_new':
            ignored.append({'offsets': [m.start(), m.end()], 'statute': statute, 'claims': claims, 'reason': status})
            continue
        refstr = _refs(m.group('rest')) if statute in ('102', '103') else []
        key = (statute, tuple(claims), tuple(refstr))
        if key in seen: ann[seen[key]]['repeats'] += 1; continue
        rid = f'R{len(rejections) + 1}'; seen[key] = rid
        r = {'id': rid, 'statute': statute, 'claims': claims, 'locator': {'char_start': m.start(), 'char_end': m.end()}}
        if document_id: r['locator']['document_id'] = document_id
        if subsection: r['statutory_subsection'] = subsection
        if refstr: r['reference_ids'] = ref_ids(refstr)
        if m.group('prov') and statute.startswith('double'): r['provisional'] = True
        if statute.startswith('double'):
            cm = re.search(r'(?:over|of)\s+(?:claims?\s+[\d,\-\s and]+\s+of\s+)?(?:(?:U\.?S\.?\s+)?(?:Patent|Application)(?:\s+No\.?)?\s+)([\d/,]{6,})', m.group('rest'), re.I)
            if cm: r['conflicting_application_or_patent'] = cm.group(1).strip(',')
        rejections.append(r)
        ann[rid] = {'pattern_id': pid, 'basis': 'form_paragraph', 'confidence': 'pattern' if statute != 'other' else 'ambiguous', 'ground': ground,
                    'status_hint': 'maintained_or_new', 'section': section or None, 'claims_text': re.sub(r'\s+', ' ', m.group('claims'))[:120], 'repeats': 0}

    asm_claims = set()
    for m in OBJ.finditer(text):
        rest = m.group('rest'); claims = expand_claims(m.group('claims')); oid = f'O{len(objections) + 1}'
        allowable = bool(re.search(r'would\s+be\s+allowable\s+if\s+rewritten', rest, re.I))
        o = {'id': oid, 'kind': 'claim', 'claims': claims, 'locator': {'char_start': m.start(), 'char_end': m.end()}}
        if allowable:
            o['allowable_if_rewritten'] = True; o['categories'] = ['dependent_on_rejected_claim_allowable']; asm_claims.update(claims)
        elif re.search(r'informalit', rest, re.I): o['categories'] = ['informality_typo_grammar']
        elif re.search(r'1\.75\(c\)', rest): o['categories'] = ['improper_dependent_claim_1_75c']; o['basis'] = '37 CFR 1.75(c)'
        objections.append(o)
        ann[oid] = {'pattern_id': 'objection_claim' + ('_allowable' if allowable else ''), 'basis': 'form_paragraph', 'confidence': 'pattern', 'status_hint': 'maintained_or_new', 'section': _section_at(heads, m.start())}
    for kind, rx in OTHER_OBJ:
        m = rx.search(text)
        if m:
            oid = f'O{len(objections) + 1}'
            objections.append({'id': oid, 'kind': kind, 'locator': {'char_start': m.start(), 'char_end': m.end()}})
            ann[oid] = {'pattern_id': f'objection_{kind}', 'basis': 'phrase', 'confidence': 'pattern', 'status_hint': 'maintained_or_new', 'section': _section_at(heads, m.start())}

    asm_condition = 'rewrite_in_independent_form' if asm_claims else None
    for m in ALLOWABLE.finditer(text):
        cond = m.group('cond').lower()
        if 'rewritten in independent form' in cond and not re.search(r'overcome|112|101|reject', cond): continue  # already counted via the objection
        asm_claims.update(expand_claims(m.group('claims')))
        asm_condition = asm_condition or ('overcome_112_and_rewrite' if '112' in cond else 'other')
        ann.setdefault('allowable_subject_matter', {'pattern_id': 'allowable_if', 'basis': 'form_paragraph', 'confidence': 'pattern', 'offsets': [m.start(), m.end()]})
    allowed = sorted({c for m in ALLOWED.finditer(text) for c in expand_claims(m.group('claims'))})
    summary = {}
    for m in SUMMARY.finditer(text):
        k = {'pending in the application': 'pending', 'rejected': 'rejected', 'objected to': 'objected_to', 'allowed': 'allowed', 'withdrawn from consideration': 'withdrawn'}[re.sub(r'\s+', ' ', m.group('what').lower())]
        summary.setdefault(k, set()).update(expand_claims(m.group('claims')))
    if allowed: summary.setdefault('allowed', set()).update(allowed)

    restriction = None
    for kind, rx in RESTRICT:
        m = rx.search(text)
        if m:
            restriction = {'kind': 'both' if restriction else kind}
            ann['restriction'] = {'pattern_id': f'restriction_{kind}', 'basis': 'phrase', 'confidence': 'pattern', 'offsets': [m.start(), m.end()]}
    if restriction:
        groups = [{'label': g.group('label'), 'claims': expand_claims(g.group('claims')), 'description': re.sub(r'\s+', ' ', g.group('desc')).strip()[:200]} for g in GROUP.finditer(text)]
        if groups: restriction['groups'] = groups

    # Headings with no matching finding: report them; a rejection heading for a statute with no pattern match becomes heading_only.
    found_statutes = {r['statute'][:3] for r in rejections}
    headings_without = []
    for start, h in heads:
        hl = h.lower()
        if hl.startswith('claim rejection'):
            sm = re.search(r'\b(10[123]|112)\b', h)
            s = sm.group(1) if sm else None
            if s and s not in found_statutes:
                rid = f'R{len(rejections) + 1}'
                rejections.append({'id': rid, 'statute': s if s != '112' else 'other', 'claims': [], 'locator': {'char_start': start, 'char_end': start + len(h)}})
                if s == '112': rejections[-1]['statutory_subsection'] = '112 (subsection not determined)'
                ann[rid] = {'pattern_id': 'heading', 'basis': 'heading_only', 'confidence': 'heading_only', 'status_hint': 'unknown', 'section': h, 'ground': None}
                found_statutes.add(s)
            elif not s: headings_without.append(h)
        elif hl.startswith('double patenting') and not any(r['statute'].startswith('double') for r in rejections):
            near = text[start:start + 1500].lower()
            if 'double patenting rejection' in near or 'rejected on the ground' in near:
                rid = f'R{len(rejections) + 1}'
                st = 'double_patenting_nonstatutory' if ('nonstatutory' in near or 'obviousness-type' in near) else 'double_patenting_statutory' if 'statutory double patenting' in near else 'other'
                rejections.append({'id': rid, 'statute': st, 'claims': [], 'locator': {'char_start': start, 'char_end': start + len(h)}})
                ann[rid] = {'pattern_id': 'heading_dp', 'basis': 'heading_only', 'confidence': 'heading_only', 'status_hint': 'unknown', 'section': h, 'ground': 'double_patenting'}
            else: headings_without.append(h)
        elif hl.startswith('claim objection') and not any(o['kind'] == 'claim' for o in objections): headings_without.append(h)
        elif hl.startswith('election/restriction') and not restriction: headings_without.append(h)
        elif hl == 'drawings' and not any(o['kind'] == 'drawing' for o in objections): headings_without.append(h)
        elif hl == 'specification' and not any(o['kind'] == 'specification' for o in objections): headings_without.append(h)

    fm, nm = FINAL.search(text), NONFINAL.search(text)
    if fm: finality, fbasis = 'final', 'phrase: THIS ACTION IS MADE FINAL'
    elif nm: finality, fbasis = 'non_final', 'phrase: non-final statement'
    elif document_code in ('CTFR', 'CTNF'): finality, fbasis = ('final' if document_code == 'CTFR' else 'non_final'), f'document code {document_code}'
    else: finality, fbasis = 'unknown', 'no finality statement found'
    kind = finality if finality != 'unknown' else 'other'
    if restriction and not rejections and finality == 'unknown': kind = 'restriction_election'
    pm = PERIOD.search(text)

    header = extract_header(text)
    if mail_date:   # supplied with the request (for example from document metadata): it wins over the text
        header['mail_date'] = {'value': mail_date, 'how': 'supplied with the request (document metadata)', **({'text_value': header['mail_date']['value']} if header.get('mail_date') and header['mail_date']['value'] != mail_date else {})}
    mail_date = (header.get('mail_date') or {}).get('value')
    action = {'document_id': document_id or 'caller-text', 'document_code': document_code or ('CTFR' if finality == 'final' else 'CTNF' if finality == 'non_final' else 'unknown'), 'kind': kind}
    if mail_date: action['mail_date'] = mail_date
    if pm: n = pm.group('n').upper(); action['shortened_statutory_period_months'] = int(n) if n.isdigit() else WORDS[n]
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest(); action['source_sha256'] = sha
    ocr_flag = bool(ocr) if ocr is not None else ('�' in text)
    action['source_form'] = 'ocr' if ocr_flag else 'xml' if document_code else 'pdf_text'
    if not document_code: del action['source_form']

    application = {'number': application_number or '00000000'}
    if header.get('art_unit'): application['art_unit'] = header['art_unit']['value']
    if header.get('examiner'): application['examiner'] = header['examiner']['value']
    if header.get('application_number') and application_number: header['application_number']['matches_request'] = header['application_number']['value'] == re.sub(r'\D', '', application_number)
    if AIA.search(text): application['aia'] = True
    elif PREAIA.search(text): application['aia'] = False

    analysis = {'schema_version': '1.0', 'application': application, 'action': action, 'rejections': rejections, 'objections': objections,
                'references': list(refs.values()),
                'provenance': {'method': 'rule_extracted', 'classifier': f'{PARSER} {VERSION} (form paragraphs, PTOL-326 summary lines, headings)',
                               'created': now or '1970-01-01T00:00:00Z', 'confidence': 'low' if ocr_flag else 'medium', 'ocr_derived': ocr_flag, 'human_verified': False,
                               'notes': 'Deterministic pattern extraction. Categories, examiner rationale and limitation mappings are not extracted.'}}
    if summary: analysis['claims_status'] = {k: sorted(v) for k, v in summary.items()}
    if restriction: analysis['restriction'] = restriction
    if asm_claims or allowed:
        asm = {'indicated': True, 'claims': sorted(asm_claims | set(allowed)), 'condition': asm_condition or 'allowed_as_is'}
        analysis['allowable_subject_matter'] = asm
    else:
        analysis['allowable_subject_matter'] = {'indicated': False}

    covered = {c for r in rejections for c in r['claims']}
    not_recognised = {'rejected_claims_without_ground': sorted(set(summary.get('rejected', set())) - covered),
                      'headings_without_findings': headings_without,
                      'ignored_rejection_sentences': ignored,
                      'notes': []}
    if not application_number: not_recognised['notes'].append('application number not supplied; application.number is a placeholder (00000000)')
    if not mail_date: not_recognised['notes'].append('mail date not determined; no deadline is computed in any case')
    if not rejections and not objections and not restriction: not_recognised['notes'].append('no rejection, objection or restriction pattern recognised; the text may not be an office action, may be OCR-damaged, or may use unrecognised wording')
    if 'other' in {r['statute'] for r in rejections}: not_recognised['notes'].append('at least one rejection basis was not recognised (statute "other")')
    ea = ELECTION_ACK.search(text)
    facts = {'election_acknowledged': bool(ea), 'finality': finality, 'finality_basis': fbasis, 'aia_status': 'aia' if application.get('aia') is True else 'pre_aia' if application.get('aia') is False else 'unknown',
             'shortened_period_text_found': bool(pm), 'ocr_suspected': ocr_flag}
    header['note'] = 'Read from the text of the action; unverified. Used for nothing except this result: no provider or network query and no write is made from it (local read-only database queries only).'
    return {'analysis': analysis, 'annotations': ann, 'facts': facts, 'not_recognised': not_recognised, 'header': header}
