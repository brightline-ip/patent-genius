"""File-wrapper document downloads through the Hub gateway command (routes application-documents and application-pdf), with the stand-in Hub:
inventory parsing, hash-checked staging of PDF / DOCX / XML-archive originals, a 12 MB answer read in full, refusal of a bad signature or a
changed identity, and the family downloader end to end. No Hub module is imported anywhere and no key is held by these processes."""
import hashlib, json, os, re, shutil, subprocess, tempfile, unittest
from pathlib import Path

from hubenv import StubHub, clean_environ

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
APP = '10000001'
URL = lambda app, doc: f'https://api.uspto.gov/api/v1/download/applications/{app}/{doc}'


def row(doc, code, date, options):
    return {'applicationNumberText': APP, 'documentIdentifier': doc, 'documentCode': code, 'documentCodeDescriptionText': 'x', 'officialDate': date + 'T00:00:00', 'downloadOptionBag': options}


def pdf_opt(doc): return {'mimeTypeIdentifier': 'PDF', 'downloadUrl': URL(APP, doc) + '.pdf', 'pageTotalQuantity': 3}


@unittest.skipUnless(NODE, 'node not available')
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name); self.data = self.root / 'data'; self.data.mkdir()

    def node(self, script, *args, hub=None, extra=None, timeout=120):
        env = {**(hub.env() if hub else clean_environ()), 'PG_TOOL_DATA_DIR': str(self.data), **(extra or {})}
        return subprocess.run([NODE, str(ROOT / script), *args], capture_output=True, text=True, env=env, cwd=str(self.root), timeout=timeout)

    def js(self, code, *args):
        return subprocess.run([NODE, '--input-type=module', '-e', code, '--', *args], capture_output=True, text=True, env=clean_environ(), cwd=str(self.root), timeout=60)


class Unit(Base):
    def test_inventory_statuses_follow_the_documented_forms(self):
        rows = [row('DOC1', 'CTNF', '2011-01-10', [pdf_opt('DOC1'), {'mimeTypeIdentifier': 'XML', 'downloadUrl': URL(APP, 'DOC1') + '/xmlarchive'}]),
                row('DOC2', 'CTFR', '2011-08-10', [{'mimeTypeIdentifier': 'PDF', 'downloadUrl': URL(APP, 'DOC2') + '/files/ab12.pdf'}]),
                row('DOC3', 'A...', '2011-09-01', [{'mimeTypeIdentifier': 'PDF', 'downloadUrl': 'https://evil.example/x.pdf'}]),
                row('DOC4', 'REM', '2011-09-02', [{'mimeTypeIdentifier': 'MS_WORD', 'downloadUrl': URL(APP, 'DOC4') + '/files/w9.docx'}]),
                {'applicationNumberText': '99999999', 'documentIdentifier': 'DOC5'}, row('DOC1', 'CTNF', '2011-01-10', [])]
        code = "import {parseInventory} from %s; const r = parseInventory(JSON.parse(process.argv[1]), process.argv[2]); console.log(JSON.stringify(r))" % json.dumps((ROOT / 'hub_documents.mjs').as_uri())
        r = self.js(code, json.dumps(rows), APP); self.assertEqual(r.returncode, 0, r.stderr); inv = json.loads(r.stdout)
        by = {d['document_id']: d for d in inv['documents']}
        self.assertEqual((by['DOC1']['status'], by['DOC1']['xml_available']), ('AUTHENTICATED_DOWNLOAD_REQUIRED', True))
        self.assertEqual((by['DOC2']['status'], by['DOC2']['file_id']), ('AUTHENTICATED_FILE_DOWNLOAD_REQUIRED', 'ab12'))
        self.assertEqual(by['DOC3']['status'], 'UNRECOGNIZED_DOWNLOAD_DESTINATION'); self.assertEqual(by['DOC4']['docx_file_id'], 'w9')
        self.assertEqual(sorted(i['status'] for i in inv['issues']), ['DUPLICATE_DOCUMENT_ID', 'UNRESOLVED_IDENTITY'])

    def test_requests_outside_the_bounded_contract_are_refused(self):
        code = "import {originalIdentity} from %s; const bad = []; for (const x of JSON.parse(process.argv[1])) { try { originalIdentity(x); bad.push(x) } catch {} } console.log(JSON.stringify(bad))" % json.dumps((ROOT / 'hub_documents.mjs').as_uri())
        cases = [{'applicationNumber': '1234', 'documentIdentifier': 'A'}, {'applicationNumber': APP, 'documentIdentifier': '../x'}, {'applicationNumber': APP, 'documentIdentifier': 'A', 'format': 'exe'},
                 {'applicationNumber': APP, 'documentIdentifier': 'A', 'format': 'pdf'}, {'applicationNumber': APP, 'documentIdentifier': 'A', 'fileId': 'x'}, {'applicationNumber': APP, 'documentIdentifier': 'A', 'format': 'xmlarchive', 'fileId': 'x'}]
        r = self.js(code, json.dumps(cases)); self.assertEqual(json.loads(r.stdout), [], r.stderr)


class Originals(Base):
    def acquire(self, hub, *specs):
        r = self.node('acquire_round.mjs', APP, str(len(specs)), *specs, hub=hub); self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1]), json.loads(Path(json.loads(r.stdout.strip().splitlines()[-1])['receipt']).read_text(encoding='utf-8'))

    def test_pdf_docx_and_xml_archive_are_staged_with_verified_hashes(self):
        hub = StubHub(self.root / 'hub', originals={APP: {'P1': {'size': 3000}, 'W1': {'kind': 'docx', 'size': 900}, 'X1': {'kind': 'tar'}}})
        summary, rec = self.acquire(hub, 'P1', 'W1@docx:f1', 'X1@xml')
        self.assertEqual((summary['acquired'], summary['failures']), (3, 0))
        d = self.data / 'public-originals' / APP
        for name in ('P1.pdf', 'W1.docx', 'X1.tar'): self.assertTrue((d / name).is_file(), name)
        for a in rec['acquired']: self.assertEqual(hashlib.sha256(Path(a['staged_path']).read_bytes()).hexdigest(), a['sha256'])
        sent = [c['body'] for c in hub.calls()]; self.assertEqual(sorted(c['route'] for c in hub.calls()), ['application-pdf'] * 3)
        self.assertIn({'application_number': APP, 'document_identifier': 'W1', 'file_id': 'f1', 'format': 'docx'}, sent); self.assertIn({'application_number': APP, 'document_identifier': 'X1', 'format': 'xmlarchive'}, sent)

    def test_a_twelve_megabyte_original_comes_through_whole(self):
        hub = StubHub(self.root / 'hub', originals={APP: {'BIG': {'size': 12 * 1024 * 1024}}})
        summary, rec = self.acquire(hub, 'BIG'); self.assertEqual(summary['acquired'], 1)
        self.assertEqual((self.data / 'public-originals' / APP / 'BIG.pdf').stat().st_size, 12 * 1024 * 1024)

    def test_a_bad_signature_a_changed_identity_and_a_missing_document_stage_nothing(self):
        hub = StubHub(self.root / 'hub', originals={APP: {'BAD': {'bad_signature': True}, 'SWAP': {'wrong_identity': True}}})
        summary, rec = self.acquire(hub, 'BAD', 'SWAP', 'GONE'); self.assertEqual((summary['acquired'], summary['failures']), (0, 3))
        msgs = ' | '.join(f['error'] for f in rec['failures']); self.assertIn('signature', msgs); self.assertIn('identity', msgs); self.assertIn('status 404', msgs)
        self.assertEqual([p.name for p in (self.data / 'public-originals' / APP).glob('*.pdf')], [])

    def test_it_never_overwrites_a_staged_file_and_needs_a_hub(self):
        hub = StubHub(self.root / 'hub', originals={APP: {'P1': {'size': 2000}}})
        self.acquire(hub, 'P1'); summary, rec = self.acquire(hub, 'P1'); self.assertEqual(summary['failures'], 1); self.assertIn('refusing to overwrite', rec['failures'][0]['error'])
        r = self.node('acquire_round.mjs', APP, '1', 'P1'); self.assertEqual(r.returncode, 78)

    def test_an_older_hub_and_a_key_beside_the_stand_in_are_stops(self):
        r = self.node('acquire_round.mjs', APP, '1', 'P1', hub=StubHub(self.root / 'old', old_hub=True))
        self.assertIn('older than 0.2.0', json.dumps(json.loads(Path(json.loads(r.stdout.strip().splitlines()[-1])['receipt']).read_text(encoding='utf-8'))))
        hub = StubHub(self.root / 'h2', originals={APP: {'P1': {'size': 2000}}})
        r = self.node('acquire_round.mjs', APP, '1', 'P1', hub=hub, extra={'USPTO_API_KEY': 'not-a-real-key'}); self.assertEqual(r.returncode, 78); self.assertEqual(hub.call_count(), 0)


class Family(Base):
    def test_the_family_downloader_lists_chooses_and_downloads_through_the_gateway(self):
        rows = [row('CT1', 'CTNF', '2011-01-10', [pdf_opt('CT1')]), row('FR1', 'CTFR', '2011-08-10', [pdf_opt('FR1'), {'mimeTypeIdentifier': 'XML', 'downloadUrl': URL(APP, 'FR1') + '/xmlarchive'}]),
                row('OT1', 'IDS', '2011-02-01', [pdf_opt('OT1')])]
        hub = StubHub(self.root / 'hub', documents={APP: rows}, originals={APP: {'CT1': {'size': 2000}, 'FR1': {'kind': 'tar'}}})
        sel = self.root / 'sel.json'
        sel.write_text(json.dumps({'frame_id': 'batch-one', 'selection': {'selected': [{'family_key': 'F1', 'stratum': 'agri', 'application_numbers': [APP]}]}}), encoding='utf-8')
        r = self.node('acquire_family.mjs', str(sel), '1', '5', '--preset', 'office-actions', hub=hub); self.assertEqual(r.returncode, 0, r.stderr)
        d = self.data / 'public-originals' / APP
        self.assertTrue((d / 'CT1.pdf').is_file() and (d / 'FR1.tar').is_file()); self.assertFalse((d / 'OT1.pdf').exists())     # only the selected document types, XML where offered
        self.assertTrue((d / 'inventory.json').is_file())
        routes = [c['route'] for c in hub.calls()]; self.assertEqual(routes.count('application-documents'), 1); self.assertEqual(routes.count('application-pdf'), 2)
        n = hub.call_count()
        r = self.node('acquire_family.mjs', str(sel), '1', '5', '--preset', 'office-actions', hub=hub); self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(hub.call_count(), n)   # nothing is fetched twice
        self.assertIn('nothing new to download', r.stdout)


class NoHubInternals(unittest.TestCase):
    """No code path in Patent Genius imports Hub internals or loads a Hub module."""
    PATTERNS = [r'patent-documents\.mjs', r'src[\\/]+gateway\.mjs', r'src[\\/]+hub\.mjs', r'src[\\/]+release\.mjs', r'importHubModule', r"join\([^)]*,\s*'src'", r'integrations[\\/]+research-client',
                r'\bResearchClient\b', r'hub_bridge', r'PG_TOOL_LIVE', r'pathToFileURL\(path\.resolve\(target', r'FRAME_GATEWAY_MODULE\]|process\.env\.FRAME_GATEWAY_MODULE\)\s*\)?\s*import']

    def test_no_code_imports_hub_internals(self):
        code = [p for p in ROOT.rglob('*') if p.suffix in ('.py', '.mjs') and 'tests' not in p.parts and '.git' not in p.parts and '__pycache__' not in p.parts]
        self.assertGreater(len(code), 20)
        for p in code:
            t = p.read_text(encoding='utf-8')
            if re.search(r'\bimport\(', t) and p.suffix == '.mjs': self.assertNotRegex(t, r'import\((?!\s*[\'"]node:|\s*[\'"]\./)', p.name)       # no dynamic import of a computed (Hub) path
            for pat in self.PATTERNS[:-1]:
                body = '\n'.join(l for l in t.splitlines() if not l.lstrip().startswith(('//', '#', '"""', '*')))
                self.assertNotRegex(body, pat, f'{p.relative_to(ROOT)} matches {pat}')

    def test_the_bridge_and_its_helpers_are_gone(self):
        for name in ('pg_tool/hub_bridge.mjs', 'pg_tool/hub.py', 'pg_tool/usptolock.py'): self.assertFalse((ROOT / name).exists(), name)

    def test_the_stand_in_is_the_only_replacement_and_it_is_guarded(self):
        from pg_tool import hubgw
        with self.assertRaises(hubgw.HubGatewayError): hubgw.find_hub(clean_environ(PG_HUB_TEST_STUB='["x"]'))


if __name__ == '__main__':
    unittest.main()
