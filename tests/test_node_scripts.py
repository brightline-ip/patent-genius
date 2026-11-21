"""The Node scripts: exit 78 without the Hub (frame_run included), the data-folder rule, document-type selection from a preset,
codes or scope.json, and a clear exit 66 instead of a crash. No network and no real Hub: a stub gateway stands in where needed."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

from hubenv import StubHub, clean_environ

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
CLEAN = clean_environ()   # no key, no Hub setting, no look-up in the Hub's default folder


def node(script, *args, data=None, extra=None, cwd=None):
    env = {**CLEAN, **({'PG_TOOL_DATA_DIR': str(data)} if data is not None else {}), **(extra or {})}
    return subprocess.run([NODE, str(ROOT / script), *args], capture_output=True, text=True, env=env, cwd=cwd or tempfile.gettempdir(), timeout=60)


def install_files():
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts)


@unittest.skipUnless(NODE, 'node not available')
class WithoutTheHub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.data = Path(self.tmp.name) / 'data'; self.data.mkdir()

    def test_frame_run_exits_78_before_doing_anything(self):
        plan = {'frame_id': 'batch-one', 'windows': [{'id': 'w1', 'from': '2010-01-01', 'to': '2010-01-08'}], 'queries': [{'query_id': 'q1', 'stratum': 'agri', 'window_id': 'w1', 'q': 'x'}]}
        pp = Path(self.tmp.name) / 'plan.json'; pp.write_text(json.dumps(plan), encoding='utf-8')
        r = node('frame_run.mjs', str(pp), '100', '5', data=self.data)
        self.assertEqual(r.returncode, 78, r.stderr); self.assertIn('Patent Research Hub', r.stderr); self.assertNotIn('pages used', r.stderr)
        self.assertEqual(list(self.data.iterdir()), [])        # no receipts, no folders, nothing counted as used

    def test_the_other_scripts_still_exit_78(self):
        for script, args in (('frame_search.mjs', ['batch-one', 'q1', 'x', '10']), ('frame_dryrun.mjs', ['x', '1']), ('acquire_round.mjs', ['10000001', '1', 'DOC1']),
                             ('acquire_family.mjs', ['sel.json', '1', '1', '--preset', 'office-actions'])):
            r = node(script, *args, data=self.data); self.assertEqual(r.returncode, 78, (script, r.stderr))
        self.assertEqual(list(self.data.iterdir()), [])

    def test_a_reserved_frame_id_is_refused(self):
        hub = StubHub(Path(self.tmp.name) / 'hub')   # a Hub is "configured", so the frame id check is what refuses
        for fid in ('con', 'nul', 'Y2010', 'a b'):
            pp = Path(self.tmp.name) / f'{abs(hash(fid))}.json'; pp.write_text(json.dumps({'frame_id': fid, 'windows': [], 'queries': []}), encoding='utf-8')
            r = node('frame_run.mjs', str(pp), '100', '5', data=self.data, extra=hub.env())
            self.assertEqual(r.returncode, 2, (fid, r.stderr)); self.assertIn('invalid frame id', r.stderr)
        self.assertEqual(list(self.data.iterdir()), [])


@unittest.skipUnless(NODE, 'node not available')
class DataFolderRule(unittest.TestCase):
    def test_a_data_folder_inside_the_install_folder_is_refused(self):
        before = install_files()
        for sub in ('scratch-data', '.', 'a/b'):
            r = node('acquire_supplement.mjs', '--preset', 'procedural', data=ROOT / sub)
            self.assertEqual(r.returncode, 78, (sub, r.stderr)); self.assertIn('inside the install folder', r.stderr)
        self.assertFalse((ROOT / 'scratch-data').exists()); self.assertEqual(install_files(), before)

    def test_a_sibling_folder_with_the_same_prefix_is_allowed(self):
        with tempfile.TemporaryDirectory() as t:
            r = node('acquire_supplement.mjs', '--preset', 'procedural', data=Path(t) / 'data')
            self.assertEqual(r.returncode, 66, r.stderr)     # reached the "no normalized documents" check, so the folder rule let it through

    def test_default_folder_is_per_user_and_outside_the_install_folder(self):
        r = subprocess.run([NODE, '--input-type=module', '-e', f"import {{defaultDataDir, isInsideInstall}} from {(ROOT / 'hub_config.mjs').as_uri()!r}; console.log(JSON.stringify({{d: defaultDataDir(), inside: isInsideInstall(defaultDataDir())}}))"],
                           capture_output=True, text=True, env=CLEAN)
        d = json.loads(r.stdout); self.assertFalse(d['inside']); self.assertTrue(d['d'].endswith(('PatentGenius-Data', 'patent-genius-data')))


@unittest.skipUnless(NODE, 'node not available')
class DocumentSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.data = Path(self.tmp.name) / 'data'; self.data.mkdir()

    def test_nothing_is_selected_by_default_and_nothing_is_requested(self):
        r = node('acquire_supplement.mjs', data=self.data)
        self.assertEqual(r.returncode, 64, r.stderr); self.assertIn('no document types selected', r.stderr); self.assertEqual(list(self.data.iterdir()), [])
        (self.data / 'scope.json').write_text(json.dumps({'documents': {'preset': 'none'}}), encoding='utf-8')
        self.assertEqual(node('acquire_supplement.mjs', data=self.data).returncode, 64)

    def test_missing_normalized_folder_is_a_clear_exit_66_not_a_crash(self):
        r = node('acquire_supplement.mjs', '--preset', 'procedural', data=self.data)
        self.assertEqual(r.returncode, 66, r.stderr); self.assertIn('does not exist', r.stderr); self.assertNotIn('ENOENT', r.stderr); self.assertNotIn('at ', r.stderr)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_unknown_preset_is_refused(self):
        r = node('acquire_supplement.mjs', '--preset', 'everything', data=self.data); self.assertEqual(r.returncode, 2); self.assertIn('unknown preset', r.stderr)

    def codes(self, *args, scope=None):
        if scope is not None: (self.data / 'scope.json').write_text(json.dumps(scope), encoding='utf-8')
        code = (f"import {{documentSelection}} from {(ROOT / 'hub_config.mjs').as_uri()!r}; const r = documentSelection(process.argv.slice(1), 't'); console.log(JSON.stringify(r))")
        r = subprocess.run([NODE, '--input-type=module', '-e', code, '--', *args], capture_output=True, text=True, env={**CLEAN, 'PG_TOOL_DATA_DIR': str(self.data)})
        return r, (json.loads(r.stdout) if r.returncode == 0 else None)

    def test_presets_codes_and_scope_give_the_same_lists_as_python(self):
        from pg_tool import scope as scope_mod
        for preset in ('office-actions', 'prosecution', 'procedural'):
            r, d = self.codes('--preset', preset, 'positional'); self.assertEqual(d['codes'], scope_mod.presets()[preset]); self.assertEqual(d['positional'], ['positional'])
        r, d = self.codes('--codes', 'CTNF, A.NE', '3'); self.assertEqual(d['codes'], ['CTNF', 'A.NE'])
        r, d = self.codes(scope={'documents': {'preset': 'office-actions'}}); self.assertEqual(d['codes'], ['CTNF', 'CTFR'])
        r, d = self.codes(scope={'documents': {'preset': 'custom', 'codes': ['NOA', 'ABN']}}); self.assertEqual(d['codes'], ['NOA', 'ABN'])
        r, d = self.codes('--preset', 'procedural', scope={'documents': {'preset': 'office-actions'}}); self.assertEqual(d['codes'], scope_mod.presets()['procedural'])   # the flag wins over scope.json

    def test_acquire_family_uses_the_selected_codes_and_writes_a_receipt_in_the_data_folder(self):
        hub = StubHub(Path(self.tmp.name) / 'hub')     # a Hub is configured; an empty selection asks it for nothing
        sel = Path(self.tmp.name) / 'sel.json'
        sel.write_text(json.dumps({'frame_id': 'batch-one', 'selection': {'selected': []}}), encoding='utf-8')
        r = node('acquire_family.mjs', str(sel), '1', '5', '--preset', 'office-actions', data=self.data, extra=hub.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        receipts = list((self.data / 'corpus' / 'acquisitions').glob('batch-one-first1-*.json'))
        self.assertEqual(len(receipts), 1); self.assertEqual(json.loads(receipts[0].read_text(encoding='utf-8'))['priority'], ['CTNF', 'CTFR'])
        r = node('acquire_family.mjs', str(sel), '1', '5', data=self.data, extra=hub.env())     # no selection at all
        self.assertEqual(r.returncode, 64, r.stderr)


@unittest.skipUnless(NODE, 'node not available')
class FrameSearchThroughTheGateway(unittest.TestCase):
    """frame_search.mjs talks to the Hub only through the gateway command (a stand-in here): no key, no module import."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.data = Path(self.tmp.name) / 'data'; self.data.mkdir()

    def search(self, hub, extra=None):
        return node('frame_search.mjs', 'batch-one', 'q1', 'applicationMetaData.filingDate:[2010-01-01 TO 2010-01-07]', '10', '4', data=self.data, extra={**hub.env(), **(extra or {})})

    def test_pages_come_from_the_child_and_a_local_limit_is_waited_out(self):
        hub = StubHub(Path(self.tmp.name) / 'hub', uspto_count=6, script={'2': {'exit': 75, 'error': {'code': 'local_limit', 'message': 'hourly cap', 'retry_after_seconds': 0}}})
        r = self.search(hub); self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1]); self.assertEqual((out['provider_count'], out['rows'], out['stop_reason']), (6, 6, 'exhausted'))
        self.assertEqual(hub.call_count(), 3)        # page 1, the refused call, page 2: a 75 is waited out and asked again, not counted as a failure
        self.assertEqual([c['route'] for c in hub.calls()], ['uspto'] * 3)
        receipt = json.loads((self.data / 'public-originals' / 'frames' / 'batch-one' / 'q1' / 'receipt.json').read_text(encoding='utf-8'))
        self.assertNotIn('failed_attempts', receipt)

    def test_the_replacement_module_setting_is_ignored_and_a_stub_never_runs_beside_a_key(self):
        hub = StubHub(Path(self.tmp.name) / 'hub', uspto_count=2)
        r = self.search(hub, {'FRAME_GATEWAY_MODULE': str(Path(self.tmp.name) / 'evil.mjs')}); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn('FRAME_GATEWAY_MODULE is ignored', r.stderr)
        d2 = Path(self.tmp.name) / 'data2'; d2.mkdir()
        r = node('frame_search.mjs', 'batch-one', 'q2', 'x', '10', '4', data=d2, extra={**hub.env(), 'USPTO_API_KEY': 'not-a-real-key'})
        self.assertEqual(r.returncode, 78, r.stderr); self.assertIn('test-only', r.stderr); self.assertEqual(hub.call_count(), 1)       # nothing more reached the stand-in

    def test_an_older_hub_is_reported_as_too_old(self):
        hub = StubHub(Path(self.tmp.name) / 'hub', old_hub=True)
        r = self.search(hub, {'FRAME_RETRY_WAITS_MS': '0,0,0'}); self.assertEqual(r.returncode, 1, r.stderr)
        receipt = json.loads((self.data / 'public-originals' / 'frames' / 'batch-one' / 'q1' / 'receipt.json').read_text(encoding='utf-8'))
        self.assertIn('older than 0.2.0', receipt['error'])


if __name__ == '__main__':
    unittest.main()
