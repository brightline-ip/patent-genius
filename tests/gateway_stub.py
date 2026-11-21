"""A stand-in for `Hub.cmd gateway ...`, used only by the tests (through PG_HUB_TEST_STUB; see pg_tool/hubgw.py). It makes no network
call and reads no key. Its behaviour comes from the JSON file named by PG_STUB_SCRIPT:

  state       path of a JSON file that counts the calls (created on first use)
  log         path of a file that receives one line per call: the route and the request body (so a test can see what was sent)
  limits      the document `gateway --limits` prints (default: a Hub 0.2.0 with the USPTO hourly cap 1800 and 1 s spacing)
  old_hub     true: behave like a Hub before 0.2.0 (the unknown option is rejected: non-zero exit, no error object)
  fields      the field list for {"action":"fields"}
  documents   {application: [documentBag rows]} for the `application-documents` route; originals {application: {document: {size, kind, bad_signature}}} for `application-pdf`
  uspto_count the number of synthetic applications the `uspto` (ODP search) route reports
  rows        {application number: [provider rows]} served for {"action":"records"} (paged by start and rows, like the service)
  script      {"<call number>": step}: call n does this instead of the normal answer. A step is one of
              {"exit": 75, "error": {...}}          the Hub's own failure (stdout carries {"error": ...})
              {"status": 429, "retryAfterSeconds": 0}   a provider status returned in the envelope
              {"raw": "...", "exit": 1}              a failure with no error object
              {"envelope": {...}}                    this exact envelope with exit 0
"""
import json, os, sys

cfg = json.load(open(os.environ['PG_STUB_SCRIPT'], encoding='utf-8'))
args = sys.argv[1:]
if args and args[0] == 'gateway': args = args[1:]
DEFAULT_LIMITS = {'hub_version': '0.2.0', 'transport': 'direct', 'limiter_applies': True,
                  'providers': {'uspto': {'max_calls_per_hour': 1800, 'min_interval_ms': 1000, 'concurrent_calls_per_key': 1}},
                  'limiter_dir': 'stub', 'limiter_max_wait_ms': 600000, 'limiter_stale_lock_ms': 120000, 'min_wait_after_429_seconds': 5}
DEFAULT_FIELDS = ['hasRej101', 'hasRej102', 'hasRej103', 'hasRej112', 'hasRejDP', 'legalSectionCode', 'actionTypeCategory', 'paragraphNumber',
                  'groupArtUnitNumber', 'submissionDate', 'cite103Max', 'cite103EQ1', 'cite103GT3']


def out(doc, code=0):
    sys.stdout.write(json.dumps(doc)); sys.stdout.flush(); sys.exit(code)


if args == ['--limits']:
    if cfg.get('old_hub'): sys.stderr.write('unknown option --limits\n'); sys.exit(64)
    out(cfg.get('limits', DEFAULT_LIMITS))
if cfg.get('old_hub') or len(args) != 2 or args[1] != '--json-in': sys.stderr.write('usage\n'); sys.exit(1)

route = args[0]
body = json.loads(sys.stdin.read() or '{}')
state_path = cfg['state']
try: st = json.load(open(state_path, encoding='utf-8'))
except (OSError, ValueError): st = {'calls': 0}
st['calls'] += 1; n = st['calls']
json.dump(st, open(state_path, 'w', encoding='utf-8'))
if cfg.get('log'):
    with open(cfg['log'], 'a', encoding='utf-8') as f: f.write(json.dumps({'n': n, 'route': route, 'body': body}) + '\n')

step = (cfg.get('script') or {}).get(str(n))
if step:
    if 'envelope' in step: out(step['envelope'])
    if 'raw' in step: sys.stdout.write(step['raw']); sys.exit(step.get('exit', 1))
    if 'exit' in step: out({'error': step.get('error') or {'code': 'error', 'message': 'scripted failure'}}, step['exit'])
    if 'status' in step: out({'text': json.dumps({'message': 'scripted'}), 'status': step['status'], 'contentType': 'application/json', **({'retryAfterSeconds': step['retryAfterSeconds']} if 'retryAfterSeconds' in step else {})})

if route == 'application-documents':   # the file-wrapper inventory of cfg['documents'][application]
    app = body.get('application_number')
    out({'application_number': app, 'source_url': f'https://api.uspto.gov/api/v1/patent/applications/{app}/documents', 'status': 200,
         'body': {'documentBag': cfg.get('documents', {}).get(app, [])}, 'retrieved_at': '2026-01-01T00:00:00Z'})
if route == 'application-pdf':   # cfg['originals'][application][document] = {'size': n, 'kind': 'pdf'|'docx'|'tar', 'bad_signature': bool, 'wrong_identity': bool}
    import base64
    app, doc, fid, fmt = body.get('application_number'), body.get('document_identifier'), body.get('file_id'), body.get('format')
    spec = (cfg.get('originals', {}).get(app) or {}).get(doc)
    if not spec: out({'text': '{}', 'status': 404, 'contentType': 'application/json'})
    kind = spec.get('kind', 'pdf'); pre = f'https://api.uspto.gov/api/v1/download/applications/{app}/{doc}'
    url = pre + ('/xmlarchive' if fmt == 'xmlarchive' else f'/files/{fid}.{fmt}' if fid else '.pdf')
    size = int(spec.get('size', 2000))
    if kind == 'pdf': raw = b'%PDF-' + b'0' * (size - 5)
    elif kind == 'docx': raw = b'PK' + b'0' * (size - 2)
    else:   # a one-block TAR header with a valid checksum, padded to 512-byte blocks
        h = bytearray(512); h[0:5] = b'a.xml'; h[148:156] = b'        '
        chk = sum(h); h[148:156] = (oct(chk)[2:].zfill(6) + '\0 ').encode(); raw = bytes(h) + b'\0' * 512
    if spec.get('bad_signature'): raw = b'XXXXX' + raw[5:]
    ct = {'pdf': 'application/pdf', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'tar': 'application/x-tar'}[kind]
    out({'status': 200, 'application_number': app, 'document_identifier': 'OTHER' if spec.get('wrong_identity') else doc, 'extension': kind, 'url': url, 'file_id': fid,
         'contentType': ct, 'base64': base64.b64encode(raw).decode(), 'bytes': len(raw)})
if route == 'uspto':   # ODP application search: serves cfg['uspto_count'] synthetic applications, paged by offset and limit
    pg = (body.get('body') or {}).get('pagination') or {}
    off, lim, total = int(pg.get('offset', 0)), int(pg.get('limit', 1)), int(cfg.get('uspto_count', 0))
    bag = [{'applicationNumberText': f'{20000000 + i}'} for i in range(off, min(total, off + lim))]
    out({'text': json.dumps({'count': total, 'patentFileWrapperDataBag': bag}), 'status': 200, 'contentType': 'application/json'})
if route != 'oa-rejections': out({'error': {'code': 'bad_request', 'message': 'the stub serves only uspto and oa-rejections'}}, 64)
if body.get('action') == 'fields':
    out({'text': json.dumps({'fields': [{'name': f} for f in cfg.get('fields', DEFAULT_FIELDS)]}), 'status': 200, 'contentType': 'application/json'})
apps = body.get('application_numbers') or []
start, rows = int(body.get('start', 0)), int(body.get('rows', 100))
if not apps or any(not (isinstance(a, str) and len(a) == 8 and a.isdigit()) for a in apps) or not 1 <= rows <= 100:
    out({'error': {'code': 'bad_request', 'message': 'Supply 1-100 eight-digit application numbers'}}, 64)
allrows = [r for a in sorted(set(apps)) for r in cfg.get('rows', {}).get(a, [])]
page = allrows[start:start + rows]
out({'text': json.dumps({'response': {'numFound': len(allrows), 'start': start, 'docs': page}}), 'status': 200, 'contentType': 'application/json'})
