"""USPTO file-wrapper XML (ST.96) and as-filed DOCX -> component-separated Markdown.

Every XML part in an xmlarchive is classified on its own (DocumentMetadata/DocumentCode, then the member-name
code, then the root element, then the inventory code) and rendered to its OWN Markdown with an explicit
`component`, so claims, specification, remarks, office actions and data sheets are never conflated.
Amendment markup is preserved: <Ins> -> <ins>..</ins>, <Del> -> ~~..~~, <U> -> <u>..</u> (formatting only).
Claims render one per claim with number, status and dependencies.

Safety: archives are read member by member (never extracted to disk) within count/size bounds; only regular
*.xml members at <app>/<docId>/<file> are read; nested archives are skipped; XML or DOCX parts with a DOCTYPE
or ENTITY declaration are refused before parsing."""
import hashlib, io, json, re, tarfile, zipfile
import xml.etree.ElementTree as ET

# MAX_MEMBERS bounds tar header parsing only: an archive can carry many image members (for example one SVG per formula) that are
# never read. MAX_XML_MEMBERS bounds the XML members actually read, and the byte caps bound what is read.
MAX_MEMBERS, MAX_XML_MEMBERS, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES = 2000, 50, 5 * 1024 * 1024, 20 * 1024 * 1024
SKIP = {'BoundaryDataReference', 'BoundaryDataBag', 'BoundaryData', 'SpannedBoundaryData', 'HeaderText', 'Image',
        'FormParagraphNumber', 'FormParagraphCategory', 'RelatedFormParagraph', 'DocumentMetadata', 'DefaultFont',
        'LineNumber', 'PartNumber', 'ClaimNumber', 'ClaimStatusCategory'}
COMPONENT = {'CLM': 'claims', 'SPEC': 'specification', 'ABST': 'abstract', 'REM': 'remarks', 'DRW': 'drawings',
             'CTNF': 'office_action', 'CTFR': 'office_action', 'CTRS': 'office_action', 'CTEQ': 'office_action', 'CTAV': 'office_action',
             'NOA': 'notice_of_allowance', 'CNTA': 'notice_of_allowance', 'CNOA': 'notice_of_allowance', 'TRAN.LET': 'transmittal_letter', 'ABN': 'abandonment', 'A...': 'amendment', 'A.NE': 'amendment', 'A.PE': 'amendment',
             'A.NA': 'amendment', 'AMSB': 'amendment', 'RCEX': 'rce_request', 'ADS': 'application_data_sheet', 'IDS': 'ids',
             'APP.TEXT': 'application_text', 'EXIN': 'interview_summary', 'AP.B': 'appeal_brief', 'APRB': 'reply_brief', 'APEA': 'examiners_answer',
             'ELC.': 'election_response', 'INTRVIEW.APP': 'interview_request', 'A.NE.AFCP': 'afcp_request',
             # PEFR is the applicant's response to a pre-exam formalities notice, not a pre-appeal paper (labelled
             # pre_appeal_request before 2026-10-19; indexes written then keep the old label)
             'PEFR': 'pre_exam_formalities_response', 'AP.PRE.REQ': 'pre_appeal_request', 'AP.PRE.DEC': 'pre_appeal_decision',
             'N/AP': 'notice_of_appeal', 'APDA': 'appeal_decision', 'APDR': 'appeal_decision', 'APDP': 'appeal_decision'}
ROOT_COMPONENT = {'ClaimsDocument': 'claims', 'SpecificationDocument': 'specification', 'AbstractDocument': 'abstract'}
# USPTO abstracts use the SpecificationDocument root: a root is compatible with every component listed here.
ROOT_COMPATIBLE = {'SpecificationDocument': {'specification', 'abstract'}}
# Internal XML codes that stand for an inventory code (an allowance's XML part says CNTA/CNOA, its inventory says NOA).
ALIASES = {'NOA': {'CNTA', 'CNOA'}}


def same_code(part_code, inventory_code):
    return bool(inventory_code) and (part_code == inventory_code or part_code in ALIASES.get(inventory_code, ()))


def _local(tag): return re.sub(r'^\{[^}]*\}', '', tag)


def _refuse_dtd(data):
    if re.search(rb'<!DOCTYPE|<!ENTITY', data, re.I): raise ValueError('DTD/entity declarations are refused')


def xml_members(tar_bytes, app, doc):
    """[(name, bytes)] of the archive's XML documents for exactly this application/document."""
    out, total = [], 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as t:
        members = t.getmembers()
        if len(members) > MAX_MEMBERS: raise ValueError('archive has too many members')
        for m in members:
            parts = m.name.replace('\\', '/').split('/')
            if not m.isfile() or m.name.startswith('/') or '..' in parts or ':' in m.name or len(parts) != 3: continue
            if parts[0] != app or parts[1] != doc or not parts[2].lower().endswith('.xml'): continue
            if len(out) >= MAX_XML_MEMBERS: raise ValueError('archive has too many XML members')
            if m.size > MAX_MEMBER_BYTES or total + m.size > MAX_TOTAL_BYTES: raise ValueError('archive member too large')
            data = t.extractfile(m).read(MAX_MEMBER_BYTES + 1)
            if len(data) != m.size: raise ValueError('archive member size mismatch')
            total += m.size; out.append((parts[2], data))
    if not out: raise ValueError('archive has no XML document for this identity')
    return out


def _inline(el, mark=True):
    """Text of an element with amendment markup kept (mark=False drops it, e.g. for headings)."""
    out = []
    def walk(e):
        name = _local(e.tag)
        if name in SKIP:
            if e.tail: out.append(e.tail)
            return
        if name == 'ClaimReference':
            refs = [r.split('-')[-1].lstrip('0') for r in (e.get('{http://www.wipo.int/standards/XMLSchema/ST96/Common}idrefs') or '').split()]
            out.append(' claim ' + ', '.join(refs) + ' ' if refs else ' ')
            if e.tail: out.append(e.tail)
            return
        if name in ('Br', 'ClaimText'): out.append('\n')  # nested claim-text elements are separate claim lines
        wrap = {'Ins': ('<ins>', '</ins>'), 'Del': ('~~', '~~'), 'U': ('<u>', '</u>')}.get(name) if mark else None
        if wrap: out.append(wrap[0])
        if e.text: out.append(e.text)
        for c in e: walk(c)
        if wrap: out.append(wrap[1])
        if e.tail: out.append(e.tail)
    if el.text: out.append(el.text)
    for c in el: walk(c)
    text = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(out)).replace(' \n', '\n').replace('\n ', '\n')
    text = re.sub(r'(<ins>|<u>)\s*(</ins>|</u>)', '', text)            # empty markup
    text = text.replace('</u><u>', '').replace('</ins><ins>', '')     # adjacent fragments of the same markup
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()


def _meta(root):
    for m in root.iter():
        if _local(m.tag) == 'DocumentMetadata':
            return {_local(c.tag): c.text.strip() for c in m if c.text and c.text.strip() and len(c) == 0}
    return {}


def _claims(root):
    ns_c = '{http://www.wipo.int/standards/XMLSchema/ST96/Common}'
    blocks = []
    for c in root.iter():
        if _local(c.tag) != 'Claim': continue
        num = next((x.text.strip() for x in c if _local(x.tag) == 'ClaimNumber' and x.text), '?')
        status = next((x.text.strip() for x in c.iter() if _local(x.tag) == 'ClaimStatusCategory' and x.text), None)
        deps = sorted({r.split('-')[-1].lstrip('0') for x in c.iter() if _local(x.tag) == 'ClaimReference'
                       for r in (x.get(ns_c + 'idrefs') or '').split()}, key=lambda s: int(s) if s.isdigit() else 0)
        texts = [x for x in c if _local(x.tag) == 'ClaimText']
        body = '\n'.join(t for t in (_inline(x) for x in texts) if t)
        head = f'### Claim {num}' + (f' ({status})' if status else '') + (f' - depends on claim {", ".join(deps)}' if deps else '')
        blocks += [head, body]
    return blocks


def parse(xml_bytes):
    _refuse_dtd(xml_bytes)
    root = ET.fromstring(xml_bytes)
    kind = _local(root.tag)
    blocks = []
    if kind == 'ClaimsDocument' or any(_local(e.tag) == 'Claim' for e in root.iter()):
        blocks = _claims(root)
    if not blocks:
        def walk(e):
            name = _local(e.tag)
            if name in SKIP: return
            if name == 'Heading':
                t = _inline(e, mark=False)
                if t: blocks.append('## ' + t)
            elif name == 'P':
                t = _inline(e)
                if t: blocks.append(t)
            else:
                for c in e: walk(c)
        for c in root: walk(c)
    ocr = any(_local(e.tag) == 'OCRConfidenceData' for e in root.iter())
    return {'kind': kind, 'meta': _meta(root), 'blocks': blocks, 'uspto_ocr': ocr}


SPEC_HEADS = re.compile(r'FIELD|BACKGROUND|SUMMARY|DETAILED DESCRIPTION|BRIEF DESCRIPTION OF THE DRAWINGS|CROSS[- ]?REFERENCE', re.I)
REMARK_SIGNS = re.compile(r'\bREMARKS\b|\bARGUMENTS?\b|Applicant(s)? respectfully|REJECTIONS? UNDER|STATUS OF (THE )?CLAIMS|\bCONCLUSION\b|35 U\.S\.C\.', re.I)


def content_component(parsed):
    """What the text looks like, independent of its code: 'claims', 'specification', 'remarks' or None."""
    blocks = parsed['blocks']
    if blocks and blocks[0].startswith('### Claim '): return 'claims'
    heads = [b for b in blocks if b.startswith('## ')]
    spec = sum(1 for h in heads if SPEC_HEADS.search(h))
    remarks = sum(1 for b in blocks[:80] if REMARK_SIGNS.search(b))
    if spec >= 2 and remarks < 3: return 'specification'
    if remarks >= 2 and spec < 2: return 'remarks'
    return None


def normalize_code(code, inventory_code=None):
    """USPTO XML spells codes with '.' or '/' differently: member names hex-escape them ('AP+2eB' = 'AP.B') and
    DocumentCode metadata may use '_' ('AP_B'). Map back to the inventory spelling when that gives a known code."""
    if not code: return code
    c = re.sub(r'\+([0-9A-Fa-f]{2})', lambda m: chr(int(m.group(1), 16)), code).upper()
    if c not in COMPONENT and c != inventory_code and '_' in c:
        for alt in (c.replace('_', '.'), c.replace('_', '/')):
            if alt in COMPONENT or alt == inventory_code: return alt
    return c


def classify(member, parsed, inventory_code=None, single=False):
    """(code, component, code_source, warnings) for one XML part. The component follows the code unless the
    content clearly contradicts it (e.g. a marked-up substitute specification the USPTO indexed as REM)."""
    warnings = []
    code, source = parsed['meta'].get('DocumentCode'), 'metadata'
    if not code:
        # <app>.<MM-DD-YYYY>.<docId>.<CODE>.XML: the code is everything between the document id and .XML
        m = re.search(r'^\d{8}\.\d{2}-\d{2}-\d{4}\.[A-Za-z0-9_-]+\.(.+)\.XML$', member, re.I) or re.search(r'\.([A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)?)\.XML$', member, re.I)
        code, source = (m.group(1).upper(), 'member_name') if m else (None, None)
    code = normalize_code(code, inventory_code)
    root_component = ROOT_COMPONENT.get(parsed['kind'])
    if not code and single and inventory_code: code, source = inventory_code, 'inventory'
    component = COMPONENT.get(code) or root_component or ('office_communication' if parsed['kind'] == 'OutgoingDocument' else 'other')
    if root_component and COMPONENT.get(code) and COMPONENT[code] not in ROOT_COMPATIBLE.get(parsed['kind'], {root_component}):
        warnings.append(f'code {code} ({COMPONENT[code]}) disagrees with root {parsed["kind"]} ({root_component})')
    if not code: warnings.append('no document code found for this part')
    seen = content_component(parsed)
    if seen and component in ('claims', 'specification', 'remarks', 'amendment') and seen != component and not (component == 'amendment' and seen == 'remarks'):
        warnings.append(f'component_conflict: coded {code} ({component}) but the content is a {seen}; labelled by content')
        component = seen
    return code or 'UNKNOWN', component, source, warnings


def _front(fields):
    # JSON string syntax, ASCII only: a quote, backslash, line break or invisible/bidi character in a value (a source locator, a file name) can never break out of its line or hide in it.
    return ['---'] + [f'{k}: {json.dumps(str(v), ensure_ascii=True)}' for k, v in fields.items() if v is not None] + ['---', '']


def render(parsed, fields):
    body, prev_heading = [], True
    for b in parsed['blocks']:
        is_heading = b.startswith('#')
        if parsed['uspto_ocr'] and not is_heading and not prev_heading: body.append(b)
        else: body.extend(['', b] if body else [b])
        prev_heading = is_heading
    legend = 'amendment markup: <ins>inserted</ins>, ~~deleted~~, <u>underlined</u>'
    return '\n'.join(_front({**fields, 'markup': legend}) + body) + '\n'


def archive_parts(members, *, app, doc, inventory_code=None, archive_sha256, source_locator=None):
    """Render every XML part separately. Returns [{member, code, component, code_source, warnings, role, markdown, xml_sha256}]
    with the part whose code matches the inventory code first (role 'primary'); others are 'bundled'."""
    parts = []
    for name, data in members:
        p = parse(data)
        code, component, source, warns = classify(name, p, inventory_code, single=len(members) == 1)
        parts.append({'member': name, 'parsed': p, 'code': code, 'component': component, 'code_source': source,
                      'warnings': warns, 'xml_sha256': hashlib.sha256(data).hexdigest()})
    words = [set(re.findall(r'[a-z0-9]{3,}', ' '.join(x['parsed']['blocks']).lower())) for x in parts]
    for i in range(len(parts)):
        for j in range(i):
            union = words[i] | words[j]
            if union and len(words[i] & words[j]) / len(union) > 0.9:
                parts[i]['warnings'].append(f'near-duplicate of bundled part {parts[j]["member"]}')
    parts.sort(key=lambda x: (0 if same_code(x['code'], inventory_code) else 1, x['member']))
    if inventory_code and not same_code(parts[0]['code'], inventory_code):
        parts[0]['warnings'].append(f'no part carries the inventory code {inventory_code}; first part shown as primary')
    for i, x in enumerate(parts):
        x['role'] = 'primary' if i == 0 else 'bundled'
        p = x.pop('parsed')
        fidelity = 'USPTO OCR of an incoming filing (OCRConfidenceData present)' if p['uspto_ocr'] else 'native USPTO XML text'
        x['markdown'] = render(p, {'application': app, 'document_id': doc, 'document_code': x['code'], 'component': x['component'],
                                   'part_role': x['role'], 'inventory_code': inventory_code, 'official_date': p['meta'].get('OfficialDate'),
                                   'source': 'uspto-xmlarchive', 'xml_member': x['member'], 'xml_sha256': x['xml_sha256'],
                                   'archive_sha256': archive_sha256, 'source_locator': source_locator, 'text_fidelity': fidelity,
                                   'classification': x['code_source'], 'warnings': '; '.join(x['warnings']) or None,
                                   'derivative': 'Markdown rendering of USPTO XML; not the official record'})
    return parts


# ---- as-filed application text (APP.TEXT) delivered as DOCX -------------------------------------------------
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
# DOCX_MAX_ENTRIES bounds central-directory parsing only; only word/document.xml is read (its size and ratio are checked).
DOCX_MAX_ENTRIES, DOCX_MAX_UNCOMPRESSED = 2000, 50 * 1024 * 1024
HEADINGS = [('claims', re.compile(r'^(CLAIMS?|WHAT IS CLAIMED( IS)?:?|I CLAIM:?|WE CLAIM:?|THE CLAIMS)$', re.I)),
            ('abstract', re.compile(r'^(ABSTRACT|ABSTRACT OF THE DISCLOSURE)$', re.I))]


def docx_paragraphs(docx_bytes):
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        infos = z.infolist()
        if len(infos) > DOCX_MAX_ENTRIES or sum(i.file_size for i in infos) > DOCX_MAX_UNCOMPRESSED: raise ValueError('docx too large')
        info = z.getinfo('word/document.xml')
        if info.file_size > DOCX_MAX_UNCOMPRESSED or (info.compress_size and info.file_size / info.compress_size > 200): raise ValueError('docx document.xml ratio/size refused')
        data = z.read(info)
    _refuse_dtd(data)
    root = ET.fromstring(data)
    paras = []
    for p in root.iter(W + 'p'):
        text = ''.join((t.text or '') for t in p.iter() if t.tag in (W + 't',))  # w:delText (deleted revisions) is excluded
        text = re.sub(r'\s+', ' ', text).strip()
        if text: paras.append(text)
    return paras


def docx_components(paras):
    """Split as-filed text into specification / claims / abstract at exact heading paragraphs."""
    comps, current = {'specification': []}, 'specification'
    for t in paras:
        hit = next((name for name, rx in HEADINGS if rx.match(t)), None)
        if hit and hit not in comps: current = hit; comps[current] = []; continue
        comps[current].append(t)
    return {k: v for k, v in comps.items() if v}


def docx_parts(docx_bytes, *, app, doc, inventory_code, original_sha256, source_locator=None):
    comps = docx_components(docx_paragraphs(docx_bytes))
    code = {'specification': 'SPEC', 'claims': 'CLM', 'abstract': 'ABST'}
    out = []
    for i, (component, paras) in enumerate(comps.items()):
        warn = None if 'claims' in comps else 'no CLAIMS heading found; claims may remain inside the specification part'
        md = '\n'.join(_front({'application': app, 'document_id': doc, 'document_code': code[component], 'component': component,
                               'part_role': 'primary' if i == 0 else 'split', 'inventory_code': inventory_code, 'source': 'uspto-docx',
                               'original_sha256': original_sha256, 'source_locator': source_locator, 'text_fidelity': 'native text of the as-filed DOCX',
                               'classification': 'heading split of APP.TEXT', 'warnings': warn,
                               'derivative': 'Markdown rendering of the as-filed DOCX; not the official record'}) + ['\n\n'.join(paras)]) + '\n'
        out.append({'code': code[component], 'component': component, 'role': 'primary' if i == 0 else 'split', 'markdown': md, 'paragraphs': len(paras)})
    return out


# Backwards-compatible single-part API (used by earlier callers/tests)
def to_markdown(xml_bytes, *, app, doc, member, archive_sha256, document_type=None, source_locator=None):
    parts = archive_parts([(member, xml_bytes)], app=app, doc=doc, inventory_code=document_type, archive_sha256=archive_sha256, source_locator=source_locator)
    return parts[0]['markdown'], parts[0]['xml_sha256']
