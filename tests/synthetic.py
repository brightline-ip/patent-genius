"""Builders for small SYNTHETIC data folders used by several test modules. Everything here is invented: application numbers,
examiners, art units and CPC symbols. The field is called 'agri' and the filing years are 2010 and 2011 on purpose, so tests
exercise a user-chosen field name and years before 2015."""
import gzip, json
from pathlib import Path

FIELD = 'agri'
FRAME_ID = 'batch-one'            # not the old y<year>-<stratum> pattern: any valid frame id must work
EXAMINER = 'ROE, RICK'
ART_UNIT = '1234'


def gz_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(obj, separators=(',', ':')).encode('utf-8'), mtime=0))


def gz_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(('\n'.join(json.dumps(r) for r in rows) + '\n').encode('utf-8'), mtime=0))


def frame_row(app, filing_date, status='Patented Case', patent=None, field=FIELD, examiner=EXAMINER, cpc=('A01B1/00',), eligible=True):
    return {'application_number': app, 'filing_date': filing_date, 'cpc': list(cpc), 'strata': [field], 'query_ids': ['q1'], 'eligible': eligible,
            'family_key': app, 'status_text': status, 'status_date': '2020-01-01', 'patent_number': patent, 'art_unit': ART_UNIT, 'examiner': examiner,
            'application_type': 'UTL', 'exclusion_reason': None if eligible else 'test'}


APPS = [  # (application, filing date, status, patent)
    ('10000001', '2010-03-01', 'Patented Case', '8000001'), ('10000002', '2010-05-01', 'Abandoned -- Failure to Respond', None),
    ('10000003', '2010-07-01', 'Patented Case', '8000003'), ('10000004', '2011-02-01', 'Abandoned -- Failure to Respond', None),
    ('10000005', '2011-04-01', 'Patented Case', '8000005'), ('10000006', '2011-09-01', 'Non Final Action Mailed', None)]


def write_frame(data, frame_id=FRAME_ID, field=FIELD, apps=APPS, extra_rows=()):
    rows = [frame_row(a, d, s, p, field) for a, d, s, p in apps] + list(extra_rows)
    frame = {'frame_version': 1, 'frame_id': frame_id, 'frozen_at': '2026-01-01T00:00:00Z', 'date_convention': 'test',
             'windows': [{'id': 'w1', 'from': '2010-01-01', 'to': '2012-01-01', 'basis': 'filing_date'}],
             'cpc': {'hierarchy_version': 'test', 'strata': {field: {'expanded_symbols': ['A01B1/00']}}}, 'queries': [], 'rows': rows,
             'funnel': {}, 'selection': {'method': 'none', 'selected': []}, 'notes': ['descriptive']}
    gz_json(Path(data) / 'corpus' / 'frames' / f'{frame_id}.json.gz', frame)
    return frame


def oa_rows(app, doc, code, date, statutes=('103',), art_unit=ART_UNIT):
    out = []
    for i, s in enumerate(statutes, 1):
        out.append({'patentApplicationNumber': app, 'obsoleteDocumentIdentifier': doc, 'legacyDocumentCodeIdentifier': code, 'submissionDate': date + 'T00:00:00',
                    'legalSectionCode': s, 'actionTypeCategory': 'rejected', 'claimNumberArrayDocument': ['1', '2'], 'groupArtUnitNumber': art_unit,
                    'paragraphNumber': i, 'aliceIndicator': 0, 'mayoIndicator': 0, 'cite103Max': 2})
    return out


def write_oa(data, frame_id=FRAME_ID):
    rows = []
    rows += oa_rows('10000001', 'D1', 'CTNF', '2011-01-10', ('102', '103'))
    rows += oa_rows('10000002', 'D2', 'CTNF', '2011-02-10', ('103',)) + oa_rows('10000002', 'D2b', 'CTFR', '2011-08-10', ('103',))
    rows += oa_rows('10000003', 'D3', 'CTNF', '2011-03-10', ('101',))
    rows += oa_rows('10000004', 'D4', 'CTNF', '2012-03-10', ('112',))
    rows += oa_rows('10000005', 'D5', 'CTNF', '2012-05-10', ('103',))
    rows += oa_rows('99999999', 'DX', 'CTNF', '2012-05-10', ('103',))   # an application that is in none of the frames
    gz_jsonl(Path(data) / 'corpus' / 'oa-rejections' / f'{frame_id}.jsonl.gz', rows)


def write_events(data, frame_id=FRAME_ID):
    ev = {'10000001': [['2011-01-10', 'CTNF'], ['2011-02-01', 'EXAT'], ['2011-03-01', 'MN/=.']],
          '10000002': [['2011-02-10', 'CTNF'], ['2011-08-10', 'CTFR'], ['2012-02-01', 'MABN2']],
          '10000003': [['2011-03-10', 'CTNF'], ['2011-06-01', 'MN/=.']],
          '10000004': [['2012-03-10', 'CTNF']], '10000005': [['2012-05-10', 'CTNF'], ['2012-09-01', 'MN/=.']]}
    gz_jsonl(Path(data) / 'corpus' / 'frame-events' / f'{frame_id}.jsonl.gz',
             [{'application_number': a, 'examiner': EXAMINER, 'art_unit': ART_UNIT, 'parents': [], 'events': e} for a, e in ev.items()])


def make_data_folder(root, frames=True, events=True, oa=True):
    data = Path(root)
    if frames: write_frame(data)
    if events: write_events(data)
    if oa: write_oa(data)
    return data


def write_document(data, app, doc_id, code, date, body, fidelity='text'):
    """A normalized full-text document the way the document pipeline lays it out: corpus/normalized/<app>/index.json plus one Markdown file."""
    d = Path(data) / 'corpus' / 'normalized' / app
    d.mkdir(parents=True, exist_ok=True)
    md = f'---\nofficial_date: "{date}"\ntext_fidelity: "{fidelity}"\ndocument_code: "{code}"\n---\n{body}'
    (d / f'{doc_id}.md').write_text(md, encoding='utf-8', newline='\n')
    idx = d / 'index.json'
    doc = json.loads(idx.read_text(encoding='utf-8')) if idx.exists() else {'application': app, 'documents': {}}
    doc['documents'][doc_id] = {'code': code, 'component': 'primary', 'pages': 1, 'markdown': f'{doc_id}.md', 'markdown_sha256': 'x'}
    idx.write_text(json.dumps(doc), encoding='utf-8')
