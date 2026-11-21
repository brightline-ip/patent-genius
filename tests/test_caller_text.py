"""Caller text (a client's office action) is never persisted by default and never seeds a query: nothing is written anywhere,
nothing is sent to the Hub, and nothing extracted from the text (header facts, application number, art unit, references)
is used to look anything up. persisted_by_tool is reported truthfully."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import synthetic as syn
from pg_tool import hubgw, local_data as ld, service

ROOT = Path(__file__).resolve().parents[1]
MARKER = 'ZZCALLERTEXTMARKERZZ'
OA = (f'Application No. 10/000,003   Art Unit 1234\nExaminer: Roe, Rick   Notification Date: 05/06/2011\n\n'
      f'Claim Rejections - 35 USC 103\nClaims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale (US 2019/0000001) in view of Ives. The {MARKER} widget is taught.\n'
      'THIS ACTION IS MADE FINAL.\n')


def files_under(*dirs):
    return sorted(str(p) for d in dirs for p in Path(d).rglob('*'))


class NothingIsWritten(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data, self.scratch, self.cwd = (Path(self.tmp.name) / n for n in ('data', 'scratch', 'cwd'))
        for d in (self.data, self.scratch, self.cwd): d.mkdir()
        syn.make_data_folder(self.data)

    def test_assess_and_report_write_nothing_anywhere(self):
        stack = [mock.patch.object(ld, 'DATA', self.data), mock.patch.object(tempfile, 'tempdir', str(self.scratch)),
                 mock.patch.dict(os.environ, {'TMP': str(self.scratch), 'TEMP': str(self.scratch), 'TMPDIR': str(self.scratch), 'PG_TOOL_DATA_DIR': str(self.data)})]
        for s in stack: s.start(); self.addCleanup(s.stop)
        ld._cache.clear(); self.addCleanup(ld._cache.clear)
        old_cwd = os.getcwd(); os.chdir(self.cwd); self.addCleanup(os.chdir, old_cwd)
        before_install = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts)
        before_data = files_under(self.data)
        for tool, req in (('pg_assess_office_action', {'text': OA}), ('pg_assess_office_action', {'text': OA, 'include_excerpts': True, 'context': {'stratum': 'agri'}}),
                          ('pg_report', {'subject': {'office_action': {'text': OA}}}), ('pg_report', {'subject': {'office_action': {'text': OA, 'include_excerpts': True}}})):
            e = service.call(tool, req)
            self.assertIn(e['status'], ('ok', 'partial'), tool)
            self.assertFalse(e['boundary']['persisted_by_tool']); self.assertEqual(e['boundary']['classification'], 'caller-confidential')
            self.assertEqual(e['boundary']['live_calls'], 0); self.assertFalse(e['boundary']['hub_store'])
        self.assertEqual(files_under(self.scratch, self.cwd), [])
        self.assertEqual(files_under(self.data), before_data)
        self.assertEqual(sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in p.parts and '.git' not in p.parts), before_install)

    def test_the_mcp_server_process_writes_nothing_and_logs_nothing(self):
        msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'pg_assess_office_action', 'arguments': {'text': OA}}}]
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PG_TOOL_DATA_DIR': str(self.data), 'TMP': str(self.scratch), 'TEMP': str(self.scratch), 'TMPDIR': str(self.scratch)}
        before = files_under(self.data)
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'pg_tool_mcp.py')], input='\n'.join(json.dumps(m) for m in msgs) + '\n', capture_output=True, text=True,
                           encoding='utf-8', timeout=120, cwd=self.cwd, env=env)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(out[1]['result']['structuredContent']['status'], 'ok')
        self.assertFalse(out[1]['result']['structuredContent']['boundary']['persisted_by_tool'])
        self.assertEqual(p.stderr, ''); self.assertFalse(MARKER in p.stdout)
        self.assertEqual(files_under(self.scratch, self.cwd), []); self.assertEqual(files_under(self.data), before)


class NothingIsLookedUp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        syn.make_data_folder(self.tmp.name)
        for s in (mock.patch.object(ld, 'DATA', Path(self.tmp.name)),
                  mock.patch.dict(os.environ, {'PG_HUB_ROOT': str(Path(self.tmp.name) / 'hub')})):
            s.start(); self.addCleanup(s.stop)
        ld._cache.clear(); self.addCleanup(ld._cache.clear)
        self.calls = []
        def trap(*a, **k): self.calls.append((a, k)); raise AssertionError('the Hub or a provider was called')
        for name in ('call', 'limits', '_run'):
            p = mock.patch.object(hubgw, name, trap); p.start(); self.addCleanup(p.stop)

    def test_nothing_in_the_text_triggers_a_hub_or_provider_call(self):
        # the text names application 10000003, which IS in the local frames, art unit 1234 (also local) and a US publication
        for req in ({'text': OA}, {'text': OA, 'include_statistics': True}):
            e = service.call('pg_assess_office_action', req)
            self.assertEqual(e['status'], 'ok', e.get('error'))
            self.assertEqual(e['boundary']['live_calls'], 0)
        e = service.call('pg_report', {'subject': {'office_action': {'text': OA}}, 'sections': ['summary', 'office_action', 'templates', 'statistics', 'post_grant', 'sources', 'limitations']})
        self.assertIn(e['status'], ('ok', 'partial')); self.assertEqual(self.calls, [])
        self.assertTrue(any('post-grant lookup skipped' in w for w in e['warnings']) or 'post_grant' not in [s['id'] for s in e['data']['sections']])

    def test_header_facts_do_not_select_local_context_either(self):
        e = service.call('pg_assess_office_action', {'text': OA})
        d = e['data']
        self.assertEqual(d['header']['application_number']['value'], '10000003'); self.assertEqual(d['header']['art_unit']['value'], '1234')
        self.assertEqual(d['context'], {})                         # no frame row was fetched because the text named an application
        self.assertEqual(d['statistics'], [])                      # no statistics were looked up by the text's art unit
        self.assertNotIn('technology', d['analysis']['application'])
        self.assertEqual(d['analysis']['application']['number'], '00000000')

    def test_only_the_request_can_choose_context(self):
        e = service.call('pg_assess_office_action', {'text': OA, 'application_number': '10000003'})   # an explicit request for a local application
        self.assertEqual(e['data']['context'].get('stratum'), 'agri')
        self.assertEqual(self.calls, [])

    def test_live_search_never_receives_text_derived_values(self):
        # a live search is possible only through an explicit pg_search request; assess/report cannot start one
        e = service.call('pg_assess_office_action', {'text': OA, 'live': True}); self.assertEqual(e['error']['code'], 'invalid_input')
        self.assertEqual(self.calls, [])


class TruthfulFlag(unittest.TestCase):
    def test_service_flag_is_false_because_the_service_never_writes(self):
        src = (ROOT / 'pg_tool' / 'service.py').read_text(encoding='utf-8')
        self.assertIn("'persisted_by_tool': False", src)
        for banned in ('.write_text(', '.write_bytes(', "open(", 'os.makedirs', '.mkdir('):
            body = src.replace("open(p, 'rb')", '')             # the one bounded read of an allowed input file
            self.assertNotIn(banned, body.replace("Path(raw)", ''), banned)

    def test_no_stored_run_or_log_carries_query_features(self):
        import pg_db
        self.assertNotIn('INSERT INTO collection_runs', (ROOT / 'pg_db.py').read_text(encoding='utf-8'))   # nothing in this release writes runs from caller text
        import sqlite3
        con = sqlite3.connect(':memory:'); con.executescript(pg_db.SCHEMA)
        cols = {r[1] for r in con.execute('PRAGMA table_info(collection_runs)')}; con.close()
        self.assertEqual(cols, {'run_id', 'kind', 'started_at', 'finished_at', 'scope_id', 'requests', 'applications', 'status', 'note'})


if __name__ == '__main__':
    unittest.main()
