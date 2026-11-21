"""Regression tests for the second review round: the sanitiser (ids and every derived string, the invisible-character set, provider descriptions), the
collector lock (crash takeover, race, lost lock) and the page-cap stop, compaction (ASCII front matter, one merged failures file, an unreadable file),
share denominators and the freeze-date range, the signal handlers and part-file cleanup in the Node gateway client, and the stale wording."""
import contextlib, hashlib, io, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import compact
import lowfloor
import pg_db
from pg_tool import collect_oa, fsutil, pack, service, similar, untrusted
from test_event_codes import folder
from test_review_fixes import Base

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
sha = lambda b: hashlib.sha256(b).hexdigest()
HOSTILE_OA = ('Claim Rejections - 35 USC 103\n\nClaims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over \x1b[31m\u202eEvilRef (US 2099/9999901) '
              'in view of \u200bZero\u2060Width\u00adSoft.\nTHIS ACTION IS MADE FINAL.\n')
BAD_CHARS = ('\x1b', '\u202e', '\u200b', '\u2060', '\u00ad', '\u2028', '\ufeff', '\u061c', '\U000e0041', '\x00')


def dead_pid():
    p = subprocess.Popen([sys.executable, '-c', 'pass']); p.wait(); return p.pid


class Sanitiser(Base):
    def test_every_invisible_or_format_character_is_replaced(self):
        cps = [0x200b, 0x200c, 0x200d, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xfeff, 0x00ad, 0x061c, 0xe0000, 0xe0041, 0xe007f, 0x202e, 0x2066, 0x2028, 0x2029, 0x7f, 0x85]
        for cp in cps: self.assertEqual(untrusted.clean('a' + chr(cp) + 'b'), 'a b', hex(cp))
        self.assertEqual(untrusted.clean('plain, ordinary text with accents: café'), 'plain, ordinary text with accents: café')
        self.assertEqual(untrusted.clean_line('a\n\t b\r\nc'), 'a b c')

    def test_a_citation_that_starts_with_esc_and_bidi_controls_leaves_no_bad_character_anywhere(self):
        for tool, req in (('pg_assess_office_action', {'text': HOSTILE_OA, 'include_excerpts': True}), ('pg_report', {'subject': {'office_action': {'text': HOSTILE_OA}}}),
                          ('pg_response_pack', {'text': HOSTILE_OA, 'min_score': 0.0})):
            e = service.call(tool, req); self.assertIn(e['status'], ('ok', 'partial'), (tool, e.get('error')))
            text = json.dumps(e, ensure_ascii=False)
            for bad in BAD_CHARS: self.assertNotIn(bad, text, (tool, hex(ord(bad[0]))))
            for esc in ('\\u001b', '\\u202e', '\\u200b', '\\u2060', '\\u00ad'): self.assertNotIn(esc, json.dumps(e, ensure_ascii=True), (tool, esc))
        e = service.call('pg_assess_office_action', {'text': HOSTILE_OA}); an = e['data']['analysis']
        ids = [r['id'] for r in an['references']]; self.assertTrue(ids)
        for i in ids: self.assertTrue(i == untrusted.clean_line(i, 60), i)                                   # ids built from the cleaned citation
        for r in an['rejections']:
            for i in r.get('reference_ids', []): self.assertIn(i, ids); self.assertEqual(i, untrusted.clean_line(i, 60))
        for step in e['data']['plan_outline']:
            for s in [step['title']] + step['steps'] + step['open_questions']: self.assertEqual(s, untrusted.clean(s, 600))

    def test_the_coverage_reports_provider_descriptions_are_cleaned(self):
        with tempfile.TemporaryDirectory() as t:
            d = folder(t); fid = next((d / 'corpus' / 'frame-events').glob('*.event-codes.json'))
            fid.write_text(json.dumps({'ZZZZ': 'evil \x1b[2J\u202e description \u200b with controls'}), encoding='utf-8')
            pg_db.build(data_dir=str(d), say=lambda *_: None); out = pg_db.report(d / 'corpus' / 'pg.db', 'coverage', min_n=1)
            self.assertIn('evil', out)
            for bad in BAD_CHARS: self.assertNotIn(bad, out)

    def test_the_database_report_and_search_say_the_text_is_untrusted(self):
        self.assertIn('pg_db_report', service.UNTRUSTED_TEXT_TOOLS); self.assertIn('pg_search', service.UNTRUSTED_TEXT_TOOLS)
        e = service.call('pg_db_report', {'report': 'coverage'}); self.assertEqual(e['status'], 'ok', e.get('error')); self.assertIn(untrusted.CAVEAT, e['caveats'])
        e = service.call('pg_search', {'source': 'local-frames'}); self.assertIn(e['status'], ('ok', 'partial', 'not_found')); self.assertIn(untrusted.CAVEAT, e['caveats'])


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.path = Path(self.tmp.name) / 'locks' / 'collect-oa.lock'

    def write(self, pid, token='old', age=0):
        self.path.parent.mkdir(parents=True, exist_ok=True); self.path.write_text(json.dumps({'pid': pid, 'token': token}))
        if age: t = time.time() - age; os.utime(self.path, (t, t))

    def test_a_dead_process_and_a_lock_older_than_about_60_seconds_is_taken_over(self):
        self.assertEqual(collect_oa.CRASHED_LOCK_S, 60); self.assertGreater(collect_oa.STALE_LOCK_S, 600)
        self.write(dead_pid(), age=70)
        lock = collect_oa.RunLock(self.path); lock.acquire(); self.addCleanup(lock.release)                           # the default stale time is 30 minutes: this is the crash rule
        self.assertEqual(json.loads(self.path.read_text())['token'], lock.token)
        self.assertEqual([p.name for p in self.path.parent.glob('*.taken-*')], [])

    def test_a_dead_process_but_a_fresh_lock_is_refused_and_the_message_names_the_lock_file(self):
        self.write(dead_pid(), age=20)
        with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path).acquire()
        self.assertEqual(c.exception.reason, 'locked'); self.assertIn(str(self.path), str(c.exception))
        self.write(os.getpid(), age=70)                                                                              # a live process: never taken over before the stale time
        with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path).acquire()
        self.assertIn(str(self.path), str(c.exception)); self.assertEqual(json.loads(self.path.read_text())['pid'], os.getpid())
        self.write(os.getpid(), age=100000)
        with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path, stale_s=10).acquire()
        self.assertIn('still running', str(c.exception)); self.assertIn(str(self.path), str(c.exception))

    def test_a_lock_replaced_between_the_check_and_the_takeover_is_put_back(self):
        pid = dead_pid(); self.write(pid, token='B', age=100)                                                      # what is on disk when the rename happens
        real = collect_oa.RunLock._read_file; calls = []
        def fake(path):
            calls.append(str(path))
            return ({'pid': pid, 'token': 'A'}, True) if len(calls) <= 2 else real(path)                          # what the checks saw: another lock, which was replaced before the rename
        with mock.patch.object(collect_oa.RunLock, '_read_file', staticmethod(fake)):
            with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path).acquire()
        self.assertEqual(c.exception.reason, 'locked'); self.assertEqual(json.loads(self.path.read_text())['token'], 'B')   # the live lock is back
        self.assertEqual([p.name for p in self.path.parent.glob('*.taken-*')], [])

    def test_two_runs_cannot_both_take_over_a_stale_lock(self):
        for _ in range(5):
            self.write(dead_pid(), age=100)
            locks = [collect_oa.RunLock(self.path) for _ in range(8)]; won, lost, errs = [], [], []
            gate = threading.Barrier(len(locks))
            def go(l):
                gate.wait()
                try: l.acquire(); won.append(l)
                except collect_oa.Stop as s: lost.append(s.reason)
                except Exception as e: errs.append(repr(e))
            ts = [threading.Thread(target=go, args=(l,)) for l in locks]
            for t in ts: t.start()
            for t in ts: t.join(30)
            for l in locks: l.release()
            self.assertEqual(errs, []); self.assertEqual(len(won), 1, (len(won), lost)); self.assertEqual(set(lost), {'locked'})
            for p in list(self.path.parent.glob('*')): p.unlink()

    def test_a_run_whose_lock_was_taken_stops_before_its_next_request(self):
        lock = collect_oa.RunLock(self.path, heartbeat_s=0.05); lock.acquire(); self.addCleanup(lock.release)
        self.assertFalse(lock.lost); lock.check()
        self.path.write_text(json.dumps({'pid': os.getpid(), 'token': 'someone-else'}))
        deadline = time.time() + 5
        while not lock.lost and time.time() < deadline: time.sleep(0.05)
        self.assertTrue(lock.lost)                                                                                    # the heartbeat saw it
        with self.assertRaises(collect_oa.Stop) as c: lock.check()
        self.assertEqual(c.exception.reason, 'lock_lost'); self.assertIn(str(self.path), str(c.exception))
        gw = mock.Mock(); col = collect_oa.Collector(self.tmp.name, SimpleNamespace(route='x'), gw, sleep=lambda s: None, say=lambda *_: None, lock=lock)
        with self.assertRaises(collect_oa.Stop) as c: col.request({'a': 1})
        self.assertEqual(c.exception.reason, 'lock_lost'); gw.call.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())['token'], 'someone-else')                                 # release() does not remove a lock it did not write
        lock.release(); self.assertTrue(self.path.exists())
        self.path.unlink(); l2 = collect_oa.RunLock(self.path, heartbeat_s=0.05); l2.acquire(); self.addCleanup(l2.release); self.path.unlink()           # removed: lost as well
        deadline = time.time() + 5
        while not l2.lost and time.time() < deadline: time.sleep(0.05)
        self.assertTrue(l2.lost)


class PageCap(unittest.TestCase):
    def adapter(self, found, per_page):
        a = mock.Mock(); a.page_rows = per_page; a.request.return_value = {}
        a.parse_page.side_effect = lambda env: (found, [{'k': 1}] * per_page); return a

    def test_a_page_cap_stop_is_not_resumable_and_says_so(self):
        gw = mock.Mock(); gw.call.return_value = {'status': 200}
        col = collect_oa.Collector('.', self.adapter(10000, 100), gw, sleep=lambda s: None, say=lambda *_: None)
        with mock.patch.object(collect_oa, 'MAX_PAGES_PER_BATCH', 2):
            with self.assertRaises(collect_oa.Stop) as c: col.collect_batch(['1'])
        self.assertEqual(c.exception.reason, 'bad_response'); self.assertFalse(c.exception.resumable); self.assertIn('page limit', str(c.exception))
        a = mock.Mock(); a.page_rows = 100; a.request.return_value = {}; seq = iter([(250, [{'k': 1}] * 100), (250, [])]); a.parse_page.side_effect = lambda env: next(seq)
        col = collect_oa.Collector('.', a, gw, sleep=lambda s: None, say=lambda *_: None)
        with self.assertRaises(collect_oa.Stop) as c: col.collect_batch(['1'])                                         # numFound 250, 100 arrived, then nothing: may be retried later
        self.assertTrue(c.exception.resumable); self.assertNotIn('page limit', str(c.exception))


class Compaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.app = self.root / 'public-originals' / '16000001'; self.run = self.app / 'ocr' / 'r1'; self.run.mkdir(parents=True)
        self.out = self.root / 'corpus' / 'normalized' / '16000001'

    def ocr(self, doc, text=b'page one\fpage two', tamper=False, run=None):
        run = run or self.run; run.mkdir(parents=True, exist_ok=True); (run / f'{doc}.txt').write_bytes(text)
        meta = {'document_id': doc, 'original_sha256': sha(b'pdf'), 'text_sha256': sha(b'WRONG' if tamper else text), 'pages': [{'page': 1, 'status': 'ocr_ok'}, {'page': 2, 'status': 'ocr_ok'}]}
        (run / f'{doc}.pages.json').write_text(json.dumps(meta))

    def test_front_matter_is_ascii_json_in_the_ocr_path_too(self):
        run = self.app / 'ocr' / 'ré\u202e1'; self.ocr('DOC1', run=run)
        compact.normalize(self.app, run.name, root=self.root)
        md = (self.out / 'DOC1.md').read_text(encoding='utf-8'); front = md.split('---')[1]
        self.assertTrue(front.isascii(), front); self.assertIn('ocr_run: "r\\u00e9\\u202e1"', front); self.assertNotIn('\u202e', md)
        for line in front.strip().splitlines(): self.assertRegex(line, r'^[a-z_0-9]+: ".*"$')
        import xml_to_md
        self.assertTrue(''.join(xml_to_md._front({'source_locator': 'a"b\n\u202ecé'})).isascii())

    def test_one_merged_failures_file_cleared_by_a_clean_run_with_a_count_and_a_failing_exit(self):
        self.ocr('DOC1', tamper=True); self.ocr('DOC2')
        err = io.StringIO()
        with contextlib.redirect_stderr(err): compact.normalize(self.app, 'r1', root=self.root)
        path = self.out / 'compact-failures.json'; self.assertEqual(list(json.loads(path.read_text())['failed']), ['DOC1'])
        (self.app / 'BAD.tar').mkdir()                                                                                # an unreadable "archive": read_bytes raises
        with contextlib.redirect_stderr(err): done = compact.normalize_xml(self.app, root=self.root)
        self.assertEqual(done, []); self.assertEqual(sorted(json.loads(path.read_text())['failed']), ['BAD', 'DOC1'])  # merged, not replaced
        self.ocr('DOC1')                                                                                              # fixed
        with contextlib.redirect_stderr(err): compact.normalize(self.app, 'r1', root=self.root)
        self.assertEqual(list(json.loads(path.read_text())['failed']), ['BAD'])
        (self.app / 'BAD.tar').rmdir()
        with contextlib.redirect_stderr(err): compact.normalize_xml(self.app, root=self.root)
        self.assertFalse(path.exists())                                                                               # a clean run removes the file
        self.assertEqual([p.name for p in self.out.glob('*.tmp*')], [])

    def test_the_command_line_reports_the_failed_count_and_exits_non_zero(self):
        self.ocr('DOC1', tamper=True); self.ocr('DOC2')
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'compact.py'), 'normalize', str(self.app), 'r1', '--data-dir', str(self.root)], capture_output=True, text=True, encoding='utf-8', cwd=str(self.root))
        self.assertEqual(p.returncode, 1, p.stderr); self.assertEqual(json.loads(p.stdout)['failed'], 1); self.assertIn('DOC1 skipped', p.stderr)
        self.ocr('DOC1')
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'compact.py'), 'normalize', str(self.app), 'r1', '--data-dir', str(self.root)], capture_output=True, text=True, encoding='utf-8', cwd=str(self.root))
        self.assertEqual(p.returncode, 0, p.stderr); self.assertEqual(json.loads(p.stdout)['failed'], 0); self.assertFalse((self.out / 'compact-failures.json').exists())


class Statistics(Base):
    def setUp(self):
        super().setUp(); self.addCleanup(lowfloor.patch_floor())
        self.db2 = Path(self.tmp.name) / 'copy.db'; shutil.copy(self.db, self.db2)
        con = sqlite3.connect(self.db2)
        con.execute("INSERT INTO frames (frame_id, frozen_at) VALUES ('other', '2025-06-01T00:00:00Z')"); con.execute("UPDATE memberships SET frame_id='other' WHERE rowid % 2 = 0")
        con.commit(); con.close()

    def test_interview_and_amendment_shares_use_actions_with_a_recognised_next_event(self):
        res = similar.find(self.db, {'statutes': ['103', '102']}, weights=similar.effective_weights(None), min_score=0.0, min_n=1); lab = res['applicant_action_labels']
        n = lab['actions_with_a_recognised_next_event']
        self.assertGreater(lab['families_with_anchored_event_data'], n)                                               # some anchored actions have no recognised next event
        for k in ('interview_before_next', 'amendment_before_next', 'rce_or_appeal_next'): self.assertEqual(lab[k]['n'], n, k)
        self.assertIn('may still come', lab['share_basis']); self.assertIn('whose next event is recognised', similar.ACTION_NOTE)

    def test_as_of_reports_the_freeze_date_range_of_the_matched_families(self):
        one = similar.find(self.db, {'statutes': ['103', '102']}, weights=similar.effective_weights(None), min_score=0.0, min_n=1)['outcome_summary']
        self.assertEqual((one['as_of_from'], one['as_of_to'], one['as_of']), ('2026-01-01', '2026-01-01', '2026-01-01'))
        res = similar.find(self.db2, {'statutes': ['103', '102']}, weights=similar.effective_weights(None), min_score=0.0, min_n=1); o = res['outcome_summary']
        self.assertEqual((o['as_of_from'], o['as_of_to']), ('2025-06-01', '2026-01-01'))
        lines = pack.similar_lines(res, 1); self.assertTrue(any('freeze dates of their frames, 2025-06-01 to 2026-01-01' in x for x in lines), lines)


@unittest.skipUnless(NODE, 'node not available')
class NodeGateway(unittest.TestCase):
    def node(self, code):
        return subprocess.run([NODE, '--input-type=module', '-e', code], capture_output=True, text=True, encoding='utf-8', cwd=str(ROOT), timeout=60)

    def test_importing_the_gateway_client_installs_no_signal_handler_and_does_not_exit(self):
        r = self.node("import('./hub_gateway.mjs').then(() => console.log('LISTENERS=' + process.listenerCount('SIGINT') + ',' + process.listenerCount('SIGTERM') + ',' + process.listenerCount('exit')))")
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn('LISTENERS=0,0,0', r.stdout)
        src = (ROOT / 'hub_gateway.mjs').read_text(encoding='utf-8'); self.assertNotIn('process.exit(130)', src)

    def test_part_files_of_dead_processes_are_swept_and_a_live_ones_kept(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); (d / f'A.pdf.part-{dead_pid()}').write_bytes(b'x'); (d / f'B.pdf.part-{os.getpid()}').write_bytes(b'x'); (d / 'C.pdf').write_bytes(b'x'); (d / 'D.pdf.partial').write_bytes(b'x')
            r = self.node(f"import {{ sweepPartFiles }} from './hub_documents.mjs'; console.log('REMOVED=' + sweepPartFiles({json.dumps(str(d))}))")
            self.assertEqual(r.returncode, 0, r.stderr); self.assertIn('REMOVED=1', r.stdout)
            self.assertEqual(sorted(p.name for p in d.iterdir()), sorted([f'B.pdf.part-{os.getpid()}', 'C.pdf', 'D.pdf.partial']))
            data = b'hello'; r = self.node(f"import {{ stageVerified }} from './hub_documents.mjs'; const x = stageVerified({json.dumps(str(d))}, 'E', Buffer.from('hello'), '{sha(data)}', 'pdf'); console.log('OK=' + x.bytes)")
            self.assertIn('OK=5', r.stdout, r.stderr); self.assertTrue((d / 'E.pdf').exists())


class StaleWording(unittest.TestCase):
    def test_the_contract_says_5_everywhere_min_n_appears(self):
        c = json.loads((ROOT / 'pg_tool' / 'contract.json').read_text(encoding='utf-8'))
        found = []
        def walk(n):
            if isinstance(n, dict):
                for k, v in n.items():
                    if k == 'min_n' and isinstance(v, dict) and 'minimum' in v: found.append(v)
                    else: walk(v)
            elif isinstance(n, list):
                for v in n: walk(v)
        walk(c); self.assertGreaterEqual(len(found), 4)
        for v in found: self.assertEqual(v['minimum'], 5); self.assertNotIn('Default: 1', v.get('description', ''))
        self.assertEqual(service.call('pg_trends', {'group_by': 'stratum', 'min_n': 4})['error']['code'], 'invalid_input')

    def test_the_documents_say_what_the_code_does(self):
        doc = (ROOT / 'docs' / 'SIMILAR.md').read_text(encoding='utf-8')
        for t in ('mailing', 'mail_exact', 'ct_near', 'never used', 'simultaneous', 'inclusive', 'precedence', 'rescinded abandonment', 'skipped', 'as_of_from', 'recognised'): self.assertIn(t, doc, t)
        self.assertNotIn('within 3 days', doc)
        for p in ('README.md', 'SECURITY.md'): self.assertIn('`pg_corpus` returns', (ROOT / p).read_text(encoding='utf-8').replace('**`pg_corpus`', '`pg_corpus`').replace('`pg_corpus` returns a local', '`pg_corpus` returns'), p)
        self.assertIn('raw', (ROOT / 'SECURITY.md').read_text(encoding='utf-8')); self.assertNotIn('+-3 days', (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'))
        self.assertIn('## 0.1.0 - 2026-11-21 (UTC)', (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'))
        db = (ROOT / 'pg_db.py').read_text(encoding='utf-8'); self.assertNotIn('rejection mix, actions, interviews, RCEs, appeals', db)
        examiners = pg_db.REPORTS['examiners'][0]; self.assertIn('command line also shows', examiners)
        xml = (ROOT / 'xml_to_md.py').read_text(encoding='utf-8'); self.assertIn('ASCII only', xml)

    def test_a_dead_process_is_not_alive_and_this_one_is(self):
        self.assertTrue(fsutil.pid_alive(os.getpid())); self.assertFalse(fsutil.pid_alive(dead_pid()))


if __name__ == '__main__':
    unittest.main()
