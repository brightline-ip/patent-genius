"""Tool contract 1.1.0: eleven tools, the three new ones validated from the contract, and pg_db_report (named reports; exact examiner match only;
small groups left out; a floor on min_n)."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_db
from pg_tool import local_data as ld, mcp_server, service
from test_oa_db import build_folder

ROOT = Path(__file__).resolve().parents[1]
NEW = ('pg_similar_office_actions', 'pg_response_pack', 'pg_db_report')


class Contract(unittest.TestCase):
    def test_version_and_tools(self):
        c = service.CONTRACT; self.assertEqual(c['contract_version'], '1.1.0'); self.assertEqual(len(c['tools']), 11)
        self.assertEqual(set(service.TOOLS), set(c['tools'])); self.assertTrue(set(NEW) <= set(c['tools']))
        for name, t in c['tools'].items():
            self.assertIn(t['input']['$ref'].split('/')[-1], c['$defs'], name); self.assertIn(t['output_data']['$ref'].split('/')[-1], c['$defs'], name); self.assertTrue(t['description'])
        self.assertEqual([t['name'] for t in mcp_server.tool_list()], list(c['tools']))
        self.assertEqual(mcp_server.SERVER['version'], '1.1.0')

    def test_descriptions_promise_no_argument_text_model_or_prediction(self):
        for n in NEW:
            d = service.CONTRACT['tools'][n]['description'].lower()
            self.assertNotRegex(d, r'\bdrafts? (the )?(response|argument)|\bwrites? (the )?(response|argument)|predict(s|ion) (of|the) (outcome|allowance)|uses? a model')
        self.assertIn('no tool-authored argument text', service.CONTRACT['tools']['pg_response_pack']['description'])
        self.assertIn('no model, no prediction', service.CONTRACT['tools']['pg_similar_office_actions']['description'])

    def test_inputs_are_validated_by_the_contract(self):
        for tool, bad in (('pg_response_pack', {}), ('pg_response_pack', {'text': 'x', 'features': {'statutes': ['103']}, 'bogus': 1}), ('pg_response_pack', {'text': 'x', 'min_n': 0}),
                          ('pg_db_report', {}), ('pg_db_report', {'report': 'overview', 'min_n': 1}), ('pg_db_report', {'report': 'DROP'}), ('pg_db_report', {'report': 'examiners', 'examiner': 'ROE%'}),
                          ('pg_db_report', {'report': 'examiners', 'examiner': '_'}), ('pg_db_report', {'report': 'overview', 'years': '20x'}),
                          ('pg_similar_office_actions', {'text': 'x', 'limit': 51}), ('pg_similar_office_actions', {'text': 'x', 'min_score': 2}), ('pg_similar_office_actions', {'text': 'x', 'require': ['nope']})):
            e = service.call(tool, bad); self.assertEqual((e['status'], e['error']['code']), ('error', 'invalid_input'), (tool, bad))

    def test_the_mcp_server_lists_and_runs_the_new_tools_in_a_process(self):
        with tempfile.TemporaryDirectory() as t:
            msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'pg_db_report', 'arguments': {'report': 'coverage'}}}]
            env = {k: v for k, v in os.environ.items() if not k.startswith('PG_')}; env.update(PG_TOOL_DATA_DIR=t, PYTHONDONTWRITEBYTECODE='1')
            p = subprocess.run([sys.executable, '-B', str(ROOT / 'pg_tool_mcp.py')], input='\n'.join(json.dumps(m) for m in msgs) + '\n', capture_output=True, text=True, encoding='utf-8', timeout=120, env=env, cwd=t)
            out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
            self.assertEqual(len(out[0]['result']['tools']), 11); self.assertEqual(out[1]['result']['structuredContent']['status'], 'not_found'); self.assertEqual(p.stderr, '')


class DbReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.data = build_folder(cls.tmp.name); pg_db.build(data_dir=str(cls.data), say=lambda *_: None)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def setUp(self):
        p = mock.patch.object(ld, 'DATA', self.data); p.start(); self.addCleanup(p.stop)

    def test_a_named_report_with_provenance_and_the_left_out_count(self):
        e = service.call('pg_db_report', {'report': 'overview', 'min_n': 5}); self.assertEqual(e['status'], 'ok', e.get('error')); d = e['data']
        self.assertEqual((d['rows'], d['suppressed_groups']), ([], 2)); self.assertEqual(d['columns'][0], 'field'); self.assertEqual(d['built']['version'], '2.3')
        self.assertIn('Groups smaller than 5 are left out (2 left out here)', ' '.join(e['caveats'])); self.assertFalse(e['boundary']['persisted_by_tool']); self.assertEqual(e['boundary']['classification'], 'public')
        e = service.call('pg_db_report', {'report': 'coverage'}); self.assertTrue(any(r[0] == 'scope' for r in e['data']['rows']))
        self.assertTrue(any('office-action rows' in r[0] for r in e['data']['rows']))

    def test_the_examiner_filter_is_an_exact_name_not_a_pattern(self):
        for ok in ('Roe, Rick', 'roe, rick', 'ROE, RICK'):
            self.assertEqual(service.call('pg_db_report', {'report': 'examiners', 'examiner': ok, 'min_n': 5})['status'], 'ok', ok)
        for wild in ('ROE%', 'R_E', '%', 'ROE, RICK%'): self.assertEqual(service.call('pg_db_report', {'report': 'examiners', 'examiner': wild, 'min_n': 5})['error']['code'], 'invalid_input', wild)
        con = pg_db.connect_ro(self.data / 'corpus' / 'pg.db')
        try: n = con.execute("SELECT COUNT(*) FROM applications WHERE examiner LIKE 'ROE, RICK'").fetchone()[0]
        finally: con.close()
        self.assertGreater(n, 0)

    def test_small_groups_are_left_out_even_when_a_report_does_not_filter_them(self):
        r = pg_db.report_data(self.data / 'corpus' / 'pg.db', 'overview', min_n=1); self.assertEqual(len(r['rows']), 2)
        self.assertEqual(pg_db.report_data(self.data / 'corpus' / 'pg.db', 'overview', min_n=4)['rows'], [])

    def test_no_report_crosses_an_applicant_action_with_an_outcome_per_named_examiner(self):
        for name, (_, sql) in pg_db.REPORTS.items():
            self.assertNotIn('with_interview', sql or '', name)                                  # the interview-by-outcome-per-examiner column is gone everywhere
        db = self.data / 'corpus' / 'pg.db'; applicant = ('pct_interview', 'pct_rce', 'pct_appeal', 'interview', 'rce', 'afcp', 'notice_of_appeal')
        for name in pg_db.REPORTS:
            r = pg_db.report_data(db, name, min_n=5, public=True)
            if 'examiner' in r['columns']: self.assertFalse(set(r['columns']) & set(applicant), name)
            e = service.call('pg_db_report', {'report': name, 'min_n': 5}); self.assertEqual(e['status'], 'ok', name)
            if 'examiner' in e['data']['columns']: self.assertFalse(set(e['data']['columns']) & set(applicant), name)
        self.assertIn('pct_interview', pg_db.report_data(db, 'examiners', min_n=1)['columns'])                    # the command line keeps the descriptive column
        self.assertNotIn('pct_interview', service.call('pg_db_report', {'report': 'examiners', 'min_n': 5})['data']['columns'])

    def test_the_101_outcome_share_is_hidden_below_min_n_and_open_cases_are_counted_apart(self):
        db = self.data / 'corpus' / 'pg.db'
        r = pg_db.report_data(db, '101_art_units', min_n=1); c = r['columns']; row = dict(zip(c, r['rows'][0]))
        self.assertEqual((row['with_101'], row['resolved_after_101'], row['open_after_101']), (1, 1, 0)); self.assertEqual(row['pct_allowed_of_resolved_after_101'], 100.0)
        r = pg_db.report_data(db, '101_art_units', min_n=2); self.assertIsNone(dict(zip(r['columns'], r['rows'][0]))['pct_allowed_of_resolved_after_101'])        # with_101 is 1: below min_n
        self.assertNotIn('pct_granted_after_101', c)

    def test_no_database_is_not_found_and_the_row_limit_applies(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(ld, 'DATA', Path(t)):
            e = service.call('pg_db_report', {'report': 'overview'}); self.assertEqual(e['status'], 'not_found'); self.assertIn('db build', e['error']['message'])
        e = service.call('pg_db_report', {'report': 'coverage', 'limit': 3}); self.assertEqual(len(e['data']['rows']), 3); self.assertTrue(e['data']['truncated'])


if __name__ == '__main__':
    unittest.main()
