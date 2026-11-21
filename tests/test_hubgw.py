"""pg_tool/hubgw.py: the only way Patent Genius reaches a provider is the Hub's gateway command as a short-lived child. The Hub here is a
stand-in (tests/gateway_stub.py): no real Hub, no key, no network."""
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from hubenv import StubHub, clean_environ
from pg_tool import hubgw

ROOT = Path(__file__).resolve().parents[1]


def env_with(**extra):
    return clean_environ(**extra)


class Finding(unittest.TestCase):
    def test_the_stand_in_needs_its_own_flag_and_never_runs_beside_a_key(self):
        stub = json.dumps([sys.executable, 'x'])
        with self.assertRaises(hubgw.HubGatewayError) as c: hubgw.find_hub(env_with(PG_HUB_TEST_STUB=stub))
        self.assertEqual(c.exception.code, 'test_stub_refused')
        for key in ('USPTO_API_KEY', 'COURTLISTENER_TOKEN', 'EPO_OPS_CONSUMER_KEY', 'SOMETHING_SECRET'):
            with self.assertRaises(hubgw.HubGatewayError, msg=key): hubgw.find_hub(env_with(PG_HUB_TEST_STUB=stub, PG_ALLOW_TEST_STUB='1', **{key: 'k'}))
        self.assertTrue(hubgw.find_hub(env_with(PG_HUB_TEST_STUB=stub, PG_ALLOW_TEST_STUB='1'))['stub'])
        with self.assertRaises(hubgw.HubGatewayError): hubgw.find_hub(env_with(PG_HUB_TEST_STUB='not json', PG_ALLOW_TEST_STUB='1'))

    def test_the_replacement_module_setting_has_no_effect(self):
        self.assertIsNone(hubgw.find_hub(env_with(FRAME_GATEWAY_MODULE='C:/evil.mjs', PG_GATEWAY_MODULE='C:/evil.mjs')))

    def test_order_of_discovery(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / 'hub'; root.mkdir(); (root / 'Hub.cmd').write_text('@echo off', encoding='utf-8')
            self.assertEqual(hubgw.find_hub(env_with(PG_HUB_CMD='C:/x/Hub.cmd', PG_HUB_ROOT=str(root)))['source'], 'PG_HUB_CMD')
            self.assertEqual(hubgw.find_hub(env_with(PG_HUB_ROOT=str(root)))['source'], 'PG_HUB_ROOT')
            self.assertEqual(hubgw.find_hub(env_with(PG_TOOL_HUB_ROOT=str(root)))['source'], 'PG_HUB_ROOT')
            self.assertFalse(hubgw.find_hub(env_with(PG_HUB_ROOT=str(Path(t) / 'none')))['exists'])
            default = Path(t) / 'Brightline' / 'PatentResearchHub'; default.mkdir(parents=True); (default / 'Hub.cmd').write_text('@echo off', encoding='utf-8')
            base = {k: v for k, v in env_with().items() if k != 'PG_HUB_DISCOVER'}
            found = hubgw.find_hub({**base, 'LOCALAPPDATA': t})
            if os.name == 'nt': self.assertEqual(found['source'], 'default install folder')
            self.assertIsNone(hubgw.find_hub({**base, 'LOCALAPPDATA': t, 'PG_HUB_DISCOVER': '0'}))

    def test_no_hub_is_a_clear_error_and_starts_nothing(self):
        with mock.patch.dict(os.environ, env_with(), clear=True):
            with self.assertRaises(hubgw.HubGatewayError) as c: hubgw.call('oa-rejections', {'action': 'fields'})
        self.assertEqual(c.exception.code, 'hub_unavailable'); self.assertIn('PG_HUB_CMD', str(c.exception))


class Calls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.dir = Path(self.tmp.name)

    def test_an_envelope_comes_back_and_the_request_is_what_was_sent(self):
        hub = StubHub(self.dir, rows={'10000001': [{'patentApplicationNumber': '10000001'}]})
        with hub.patch():
            env = hubgw.call('oa-rejections', {'action': 'records', 'application_numbers': ['10000001'], 'start': 0, 'rows': 100})
        self.assertEqual(env['status'], 200); self.assertEqual(json.loads(env['text'])['response']['numFound'], 1)
        sent = hub.calls()[0]; self.assertEqual(sent['route'], 'oa-rejections'); self.assertEqual(sent['body']['application_numbers'], ['10000001'])

    def test_exit_codes_map_to_error_codes(self):
        script = {'1': {'exit': 75, 'error': {'code': 'local_limit', 'message': 'hourly cap', 'retry_after_seconds': 12.5}},
                  '2': {'exit': 75, 'error': {'code': 'local_busy', 'message': 'busy', 'retry_after_seconds': 0}},
                  '9': {'exit': 75, 'error': {'code': 'local_state', 'message': 'limiter state unreadable'}}, '3': {'exit': 2, 'error': {'code': 'not_configured', 'message': 'no key', 'missing': ['USPTO_API_KEY'], 'register': 'https://example.invalid', 'terms': 'https://example.invalid/t'}},
                  '4': {'exit': 64, 'error': {'code': 'bad_request', 'message': 'bad'}}, '5': {'exit': 1, 'error': {'code': 'error', 'message': 'boom'}},
                  '6': {'raw': '', 'exit': 64}, '7': {'raw': 'not json at all', 'exit': 1}, '8': {'status': 429, 'retryAfterSeconds': 7}}
        hub = StubHub(self.dir, script=script)
        got = []
        with hub.patch():
            for i in range(1, 10):
                try: got.append(('ok', hubgw.call('oa-rejections', {'action': 'fields'})))
                except hubgw.HubGatewayError as e: got.append((e.code, e))
        self.assertEqual([g[0] for g in got], ['local_limit', 'local_busy', 'not_configured', 'bad_request', 'gateway_error', 'hub_too_old', 'hub_too_old', 'ok', 'local_state'])
        self.assertEqual(got[0][1].retry_after_seconds, 12.5); self.assertEqual(got[1][1].retry_after_seconds, 0.0)
        self.assertEqual(got[2][1].detail['missing'], ['USPTO_API_KEY'])
        self.assertIn('0.2.0', str(got[5][1]))
        self.assertEqual(got[7][1]['status'], 429)     # a provider 429 is RETURNED inside a successful envelope, not raised
        self.assertEqual(got[7][1]['retryAfterSeconds'], 7)

    def test_limits_and_an_older_hub(self):
        with StubHub(self.dir / 'a').patch(): lim = hubgw.limits()
        self.assertEqual(lim['hub_version'], '0.2.0'); per_hour, gap = hubgw.uspto_limits(lim)
        self.assertEqual(per_hour, 1800); self.assertEqual(gap, 1.0)
        self.assertEqual(hubgw.uspto_limits({'providers': {'uspto': {'max_calls_per_hour': 0, 'min_interval_ms': 500}}})[0], 7200)   # 0 means no hourly cap: spacing decides
        self.assertEqual(hubgw.uspto_limits({'providers': {'uspto': {'max_calls_per_hour': 100, 'min_interval_ms': 500}}})[0], 100)
        with StubHub(self.dir / 'b', old_hub=True).patch():
            with self.assertRaises(hubgw.HubGatewayError) as c: hubgw.limits()
        self.assertEqual(c.exception.code, 'hub_too_old')
        with StubHub(self.dir / 'c', limits={'hub_version': '0.1.9'}).patch():
            with self.assertRaises(hubgw.HubGatewayError) as c: hubgw.limits()
        self.assertEqual(c.exception.code, 'hub_too_old')

    def test_a_big_answer_is_read_in_full(self):
        stub = self.dir / 'big.py'
        stub.write_text("import json, sys\nsys.stdin.read()\nsys.stdout.write(json.dumps({'text': 'x' * 6000000, 'status': 200, 'contentType': 'application/json'}))\n", encoding='utf-8')
        with mock.patch.dict(os.environ, env_with(PG_HUB_TEST_STUB=json.dumps([sys.executable, str(stub)]), PG_ALLOW_TEST_STUB='1'), clear=True):
            env = hubgw.call('application-pdf', {'x': 1})
        self.assertEqual(len(env['text']), 6000000)

    def test_a_timeout_stops_the_whole_process_tree_and_says_the_call_may_have_happened(self):
        marker = self.dir / 'grandchild-finished.txt'
        stub = self.dir / 'slow.py'
        stub.write_text("import os, subprocess, sys, time\nsys.stdin.read()\n"
                        "subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(4); open(sys.argv[1], \"w\").write(\"x\")', os.environ['MARKER']])\ntime.sleep(60)\n", encoding='utf-8')
        with mock.patch.dict(os.environ, env_with(PG_HUB_TEST_STUB=json.dumps([sys.executable, str(stub)]), PG_ALLOW_TEST_STUB='1', MARKER=str(marker)), clear=True):
            t0 = time.time()
            with self.assertRaises(hubgw.HubGatewayError) as c: hubgw.call('uspto', {'x': 1}, timeout=1.5)
        self.assertEqual(c.exception.code, 'timeout'); self.assertTrue(c.exception.detail.get('may_have_happened')); self.assertIn('MAY have happened', str(c.exception))
        self.assertLess(time.time() - t0, 15)
        time.sleep(6)
        self.assertFalse(marker.exists(), 'the grandchild process survived the timeout')

    def test_ctrl_c_during_a_call_kills_the_whole_gateway_tree(self):
        marker = self.dir / 'grandchild-after-interrupt.txt'
        stub = self.dir / 'slow2.py'
        stub.write_text("import os, subprocess, sys, time\nsys.stdin.read()\n"
                        "subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(4); open(sys.argv[1], \"w\").write(\"x\")', os.environ['MARKER']])\ntime.sleep(60)\n", encoding='utf-8')
        real =subprocess.Popen.communicate
        def interrupted(self_, *a, **k):
            if 'taskkill' in str(self_.args): return real(self_, *a, **k)   # only the gateway child is interrupted, not the tool that stops it
            time.sleep(1.5); raise KeyboardInterrupt()          # the user presses Ctrl+C while the child is running
        with mock.patch.dict(os.environ, env_with(PG_HUB_TEST_STUB=json.dumps([sys.executable, str(stub)]), PG_ALLOW_TEST_STUB='1', MARKER=str(marker)), clear=True):
            with mock.patch.object(subprocess.Popen, 'communicate', interrupted):
                with self.assertRaises(KeyboardInterrupt): hubgw.call('uspto', {'x': 1}, timeout=60)
        self.assertEqual(hubgw._LIVE, set())
        time.sleep(6); self.assertFalse(marker.exists(), 'the grandchild survived Ctrl+C')

    def test_the_stand_in_child_never_sees_a_key(self):
        stub = self.dir / 'env.py'
        stub.write_text("import json, os, sys\nsys.stdin.read()\nprint(json.dumps({'status': 200, 'text': json.dumps(sorted(k for k in os.environ if 'KEY' in k)), 'contentType': 'x'}))\n", encoding='utf-8')
        with mock.patch.dict(os.environ, env_with(PG_HUB_TEST_STUB=json.dumps([sys.executable, str(stub)]), PG_ALLOW_TEST_STUB='1'), clear=True):
            self.assertEqual(json.loads(hubgw.call('uspto', {'x': 1})['text']), [])

    def test_the_module_never_reads_a_key_file(self):
        src = (ROOT / 'pg_tool' / 'hubgw.py').read_text(encoding='utf-8')
        self.assertNotRegex(src, r'(?<![A-Za-z])open\('); self.assertNotIn('read_text', src); self.assertNotIn('read_bytes', src)
        self.assertNotIn('PATENT_RESEARCH_DATA_DIR', src)       # the Hub's limiter location is the Hub's business


if __name__ == '__main__':
    unittest.main()
