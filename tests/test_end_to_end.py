"""One synthetic run through the whole path, as a user would run it, in a temporary data folder: scope, collect (with the Hub stand-in), database
build, assess, similar, pack and shell. Every command is the real command line in its own process. No real Hub, key, network or per-user folder."""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

import synthetic as syn
from hubenv import StubHub, clean_environ, provider_row as pr
from test_oa_db import TEXT_1

ROOT = Path(__file__).resolve().parents[1]
OA_TEXT = ('Application No. 10/000,002   Art Unit 1234\nExaminer: Roe, Rick   Notification Date: 05/06/2012\n\nClaim Rejections - 35 USC 103\n'
           'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale (US 2019/0000001) in view of Ives. ZZE2EMARKERZZ\n'
           'Claim Rejections - 35 USC 102\nClaims 4-5 are rejected under 35 U.S.C. 102 as being anticipated by Kay (US 7,000,001).\nTHIS ACTION IS MADE FINAL.\n')


def provider_rows():
    rows = {}
    rows['10000001'] = [pr('10000001', 'D1', 'CTNF', '2011-01-10', '102', paragraph=1), pr('10000001', 'D1', 'CTNF', '2011-01-10', '103', paragraph=2)]
    rows['10000002'] = [pr('10000002', 'D2', 'CTNF', '2011-02-10', '103', paragraph=1), pr('10000002', 'D2b', 'CTFR', '2011-08-10', '102', paragraph=1), pr('10000002', 'D2b', 'CTFR', '2011-08-10', '103', paragraph=2)]
    rows['10000003'] = [pr('10000003', 'D3', 'CTNF', '2011-03-10', '101', paragraph=1, aliceIndicator=1)]
    rows['10000004'] = [pr('10000004', 'D4', 'CTNF', '2012-03-10', '103', paragraph=1), pr('10000004', 'D4', 'CTNF', '2012-03-10', '102', paragraph=2)]
    rows['10000005'] = [pr('10000005', 'D5', 'CTNF', '2012-05-10', '103', paragraph=1)]
    return rows                                                                    # 10000006 has no office-action rows at all


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.data = self.root / 'data'; self.fake_local = self.root / 'localappdata'; self.fake_local.mkdir()
        self.hub = StubHub(self.root / 'hub', rows=provider_rows())
        self.env = self.hub.env(LOCALAPPDATA=str(self.fake_local), HOME=str(self.fake_local), XDG_DATA_HOME=str(self.fake_local), PYTHONDONTWRITEBYTECODE='1')

    def pg(self, *argv, ok=(0,), env=None):
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'tests' / 'pg_cli_lowfloor.py'), *argv, '--data-dir', str(self.data)] if argv[0] != 'shell' else [sys.executable, '-B', str(ROOT / 'tests' / 'pg_cli_lowfloor.py'), *argv],
                           capture_output=True, text=True, encoding='utf-8', env=env or self.env, cwd=str(self.root), timeout=180)
        self.assertIn(p.returncode, ok, f'{argv}: {p.stdout[-1500:]} {p.stderr[-800:]}')
        return p

    def test_the_whole_path(self):
        # 1. scope
        self.pg('scope', 'init')
        scope = json.loads((self.data / 'scope.json').read_text(encoding='utf-8'))
        scope['data_types'] = ['frames', 'events', 'oa_rejections']; scope['limits'] = {'max_applications': 100, 'max_requests_per_run': 50}
        (self.data / 'scope.json').write_text(json.dumps(scope), encoding='utf-8'); self.pg('scope', 'validate')
        # your frames, events and one full-text action (these come from the frame and document tools in real use)
        syn.write_frame(self.data); syn.write_events(self.data)
        ev = {'10000002': [['2011-02-10', 'CTNF'], ['2011-08-10', 'CTFR'], ['2012-02-01', 'MABN2']], '10000003': [['2011-03-10', 'CTNF'], ['2011-04-01', 'A...'], ['2011-05-01', 'RCEX'], ['2011-06-01', 'MN/=.']]}
        syn.gz_jsonl(self.data / 'corpus' / 'frame-events' / 'batch-one.jsonl.gz', [{'application_number': a, 'examiner': syn.EXAMINER, 'art_unit': syn.ART_UNIT, 'parents': [], 'events': e} for a, e in
                     {**{'10000001': [['2011-01-10', 'CTNF'], ['2011-02-01', 'EXAT'], ['2011-03-01', 'MN/=.']], '10000004': [['2012-03-10', 'CTNF']], '10000005': [['2012-05-10', 'CTNF'], ['2012-09-01', 'MN/=.']]}, **ev}.items()])
        syn.write_document(self.data, '10000001', 'DOC00000001', 'CTNF', '2011-01-11', TEXT_1)

        # 2. collect: the plan reads the Hub's limits and sends nothing; run needs --yes
        plan = json.loads(self.pg('collect', 'plan').stdout)
        self.assertEqual((plan['applications'], plan['batches']), (6, 1)); self.assertEqual(plan['requests_per_hour_limit'], 1800.0); self.assertEqual(self.hub.call_count(), 0)
        refused = self.pg('collect', 'run', ok=(64,)); self.assertIn('--yes', refused.stderr); self.assertEqual(self.hub.call_count(), 0)
        out = self.pg('collect', 'run', '--yes').stdout; res = json.loads(out[out.rindex('\n{') + 1:])
        self.assertEqual(res['status'], 'complete'); self.assertEqual(self.hub.call_count(), 1)
        sent = self.hub.calls()[0]; self.assertEqual(sent['route'], 'oa-rejections'); self.assertEqual(sorted(sent['body']['application_numbers']), sorted(a[0] for a in syn.APPS))
        status = json.loads(self.pg('collect', 'status').stdout); self.assertEqual(status['frames'][0]['status'], 'finished'); self.assertEqual(status['runs'][-1]['status'], 'complete')
        self.assertEqual(self.pg('collect', 'run', '--yes').returncode, 0); self.assertEqual(self.hub.call_count(), 1)       # already collected: nothing is asked twice

        # 3. database
        self.pg('db', 'build'); cov = self.pg('db', 'report', 'coverage').stdout
        self.assertIn('office-action rows: rejection', cov); self.assertIn('parser vs provider, statute 103', cov)

        # 4. assess, similar, pack, shell on an office action you hold
        oa = self.root / 'inputs' / 'oa.txt'; oa.parent.mkdir(); oa.write_text(OA_TEXT, encoding='utf-8')
        assessed = json.loads(self.pg('assess', str(oa)).stdout); self.assertEqual(sorted({r['statute'] for r in assessed['data']['analysis']['rejections']}), ['102', '103'])
        sim = json.loads(self.pg('similar', str(oa), '--min-score', '0.3', '--min-n', '1', '--application', '10000002').stdout)
        self.assertEqual(sim['status'], 'ok'); d = sim['data']; self.assertNotIn('10000002', {c['application'] for c in d['candidates']})
        self.assertEqual(d['candidates'][0]['application'], '10000001'); self.assertIn('references', {k for k, v in d['candidates'][0]['components'].items() if v['available']})
        self.assertGreaterEqual(d['outcome_summary']['matched_families'], 2); self.assertNotIn('ZZE2EMARKERZZ', json.dumps(sim))
        packfile = self.root / 'packs' / 'pack.json'
        self.pg('pack', str(oa), '--min-score', '0.3', '--min-n', '1', '--application', '10000002', '--out', str(packfile))
        pk = json.loads(packfile.read_text(encoding='utf-8')); self.assertTrue(pk['boundary']['persisted_by_tool']); self.assertEqual(pk['data']['pack']['facts']['finality'], 'final')
        shell = self.pg('shell', 'office-action-response', '--from-pack', str(packfile)).stdout
        self.assertIn('R1: 35 USC 103 rejection of claims 1-3.', shell); self.assertIn('Mail date: 2012-05-06 (read from the text; verify). No deadline computed.', shell); self.assertNotIn('ZZE2EMARKERZZ', shell)
        db = json.loads(self.pg('similar', '--features', '{"statutes": ["101"]}', '--min-score', '0.3', '--min-n', '1').stdout); self.assertEqual(db['data']['candidates'][0]['application'], '10000003')

        # 5. what stayed where: the real per-user folders were never touched, and only your own files were written
        self.assertEqual(list(self.fake_local.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ['collect', 'corpus', 'locks', 'scope.json'])
        self.assertEqual(sorted(p.name for p in (self.root / 'packs').iterdir()), ['pack.json'])

    def test_collect_refuses_without_the_scope_saying_oa_rejections(self):
        self.pg('scope', 'init')
        p = self.pg('collect', 'plan', ok=(64,)); self.assertIn('oa_rejections', p.stderr); self.assertEqual(self.hub.call_count(), 0)

    def test_an_older_hub_is_reported_clearly_by_the_command_line(self):
        self.pg('scope', 'init'); scope = json.loads((self.data / 'scope.json').read_text(encoding='utf-8')); scope['data_types'] = ['oa_rejections']
        (self.data / 'scope.json').write_text(json.dumps(scope), encoding='utf-8'); syn.write_frame(self.data)
        env = StubHub(self.root / 'old', old_hub=True).env(LOCALAPPDATA=str(self.fake_local)); p = self.pg('collect', 'run', '--yes', ok=(78,), env=env)
        self.assertIn('hub_too_old', p.stdout + p.stderr); self.assertIn('0.2.0', p.stdout + p.stderr)
        doc = self.pg('doctor', '--json', env=env, ok=(0,)); self.assertIn('hub_too_old', json.loads(doc.stdout)['hub']['problem'])


if __name__ == '__main__':
    unittest.main()
