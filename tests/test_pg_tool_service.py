"""pg_tool service, boundary and MCP tests. Offline: no tool makes a provider call (live provider search is planned, not part of 0.1.0), so there is no
Hub to stand in for here. Data comes from tiny synthetic data folders."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
from pg_tool import local_data as ld, service
from pg_tool.validate import errors as schema_errors

ROOT = Path(__file__).resolve().parents[1]
ENVELOPE = service.CONTRACT['$defs']['Envelope']
SECRET = 'ZZCLIENTSECRETTOKENZZ'
OA_TEXT = (f'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale in view of Ives. The {SECRET} widget is taught.\n'
           'Claim 4 is objected to as being dependent upon a rejected base claim, but would be allowable if rewritten in independent form.\n'
           'THIS ACTION IS MADE FINAL.\n')

def check_envelope(tc, env):
    tc.assertEqual(schema_errors(env, ENVELOPE, service.CONTRACT), [], env.get('error'))


class Envelope(unittest.TestCase):
    def test_unknown_tool_and_bad_input(self):
        e = service.call('nope', {}); self.assertEqual(e['status'], 'error')
        e = service.call('pg_template', {'key': '999'}); self.assertEqual(e['error']['code'], 'invalid_input'); check_envelope(self, e)
        e = service.call('pg_trends', {'group_by': 'stratum', 'extra': SECRET}); self.assertEqual(e['error']['code'], 'invalid_input')
        self.assertNotIn(SECRET, json.dumps(e))

    def test_live_provider_search_is_planned_not_offered(self):
        # the contract offers local-frames only; a live source or a live flag is refused as invalid input, and nothing is started
        for req in ({'source': 'odp', 'query': 'x'}, {'source': 'hub-local', 'query': 'x'}, {'source': 'local-frames', 'live': True}):
            e = service.call('pg_search', req); self.assertEqual((e['status'], e['error']['code']), ('error', 'invalid_input'), req); check_envelope(self, e)
        e = service.call('pg_post_grant', {'patent_number': '8000001', 'live': True}); self.assertEqual(e['error']['code'], 'invalid_input')
        self.assertEqual(service.CONTRACT['$defs']['SearchInput']['properties']['source']['enum'], ['local-frames'])
        self.assertIn('planned', service.CONTRACT['tools']['pg_search']['description']); self.assertIn('planned', service.CONTRACT['live_policy'])

    def test_every_template_key(self):
        enum = service.CONTRACT['$defs']['TemplateInput']['properties']['key']['enum']
        self.assertEqual(sorted(enum), sorted(service.tpl.SHELLS))  # every shell is reachable, and nothing else is offered
        for key in service.tpl.SHELLS:
            e = service.call('pg_template', {'key': key, 'include_markdown': False})
            self.assertEqual(e['status'], 'ok', key); check_envelope(self, e)
            d = e['data']; self.assertEqual(len(d['sha256']), 64); self.assertEqual(d['kind'], 'structural-shell')
        e = service.call('pg_template', {'key': '103'})  # the statute-specific evidence templates are not part of this release
        self.assertEqual(e['status'], 'error'); self.assertEqual(e['error']['code'], 'invalid_input'); check_envelope(self, e)

    def test_template_markdown_carries_draft_header(self):
        e = service.call('pg_template', {'key': 'office-action-response'})
        self.assertTrue(e['data']['markdown'].startswith('> **' + service.DRAFT_HEADER)); self.assertEqual(e['data']['draft_header'], service.DRAFT_HEADER)
        e = service.call('pg_template', {'key': 'office-action-response', 'sections': ['1']})
        self.assertTrue(e['data']['markdown'].startswith('> **' + service.DRAFT_HEADER))


class CallerBoundary(unittest.TestCase):
    def test_caller_text_not_echoed_by_default(self):
        e = service.call('pg_assess_office_action', {'text': OA_TEXT, 'matter_ref': 'M-1'})
        check_envelope(self, e)
        self.assertEqual(e['boundary']['lane'], 'caller-supplied'); self.assertEqual(e['boundary']['classification'], 'caller-confidential'); self.assertEqual(e['boundary']['matter'], 'client')
        self.assertFalse(e['boundary']['persisted_by_tool']); self.assertEqual(e['boundary']['live_calls'], 0)
        self.assertEqual(e['matter_ref'], 'M-1')
        self.assertNotIn(SECRET, json.dumps(e))
        an = e['data']['analysis']
        self.assertEqual([r['claims'] for r in an['rejections']], [[1, 2, 3]]); self.assertTrue(an['allowable_subject_matter']['indicated'])
        self.assertEqual(e['data']['action_facts']['finality'], 'final')
        self.assertTrue(e['data']['plan_outline'])
        for p in e['data']['plan_outline']: self.assertNotIn('chosen', json.dumps(p))

    def test_excerpts_opt_in_and_capped(self):
        e = service.call('pg_assess_office_action', {'text': OA_TEXT, 'include_excerpts': True})
        ex = [a['excerpt'] for a in e['data']['parser_annotations'].values() if 'excerpt' in a]
        self.assertTrue(ex); self.assertTrue(all(len(x) <= service.EXCERPT_CAP for x in ex))

    def test_report_banner_and_no_lookups_from_caller_text(self):
        e = service.call('pg_report', {'subject': {'office_action': {'text': OA_TEXT, 'application_number': '99000001'}}})
        check_envelope(self, e)
        self.assertTrue(e['data']['markdown'].startswith('> **' + service.DRAFT_HEADER)); self.assertIn('Confidential matter material', e['data']['markdown']); self.assertFalse(e['data']['contains_caller_text'])
        self.assertNotIn(SECRET, e['data']['markdown'])
        self.assertTrue(any('post-grant lookup skipped' in w for w in e['warnings']))

    def test_file_path_roots(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'oa.txt'; p.write_text(OA_TEXT, encoding='utf-8')
            e = service.call('pg_assess_office_action', {'file_path': str(p)}); self.assertEqual(e['error']['code'], 'out_of_scope_path')
            with mock.patch.dict(os.environ, {'PG_TOOL_INPUT_ROOTS': d}):
                e = service.call('pg_assess_office_action', {'file_path': str(p)}); self.assertEqual(e['status'], 'ok')
                (Path(d) / 'x.pdf').write_bytes(b'%PDF'); e = service.call('pg_assess_office_action', {'file_path': str(Path(d) / 'x.pdf')})
                self.assertEqual(e['error']['code'], 'out_of_scope_path')

    def test_oversize_text(self):
        e = service.call('pg_assess_office_action', {'text': 'a' * (service.MAX_INPUT_BYTES + 1)})
        self.assertEqual(e['error']['code'], 'invalid_input')  # rejected by the contract maxLength before reading


class Mcp(unittest.TestCase):
    def test_initialize_list_call(self):
        msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'pg_template', 'arguments': {'key': 'office-action-response', 'include_markdown': False}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'pg_assess_office_action', 'arguments': {'text': OA_TEXT}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'bogus'}]
        p = subprocess.run([sys.executable, str(ROOT / 'pg_tool_mcp.py')], input='\n'.join(json.dumps(m) for m in msgs) + '\n', capture_output=True, text=True, encoding='utf-8', timeout=120, cwd=tempfile.gettempdir())
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual([o['id'] for o in out], [1, 2, 3, 4, 5]); self.assertEqual(p.stderr, '')
        self.assertEqual(out[0]['result']['serverInfo']['name'], 'patent-genius')
        names = {t['name'] for t in out[1]['result']['tools']}
        self.assertEqual(names, set(service.CONTRACT['tools']))
        self.assertEqual(out[2]['result']['structuredContent']['status'], 'ok'); self.assertFalse(out[2]['result']['isError'])
        self.assertNotIn(SECRET, json.dumps(out[3])); self.assertEqual(out[4]['error']['code'], -32601)


class LocalCorpus(unittest.TestCase):
    """A tiny synthetic data directory (PG_TOOL_DATA_DIR equivalent): one invented application with one normalized office action."""
    APP, DOC = '99000001', 'SYNTHCTNF0001'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / 'corpus' / 'normalized' / self.APP; d.mkdir(parents=True)
        body = 'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Example Reference A in view of Example Reference B.\n' * 20
        (d / f'{self.DOC}.md').write_text('---\ndocument_code: "CTNF"\nofficial_date: "2026-01-02"\n---\n' + body, encoding='utf-8')
        (d / 'index.json').write_text(json.dumps({'documents': {self.DOC: {'markdown': f'{self.DOC}.md', 'code': 'CTNF', 'component': 'office_action'}}}), encoding='utf-8')
        p = mock.patch.object(ld, 'DATA', Path(self.tmp.name)); p.start(); self.addCleanup(p.stop)
        ld._cache.clear(); self.addCleanup(ld._cache.clear)

    def test_assess_local_document_conforms_to_analysis_schema(self):
        e = service.call('pg_assess_office_action', {'application_number': self.APP, 'document_id': self.DOC})
        check_envelope(self, e); self.assertEqual(e['boundary']['lane'], 'public')
        self.assertTrue(e['data']['schema_conformance']['valid'], e['data']['schema_conformance'])
        self.assertEqual([r['claims'] for r in e['data']['analysis']['rejections']], [[1, 2, 3]])

    def test_corpus_read_window(self):
        e = service.call('pg_corpus', {'application_number': self.APP, 'document_id': self.DOC, 'max_chars': 500})
        self.assertEqual(len(e['data']['document']['text']), 500); self.assertEqual(e['data']['document']['next_offset'], 500)

    def test_missing_data_degrades_cleanly(self):
        for tool, req in (('pg_trends', {'group_by': 'stratum'}), ('pg_search', {'source': 'local-frames', 'application_number': self.APP}),
                          ('pg_post_grant', {'application_number': self.APP})):
            e = service.call(tool, req); check_envelope(self, e); self.assertIn(e['status'], ('not_found', 'partial'), tool)
        e = service.call('pg_status', {}); check_envelope(self, e); self.assertFalse(e['data']['live']['enabled']); self.assertIn('planned', e['data']['live']['planned'])

    def test_report_public_has_draft_header(self):
        e = service.call('pg_report', {'subject': {'office_action': {'application_number': self.APP, 'document_id': self.DOC}}})
        check_envelope(self, e); md = e['data']['markdown']
        self.assertTrue(md.startswith('> **' + service.DRAFT_HEADER)); self.assertIn('Not legal advice', md)


if __name__ == '__main__':
    unittest.main()
