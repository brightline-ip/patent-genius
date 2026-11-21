"""pg_cli.py: scope init|show|validate, db, assess and doctor. doctor makes no network call; assess writes only to a path the user names."""
import contextlib, io, json, os, socket, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_cli
import synthetic as syn
from hubenv import StubHub, clean_environ
from pg_tool import paths

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'examples' / 'sample' / 'office-action.txt'
MARKER = 'ZZCLIENTMARKERZZ'


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, env or {}):
        try: rc = pg_cli.main(argv)
        except SystemExit as e: rc = e.code if isinstance(e.code, int) else 1; err.write(str(e.code) if not isinstance(e.code, int) else '')
    return rc, out.getvalue(), err.getvalue()


def tree(p):
    return sorted(x.relative_to(p).as_posix() for x in Path(p).rglob('*')) if Path(p).exists() else []


class ScopeCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.d = self.tmp.name

    def test_init_show_validate(self):
        rc, out, err = run(['scope', 'init', '--data-dir', self.d]); self.assertEqual(rc, 0, err); self.assertIn('scope.json', out)
        self.assertEqual(run(['scope', 'validate', '--data-dir', self.d])[0], 0)
        rc, out, _ = run(['scope', 'show', '--data-dir', self.d]); d = json.loads(out)
        self.assertEqual(rc, 0); self.assertEqual(len(d['sha256']), 64); self.assertEqual(d['summary']['fields'], ['agri', 'soil_sensing']); self.assertEqual(d['summary']['filing_years'], [2012, 2013])
        rc, _, err = run(['scope', 'init', '--data-dir', self.d]); self.assertEqual(rc, 1); self.assertIn('already exists', err)
        self.assertEqual(run(['scope', 'init', '--data-dir', self.d, '--force'])[0], 0)

    def test_validate_reports_problems_and_missing_file(self):
        self.assertEqual(run(['scope', 'validate', '--data-dir', self.d])[0], 2)
        Path(self.d, 'scope.json').write_text(json.dumps({'scope_version': 1, 'fields': [{'name': 'con', 'seeds': ['A01B']}], 'filing': {'from': '2012-01-01', 'to_exclusive': '2011-01-01'}}), encoding='utf-8')
        rc, _, err = run(['scope', 'validate', '--data-dir', self.d]); self.assertEqual(rc, 1)
        self.assertIn('reserved', err); self.assertIn('earlier than', err)
        other = Path(self.d, 'elsewhere.json'); other.write_text((ROOT / 'examples' / 'scope.example.json').read_text(encoding='utf-8'), encoding='utf-8')
        self.assertEqual(run(['scope', 'validate', str(other)])[0], 0)

    def test_init_inside_the_install_folder_is_refused(self):
        rc, _, err = run(['scope', 'init', '--data-dir', str(ROOT / 'scratch-data')]); self.assertEqual(rc, 1); self.assertIn('install folder', err)
        self.assertFalse((ROOT / 'scratch-data').exists())


class DbCommand(unittest.TestCase):
    def test_build_report_and_sql_through_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            syn.make_data_folder(d)
            rc, out, err = run(['db', '--data-dir', d, 'build']); self.assertEqual(rc, 0, err); self.assertIn('built', out)
            rc, out, _ = run(['db', '--data-dir', d, 'report', 'overview', '--min-n', '1']); self.assertEqual(rc, 0); self.assertIn('| agri | 2010 | 3 |', out)
            rc, out, _ = run(['db', '--data-dir', d, 'sql', 'SELECT COUNT(*) AS n FROM applications']); self.assertIn('| 6 |', out)
            rc, _, _ = run(['db', '--data-dir', d, 'sql', 'DELETE FROM events']); self.assertNotEqual(rc, 0)
            rc, out, _ = run(['db', '--data-dir', d, 'report', 'coverage']); self.assertIn('filing year 2010', out)
        rc, _, _ = run(['db', '--data-dir', str(ROOT / 'scratch-data'), 'build']); self.assertNotEqual(rc, 0)
        self.assertFalse((ROOT / 'scratch-data').exists())


class Assess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.d = Path(self.tmp.name) / 'data'; self.d.mkdir()
        self.text = Path(self.tmp.name) / 'oa.txt'
        self.text.write_text(SAMPLE.read_text(encoding='utf-8') + f'\nClaim 10 is rejected under 35 U.S.C. 112(b) as being indefinite because the {MARKER} term is unclear.\n', encoding='utf-8')

    def test_stdout_only_writes_nothing_and_says_so(self):
        before = tree(self.tmp.name)
        rc, out, err = run(['assess', str(self.text), '--data-dir', str(self.d)]); self.assertEqual(rc, 0, err)
        env = json.loads(out)
        self.assertEqual(env['status'], 'ok'); self.assertFalse(env['boundary']['persisted_by_tool']); self.assertNotIn('persisted_to', env['boundary'])
        self.assertEqual(tree(self.tmp.name), before)
        self.assertFalse(MARKER in out)        # offsets only, unless excerpts are requested
        self.assertEqual(env['data']['header']['art_unit']['value'], '9999'); self.assertEqual(env['data']['header']['mail_date']['value'], '2026-01-15')

    def test_out_file_is_the_only_write_and_persisted_by_tool_is_true(self):
        out_path = Path(self.tmp.name) / 'result' / 'assessment.json'
        before = tree(self.d)
        rc, out, err = run(['assess', str(self.text), '--out', str(out_path), '--data-dir', str(self.d)]); self.assertEqual(rc, 0, err)
        env = json.loads(out_path.read_text(encoding='utf-8'))
        self.assertTrue(env['boundary']['persisted_by_tool']); self.assertEqual(env['boundary']['persisted_to'], str(out_path))
        self.assertEqual(tree(self.d), before)       # nothing was written into the data folder
        self.assertEqual(run(['assess', str(self.text), '--out', str(out_path)])[0], 1)   # no silent overwrite
        self.assertEqual(run(['assess', str(self.text), '--out', str(out_path), '--force'])[0], 0)

    def test_out_inside_the_install_folder_is_refused(self):
        target = ROOT / 'assessment-inside.json'
        rc, _, err = run(['assess', str(self.text), '--out', str(target)]); self.assertEqual(rc, 1); self.assertIn('install folder', err)
        self.assertFalse(target.exists())

    def test_markdown_pdf_and_missing_file(self):
        rc, out, _ = run(['assess', str(self.text), '--markdown']); self.assertEqual(rc, 0); self.assertTrue(out.startswith('> **DRAFT'))
        pdf = Path(self.tmp.name) / 'x.pdf'; pdf.write_bytes(b'%PDF-1.4')
        self.assertEqual(run(['assess', str(pdf)])[0], 2)
        self.assertEqual(run(['assess', str(Path(self.tmp.name) / 'missing.txt')])[0], 2)

    def test_excerpts_only_when_asked(self):
        rc, out, _ = run(['assess', str(self.text), '--include-excerpts']); self.assertTrue(MARKER in out)


class Doctor(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, clean_environ(), clear=True); p.start(); self.addCleanup(p.stop)   # never reach a real Hub or its default folder

    def test_report_has_every_item_and_makes_no_network_call(self):
        def boom(*a, **k): raise AssertionError('network call attempted')
        with tempfile.TemporaryDirectory() as d, mock.patch.object(socket.socket, 'connect', boom), mock.patch('socket.create_connection', boom), mock.patch('socket.getaddrinfo', boom):
            r = pg_cli.doctor_report(d)
            self.assertEqual(r['network_calls'], 0)
            self.assertEqual(set(r), {'network_calls', 'python', 'node', 'data_dir', 'scope', 'hub', 'tesseract'})
            self.assertTrue(r['python']['ok']); self.assertEqual(r['data_dir']['path'], str(Path(d).resolve())); self.assertTrue(r['data_dir']['exists']); self.assertTrue(r['data_dir']['ok'])
            self.assertFalse(r['scope']['present'])
            rc, out, _ = run(['doctor', '--data-dir', d]); self.assertEqual(rc, 0); self.assertIn('makes no network call', out)
            rc, out, _ = run(['doctor', '--json', '--data-dir', d]); self.assertEqual(json.loads(out)['network_calls'], 0)

    def test_data_folder_states(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / 'not-yet'
            r = pg_cli.doctor_report(str(d)); self.assertFalse(r['data_dir']['exists']); self.assertTrue(r['data_dir']['ok']); self.assertFalse(d.exists())   # doctor never creates it
        r = pg_cli.doctor_report(str(ROOT / 'inside')); self.assertTrue(r['data_dir']['inside_install_folder']); self.assertFalse(r['data_dir']['ok'])
        self.assertEqual(run(['doctor', '--data-dir', str(ROOT / 'inside')])[0], 1)
        self.assertFalse((ROOT / 'inside').exists())

    def test_scope_hub_and_tesseract_detection(self):
        with tempfile.TemporaryDirectory() as t:
            data = Path(t) / 'data'; data.mkdir(); (data / 'scope.json').write_text('{"scope_version": 1}', encoding='utf-8')
            stub = StubHub(Path(t) / 'hub', limits={'hub_version': '9.8.7', 'providers': {}})
            with stub.patch(PG_TESSERACT=str(Path(t) / 'no-such-tesseract')):
                r = pg_cli.doctor_report(str(data))
            self.assertTrue(r['hub']['configured']); self.assertTrue(r['hub']['present']); self.assertTrue(r['hub']['ok']); self.assertEqual(r['hub']['version'], '9.8.7')
            self.assertEqual(r['hub']['command'], '(test stand-in)')
            self.assertIs(r['scope']['valid'], False); self.assertTrue(r['scope']['problems'])
            self.assertIsNone(r['tesseract']['version'])
            # an older Hub: the unknown option is rejected without an error object
            old = StubHub(Path(t) / 'old', old_hub=True)
            with old.patch(): h = pg_cli.doctor_report(str(data))['hub']
            self.assertFalse(h['ok']); self.assertIn('hub_too_old', h['problem'])
            # a Hub folder with no Hub.cmd, then nothing configured at all
            with mock.patch.dict(os.environ, clean_environ(PG_HUB_ROOT=str(Path(t) / 'missing')), clear=True):
                h = pg_cli.doctor_report(str(data))['hub']; self.assertTrue(h['configured']); self.assertFalse(h['present']); self.assertFalse(h['ok'])
            with mock.patch.dict(os.environ, clean_environ(), clear=True):
                h = pg_cli.doctor_report(str(data))['hub']; self.assertFalse(h['configured']); self.assertFalse(h['present'])

    def test_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, '-B', str(ROOT / 'pg_cli.py'), 'doctor', '--json', '--data-dir', d], capture_output=True, text=True, cwd=tempfile.gettempdir())
            self.assertEqual(json.loads(p.stdout)['network_calls'], 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
