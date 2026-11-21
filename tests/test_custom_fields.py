"""A user-chosen field name ('agri') and filing years before 2015 work end to end: frame plans, the tool service
(pg_search, pg_trends, pg_assess_office_action, pg_report, pg_status) and the coverage caveat computed from the data."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import lowfloor
import synthetic as syn
from frame_plan import plan, expansion_file
from pg_tool import stats as stats_mod
from pg_tool import local_data as ld, paths, scope as scope_mod, service
from pg_tool.validate import errors as schema_errors

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = service.CONTRACT['$defs']['Envelope']
OA = ('Application No. 10/000,003   Art Unit 1234\nExaminer: Roe, Rick   Notification Date: 05/06/2011\n\n'
      'Claim Rejections - 35 USC 101\nClaims 1-3 are rejected under 35 U.S.C. 101 because the claimed invention is directed to an abstract idea.\nTHIS ACTION IS MADE FINAL.\n')


def write_expansion(base, version='2026.08', field='agri'):
    f = Path(base) / 'corpus' / 'cpc' / version / 'expansions.json'; f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({'cpc_version': version, 'source': {'sha256': 'f' * 64}, 'strata': {field: {'seeds': ['A01B'], 'historical_prefixes': [], 'symbols': ['A01B1/00']}}}), encoding='utf-8')
    return f


def install_files():
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts)


class FramePlanCustomField(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.base = Path(self.tmp.name)

    def test_plan_for_a_custom_field_before_2015_with_any_frame_id(self):
        write_expansion(self.base)
        p = plan('batch-one', 'agri', '2010-01-01', '2010-01-20', 7, base=self.base)
        self.assertEqual([(w['from'], w['to']) for w in p['windows']], [('2010-01-01', '2010-01-08'), ('2010-01-08', '2010-01-15'), ('2010-01-15', '2010-01-20')])
        self.assertEqual(p['queries'][0]['query_id'], 'q-agri-w2010-01-01'); self.assertEqual(p['queries'][0]['stratum'], 'agri')
        self.assertIn('cpcClassificationBag:A01B*', p['queries'][0]['q'])

    def test_expansion_comes_from_the_argument_or_the_scope_version_inside_the_data_folder(self):
        a = write_expansion(self.base, '2026.08'); write_expansion(self.base, '2027.02')
        self.assertEqual(expansion_file('agri', self.base, version='2027.02'), 'corpus/cpc/2027.02/expansions.json')
        self.assertEqual(expansion_file('agri', self.base), 'corpus/cpc/2026.08/expansions.json')   # first by name
        self.assertEqual(expansion_file('agri', self.base, explicit=str(a)), 'corpus/cpc/2026.08/expansions.json')
        self.assertEqual(expansion_file('agri', self.base, explicit='corpus/cpc/2027.02/expansions.json'), 'corpus/cpc/2027.02/expansions.json')
        with tempfile.TemporaryDirectory() as other:
            outside = write_expansion(other)
            with self.assertRaises(ValueError): expansion_file('agri', self.base, explicit=str(outside))
        with self.assertRaises(KeyError): expansion_file('soil_sensing', self.base)   # defined in no expansion file

    def test_bad_ids_are_refused(self):
        write_expansion(self.base)
        for frame_id, field in (('con', 'agri'), ('NUL', 'agri'), ('ok-id', 'Agri'), ('ok-id', 'com1'), ('a b', 'agri')):
            with self.assertRaises(paths.PathError, msg=(frame_id, field)): plan(frame_id, field, '2010-01-01', '2010-02-01', base=self.base)

    def run_cli(self, *args):
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}; env.pop(paths.ENV_DATA, None)
        return subprocess.run([sys.executable, '-B', str(ROOT / 'frame_plan.py'), *args], capture_output=True, text=True, env=env, cwd=tempfile.gettempdir())

    def test_cli_uses_the_scope_and_writes_only_inside_the_data_folder(self):
        write_expansion(self.base)
        scope = json.loads((ROOT / 'examples' / 'scope.example.json').read_text(encoding='utf-8'))
        scope['filing'] = {'from': '2010-01-01', 'to_exclusive': '2010-01-15', 'window_days': 7}
        (self.base / 'scope.json').write_text(json.dumps(scope), encoding='utf-8')
        before = install_files()
        r = self.run_cli('batch-one', 'agri', '--data-dir', str(self.base)); self.assertEqual(r.returncode, 0, r.stderr)
        out = self.base / 'corpus' / 'frame-plans' / 'batch-one.json'
        self.assertEqual(len(json.loads(out.read_text(encoding='utf-8'))['windows']), 2)
        self.assertEqual(install_files(), before)
        again = self.run_cli('batch-one', 'agri', '--data-dir', str(self.base)); self.assertNotEqual(again.returncode, 0); self.assertIn('refusing to overwrite', again.stderr)
        bad = self.run_cli('batch-two', 'not_in_scope', '--data-dir', str(self.base)); self.assertNotEqual(bad.returncode, 0)
        reserved = self.run_cli('nul', 'agri', '--data-dir', str(self.base)); self.assertNotEqual(reserved.returncode, 0); self.assertIn('reserved', reserved.stderr)

    def test_cli_refuses_to_write_inside_the_install_folder(self):
        write_expansion(self.base)
        before = install_files()
        r = self.run_cli('batch-one', 'agri', '2010-01-01', '2010-02-01', '--data-dir', str(self.base), '--out', str(ROOT / 'plan-inside.json'))
        self.assertNotEqual(r.returncode, 0); self.assertIn('install folder', r.stderr)
        r = self.run_cli('batch-one', 'agri', '2010-01-01', '2010-02-01', '--data-dir', str(ROOT / 'scratch-data'))
        self.assertNotEqual(r.returncode, 0); self.assertIn('install folder', r.stderr)
        self.assertEqual(install_files(), before)


class ServiceWithCustomField(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = syn.make_data_folder(self.tmp.name)
        p = mock.patch.object(ld, 'DATA', Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        ld._cache.clear(); self.addCleanup(ld._cache.clear)
        self.addCleanup(lowfloor.patch_floor())   # tiny synthetic groups; the floor of 5 has its own test

    def ok(self, env):
        self.assertEqual(schema_errors(env, ENVELOPE, service.CONTRACT), [], env.get('error')); return env

    def test_pg_search_accepts_the_field_and_early_years(self):
        for year, n in ((2010, 3), (2011, 3)):
            e = self.ok(service.call('pg_search', {'source': 'local-frames', 'stratum': 'agri', 'filing_year': year, 'limit': 50}))
            self.assertEqual(e['status'], 'ok'); self.assertEqual(len(e['data']['results']), n)
            self.assertTrue(all(r['filing_year'] == str(year) and r['stratum'] == 'agri' and r['frame'] == 'batch-one' for r in e['data']['results']))
        self.assertEqual(service.call('pg_search', {'source': 'local-frames', 'stratum': 'agri'})['data']['total_reported'], 6)
        for bad in ({'filing_year': 1975}, {'filing_year': 2101}, {'stratum': 'Agri'}, {'stratum': 'agri; drop'}):
            e = service.call('pg_search', {'source': 'local-frames', **bad}); self.assertEqual(e['error']['code'], 'invalid_input', bad)
        e = service.call('pg_search', {'source': 'local-frames', 'stratum': 'other_field'}); self.assertEqual(e['status'], 'not_found')   # a valid name with no rows

    def test_pg_trends_groups_by_the_user_field_and_early_filing_years(self):
        e = self.ok(service.call('pg_trends', {'group_by': 'stratum', 'min_n': 1}))
        self.assertEqual(e['status'], 'ok'); self.assertEqual([r['group'] for r in e['data']['rows']], ['agri'])
        e = self.ok(service.call('pg_trends', {'group_by': 'filing_year', 'filters': {'stratum': 'agri'}, 'min_n': 1}))
        self.assertEqual([r['group'] for r in e['data']['rows']], ['2010', '2011'])
        row = {r['group']: r for r in e['data']['rows']}
        self.assertEqual(row['2010']['n'], 3)
        e = service.call('pg_trends', {'group_by': 'stratum', 'filters': {'stratum': 'Agri'}}); self.assertEqual(e['error']['code'], 'invalid_input')

    def test_coverage_caveat_is_computed_from_the_data(self):
        e = service.call('pg_trends', {'group_by': 'stratum', 'min_n': 1})
        text = ' '.join(e['caveats'])
        self.assertIn('run from 2011-01-10 to 2012-05-10', text); self.assertIn('5 of 6 frame applications', text)
        self.assertNotIn('2017', text); self.assertNotIn('Provider data starts', text)
        self.assertEqual(e['data']['coverage']['by_filing_year']['2010'], {'applications': 3, 'with_oa_data': 3})
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(ld, 'DATA', Path(empty)):
            ld._cache.clear()
            self.assertIn('No office-action rejection data', ld.coverage_caveat())

    def test_assess_with_a_user_field_context(self):
        e = self.ok(service.call('pg_assess_office_action', {'text': OA, 'context': {'stratum': 'agri', 'art_unit': '1234'}}))
        self.assertEqual(e['status'], 'ok'); self.assertEqual(e['data']['context']['stratum'], 'agri')
        self.assertEqual(e['data']['analysis']['application']['technology'], 'agri')
        self.assertTrue(e['data']['schema_conformance']['valid'], e['data']['schema_conformance'])
        self.assertEqual(service.call('pg_assess_office_action', {'text': OA, 'context': {'stratum': 'Agri'}})['error']['code'], 'invalid_input')

    def test_report_topic_and_status(self):
        e = self.ok(service.call('pg_report', {'subject': {'topic': {'stratum': 'agri'}}}))
        self.assertIn('stratum=agri', e['data']['markdown'])
        s = self.ok(service.call('pg_status', {}))
        self.assertEqual(s['data']['data_dir'], str(Path(self.tmp.name))); self.assertFalse(s['data']['scope']['present'])
        scope_mod.init(self.tmp.name)
        s = service.call('pg_status', {}); self.assertTrue(s['data']['scope']['valid']); self.assertEqual(s['data']['scope']['fields'], ['agri', 'soil_sensing'])
        (Path(self.tmp.name) / 'scope.json').write_text('{}', encoding='utf-8')
        s = service.call('pg_status', {}); self.assertIs(s['data']['scope']['valid'], False); self.assertTrue(s['data']['scope']['problems'])

    def test_the_frame_year_comes_from_each_row_and_ids_need_no_pattern(self):
        fr = ld.frames()
        self.assertEqual({r['frame'] for r in fr.values()}, {'batch-one'})
        self.assertEqual({r['filing_year'] for r in fr.values()}, {'2010', '2011'})
        syn.write_frame(self.tmp.name, 'Not A Valid Id', apps=[('10000099', '2011-01-01', 'Patented Case', '8000099')])
        ld._cache.clear()
        self.assertNotIn('10000099', ld.frames())     # a file whose name is not a valid frame id is ignored


if __name__ == '__main__':
    unittest.main()
