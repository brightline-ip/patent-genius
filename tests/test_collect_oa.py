"""Office-action rejection collection: the plan and its estimates, only-your-frames, batches and pages, resume after any stop, the wait and
stop rules (exit 75, provider 429, other 4xx, outage), atomic progress, output files, run records without caller text. The Hub is a stand-in."""
import gzip, json, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

import synthetic as syn
from hubenv import StubHub, provider_row
from pg_tool import collect_oa, fsutil, hubgw, oa_adapter

SCOPE = {'limits': {}, 'data_types': ['oa_rejections']}
APPS = [a[0] for a in syn.APPS]


def rows_for(apps=APPS, many=None):
    out = {}
    for i, a in enumerate(apps):
        r = [provider_row(a, f'D{a}', 'CTNF', '2011-01-10', '103', paragraph=1), provider_row(a, f'D{a}', 'CTNF', '2011-01-10', '', action='objected', paragraph=2)]
        if a == many: r = [provider_row(a, f'D{a}', 'CTNF', '2011-01-10', '103', paragraph=n) for n in range(1, 131)]
        out[a] = r
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.data = self.root / 'data'; self.data.mkdir()
        syn.write_frame(self.data)
        self.sleeps = []

    def hub(self, **kw):
        h = StubHub(self.root / 'hub', **kw); p = h.patch(); p.start(); self.addCleanup(p.stop); return h

    def run_collect(self, **kw):
        kw.setdefault('sleep', self.sleeps.append); kw.setdefault('say', lambda *_: None)
        return collect_oa.run(self.data, SCOPE, scope_sha='a' * 64, **kw)

    def output_rows(self, fid=syn.FRAME_ID):
        with gzip.open(self.data / 'corpus' / 'oa-rejections' / f'{fid}.jsonl.gz', 'rt', encoding='utf-8') as f: return [json.loads(l) for l in f if l.strip()]


class Plan(Base):
    def test_the_plan_comes_from_the_eligible_applications_of_your_frames_only(self):
        extra = [syn.frame_row('10000009', '2010-06-01', eligible=False), syn.frame_row('1234567', '2010-06-02')]    # ineligible, and a 7-digit number
        syn.write_frame(self.data, extra_rows=extra)
        plan = collect_oa.build_plan(self.data, SCOPE)
        f = plan['frames'][0]; self.assertEqual(f['applications'], sorted(APPS))
        self.assertEqual([s['application'] for s in f['skipped']], ['1234567']); self.assertIn('eight digits', f['skipped'][0]['reason'])

    def test_a_cap_in_the_scope_is_applied_and_the_rest_is_listed_not_sent(self):
        plan = collect_oa.build_plan(self.data, {'limits': {'max_applications': 4}})
        f = plan['frames'][0]; self.assertEqual(len(f['applications']), 4); self.assertEqual(len(f['skipped']), 2)
        self.assertTrue(all('max_applications' in s['reason'] for s in f['skipped']))

    def test_estimates_use_the_hubs_own_limits_and_name_their_assumptions(self):
        plan = collect_oa.build_plan(self.data, SCOPE)
        est = collect_oa.estimate(plan, {'hub_version': '0.2.0', 'providers': {'uspto': {'max_calls_per_hour': 1800, 'min_interval_ms': 4000, 'concurrent_calls_per_key': 1}}})
        self.assertEqual(est['requests_per_hour_limit'], 900.0)            # the spacing (4 s) is the tighter limit
        self.assertEqual(est['requests_estimated'], 2); self.assertGreater(est['disk_bytes_compressed_estimated'], 0)
        self.assertTrue(any('unmeasured' in a for a in est['assumptions']))
        self.assertIsNone(collect_oa.estimate(plan, None)['hours_estimated'])


class Run(Base):
    def test_a_full_run_writes_the_rows_exactly_as_sent_and_records_counts_only(self):
        rows = rows_for(many='10000001'); hub = self.hub(rows=rows)
        res = self.run_collect()
        self.assertEqual((res['status'], res['exit']), ('complete', 0)); self.assertEqual(res['requests'], 2)       # 140 rows in one batch = 2 pages
        got = self.output_rows(); self.assertEqual(len(got), 130 + 2 * 5); self.assertIn(rows['10000002'][0], got)    # verbatim, objections kept
        meta = json.loads((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.collected.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['row_kinds'], {'objection': 5, 'rejection': 135}); self.assertEqual(meta['adapter'], 'legacy-rejections-v2'); self.assertEqual(meta['applications_without_rows'], 0)
        sk = json.loads((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.skipped.json').read_text(encoding='utf-8')); self.assertEqual(sk, {'applications': []})
        sent = hub.calls(); self.assertTrue(all(c['route'] == 'oa-rejections' for c in sent))
        self.assertTrue(all(set(c['body']['application_numbers']) <= set(APPS) for c in sent))                  # only applications from your frames
        runs = collect_oa.read_runs(self.data); self.assertEqual(len(runs), 1); self.assertEqual(runs[0]['requests'], 2); self.assertEqual(runs[0]['status'], 'complete')
        text = (self.data / 'collect' / 'runs.jsonl').read_text(encoding='utf-8')
        for a in APPS: self.assertNotIn(a, text)                                                                   # no application number, no text
        self.assertFalse((self.data / 'collect' / 'oa-rejections' / f'{syn.FRAME_ID}.part.jsonl').exists())
        self.assertFalse((self.data / 'locks' / 'collect-oa.lock').exists())

    def test_an_application_with_no_rows_is_a_zero_not_an_error(self):
        self.hub(rows={a: r for a, r in rows_for().items() if a != '10000004'})
        self.assertEqual(self.run_collect()['status'], 'complete')
        meta = json.loads((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.collected.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['applications_without_rows_list'], ['10000004'])

    def test_a_request_cap_stops_and_the_next_run_resumes_without_asking_again(self):
        hub = self.hub(rows=rows_for())
        r1 = self.run_collect(batch_size=2, max_requests=2)
        self.assertEqual((r1['status'], r1['reason'], r1['exit']), ('stopped', 'request_cap', 3)); self.assertEqual(r1['requests'], 2)
        self.assertFalse((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz').exists())            # no output until the frame is whole
        st = collect_oa.status(self.data)['frames'][0]; self.assertEqual((st['status'], st['batches_done'], st['batches_total']), ('stopped', 2, 3))
        r2 = self.run_collect(batch_size=2)
        self.assertEqual(r2['status'], 'complete'); self.assertEqual(hub.call_count(), 3)       # 3 batches in all, none asked twice
        self.assertEqual(len(self.output_rows()), 12)
        again = self.run_collect(batch_size=2); self.assertEqual(again['status'], 'complete'); self.assertEqual(hub.call_count(), 3)   # nothing is collected twice

    def test_a_crash_after_the_last_saved_batch_is_truncated_away_on_resume(self):
        self.hub(rows=rows_for())
        self.run_collect(batch_size=2, max_requests=1)
        part = self.data / 'collect' / 'oa-rejections' / f'{syn.FRAME_ID}.part.jsonl'
        saved = part.stat().st_size
        with open(part, 'ab') as f: f.write(b'{"patentApplicationNumber": "10000')              # half a line written after the last saved progress
        self.assertEqual(self.run_collect(batch_size=2)['status'], 'complete')
        got = self.output_rows(); self.assertEqual(len(got), 12); self.assertGreater(saved, 0)

    def test_progress_is_replaced_whole_never_half_written(self):
        self.hub(rows=rows_for())
        with mock.patch('os.replace', wraps=os.replace) as rp:
            self.run_collect(batch_size=2)
        self.assertTrue(any(str(c.args[1]).endswith('.progress.json') for c in rp.call_args_list))
        for p in (self.data / 'collect' / 'oa-rejections').glob('*.progress.json'): json.loads(p.read_text(encoding='utf-8'))

    def test_exit_75_waits_the_time_asked_then_continues_and_is_not_counted_as_a_request(self):
        self.hub(rows=rows_for(), script={'1': {'exit': 75, 'error': {'code': 'local_limit', 'message': 'cap', 'retry_after_seconds': 61}}})
        res = self.run_collect()
        self.assertEqual(res['status'], 'complete'); self.assertEqual(sum(self.sleeps), 61); self.assertEqual(res['requests'], 1)

    def test_a_wait_longer_than_allowed_stops_with_progress_kept(self):
        self.hub(rows=rows_for(), script={'1': {'exit': 75, 'error': {'code': 'local_limit', 'message': 'cap', 'retry_after_seconds': 7200}}})
        res = self.run_collect(max_wait_s=3600)
        self.assertEqual((res['reason'], res['exit']), ('limit_wait_too_long', 3)); self.assertEqual(self.sleeps, [])

    def test_a_provider_429_waits_at_least_five_seconds_and_backs_off(self):
        self.hub(rows=rows_for(), script={'1': {'status': 429, 'retryAfterSeconds': 0}, '2': {'status': 429, 'retryAfterSeconds': 0}, '3': {'status': 429, 'retryAfterSeconds': 30}})
        res = self.run_collect()
        self.assertEqual(res['status'], 'complete')
        self.assertTrue(all(s >= 5 for s in self.sleeps)); self.assertEqual(self.sleeps[:3], [5.0, 10.0, 30.0])      # never sooner than 5 s; doubling; Retry-After honoured when longer

    def test_endless_429_stops_the_run_and_keeps_progress(self):
        self.hub(rows=rows_for(), script={str(i): {'status': 429} for i in range(1, 12)})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('rate_limited', 3)); self.assertTrue(all(s >= 5 for s in self.sleeps))

    def test_any_other_4xx_stops_at_once(self):
        hub = self.hub(rows=rows_for(), script={'1': {'envelope': {'text': '{"message":"no"}', 'status': 403, 'contentType': 'application/json'}}})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('client_error', 1)); self.assertEqual(hub.call_count(), 1); self.assertEqual(self.sleeps, [])

    def test_a_404_is_not_read_as_no_records(self):
        self.hub(rows=rows_for(), script={'1': {'envelope': {'text': '{}', 'status': 404, 'contentType': 'application/json'}}})
        self.assertEqual(self.run_collect()['reason'], 'client_error')
        self.assertFalse((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz').exists())

    def test_three_failures_in_a_row_are_an_outage(self):
        err = {'exit': 1, 'error': {'code': 'error', 'message': 'upstream down'}}
        hub = self.hub(rows=rows_for(), script={'1': err, '2': err, '3': err})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('outage', 3)); self.assertEqual(hub.call_count(), 3)
        self.assertEqual(self.run_collect()['status'], 'complete')          # and it can simply be started again

    def test_a_provider_5xx_counts_towards_an_outage_but_a_success_resets_it(self):
        e5 = {'envelope': {'text': '{}', 'status': 502, 'contentType': 'application/json'}}
        self.hub(rows=rows_for(), script={'1': e5, '2': e5})
        self.assertEqual(self.run_collect(batch_size=2)['status'], 'complete')

    def test_a_response_that_is_not_the_documented_shape_stops_the_run(self):
        self.hub(rows=rows_for(), script={'1': {'envelope': {'text': '{"unexpected": 1}', 'status': 200, 'contentType': 'application/json'}}})
        self.assertEqual(self.run_collect()['reason'], 'bad_response')

    def test_a_missing_key_is_reported_before_anything_is_sent(self):
        self.hub(rows=rows_for(), script={'1': {'exit': 2, 'error': {'code': 'not_configured', 'message': 'USPTO key missing', 'missing': ['USPTO_API_KEY']}}})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('not_configured', 2))

    def test_a_limiter_state_failure_is_a_stop_not_a_retry(self):
        hub = self.hub(rows=rows_for(), script={'1': {'exit': 1, 'error': {'code': 'local_state', 'message': 'limiter state unreadable'}}})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('local_state', 1)); self.assertEqual(hub.call_count(), 1); self.assertEqual(self.sleeps, [])

    def test_an_older_hub_is_hub_too_old(self):
        self.hub(rows=rows_for(), old_hub=True)
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('hub_too_old', 78)); self.assertIn('0.2.0', res['message'])

    def test_no_hub_at_all_is_exit_78(self):
        with mock.patch.dict(os.environ, {'PG_HUB_DISCOVER': '0'}):
            for k in ('PG_HUB_CMD', 'PG_HUB_ROOT', 'PG_TOOL_HUB_ROOT', 'PG_HUB_TEST_STUB'): os.environ.pop(k, None)
            res = self.run_collect()
        self.assertEqual((res['reason'], res['exit']), ('hub_unavailable', 78))

    def test_a_changed_frame_is_refused_until_restart(self):
        self.hub(rows=rows_for())
        self.run_collect(batch_size=2, max_requests=1)
        syn.write_frame(self.data, extra_rows=[syn.frame_row('10000007', '2010-08-01')])
        res = self.run_collect(batch_size=2); self.assertEqual((res['reason'], res['exit']), ('plan_changed', 1))
        res = self.run_collect(batch_size=2, restart=True); self.assertEqual(res['status'], 'complete')

    def test_one_collector_at_a_time(self):
        self.hub(rows=rows_for())
        lock = self.data / 'locks' / 'collect-oa.lock'; lock.parent.mkdir(parents=True); lock.write_text('{}', encoding='utf-8')
        res = self.run_collect(); self.assertEqual(res['reason'], 'locked')
        old = lock.stat().st_mtime - 3600; os.utime(lock, (old, old))            # a lock nobody refreshed for an hour is taken over
        self.assertEqual(self.run_collect()['status'], 'complete')

    def test_the_cap_in_the_scope_and_the_option_both_apply(self):
        self.hub(rows=rows_for())
        res = collect_oa.run(self.data, {'limits': {'max_requests_per_run': 1}}, scope_sha=None, batch_size=2, sleep=self.sleeps.append, say=lambda *_: None)
        self.assertEqual((res['reason'], res['requests']), ('request_cap', 1))


class Completeness(Base):
    def page(self, found, rows):
        return {'envelope': {'text': json.dumps({'response': {'numFound': found, 'start': 0, 'docs': rows}}), 'status': 200, 'contentType': 'application/json'}}

    def test_a_batch_that_arrives_short_is_never_saved_as_complete(self):
        hundred = [provider_row('10000001', 'D1', 'CTNF', '2011-01-10', '103', paragraph=n) for n in range(1, 101)]
        hub = self.hub(rows=rows_for(), script={'1': self.page(250, hundred), '2': self.page(250, [])})        # 250 reported, 100 delivered, then nothing
        res = self.run_collect()
        self.assertEqual((res['status'], res['reason'], res['exit']), ('stopped', 'bad_response', 1)); self.assertIn('250 rows', res['message']); self.assertIn('100', res['message'])
        self.assertFalse((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz').exists())
        prog = collect_oa.read_progress(self.data / 'collect' / 'oa-rejections' / f'{syn.FRAME_ID}.progress.json'); self.assertEqual((prog['next_batch'], prog['rows'], prog['status']), (0, 0, 'stopped'))
        self.assertEqual(hub.call_count(), 2)
        again = self.run_collect(); self.assertEqual(again['status'], 'complete')                                  # nothing half-saved: the next run does the batch in full

    def test_hitting_the_page_cap_with_rows_missing_is_a_stop_too(self):
        self.hub(rows=rows_for(many='10000001'))                                                                    # 140 rows = 2 pages
        with mock.patch.object(collect_oa, 'MAX_PAGES_PER_BATCH', 1):
            res = self.run_collect()
        self.assertEqual((res['reason'], res['exit']), ('bad_response', 1)); self.assertFalse((self.data / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz').exists())

    def test_numfound_is_stored_per_batch_in_the_progress_file(self):
        self.hub(rows=rows_for(many='10000001'))
        self.assertEqual(self.run_collect(batch_size=2)['status'], 'complete')
        prog = collect_oa.read_progress(self.data / 'collect' / 'oa-rejections' / f'{syn.FRAME_ID}.progress.json')
        self.assertEqual(prog['batches_found'], {'0': 132, '1': 4, '2': 4})                                        # 130 + 2 rows, then 2 + 2, then 2 + 2


class LocalWaits(Base):
    def test_a_zero_second_retry_is_not_obeyed_literally(self):
        self.hub(rows=rows_for(), script={'1': {'exit': 75, 'error': {'code': 'local_limit', 'message': 'cap', 'retry_after_seconds': 0}}})
        self.assertEqual(self.run_collect()['status'], 'complete'); self.assertEqual(self.sleeps, [collect_oa.MIN_LOCAL_WAIT_S])

    def test_endless_local_waits_stop_the_run(self):
        step = {'exit': 75, 'error': {'code': 'local_busy', 'message': 'busy', 'retry_after_seconds': 0}}
        hub = self.hub(rows=rows_for(), script={str(i): step for i in range(1, 200)})
        res = self.run_collect(); self.assertEqual((res['reason'], res['exit']), ('limit_wait_loop', 3)); self.assertEqual(hub.call_count(), collect_oa.MAX_LOCAL_WAITS_IN_A_ROW + 1)


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.path = Path(self.tmp.name) / 'locks' / 'collect-oa.lock'

    def old(self, seconds=100000):
        t = time.time() - seconds; os.utime(self.path, (t, t))

    def test_stale_time_is_longer_than_a_gateway_call_can_take(self):
        self.assertGreater(collect_oa.STALE_LOCK_S, hubgw.CALL_TIMEOUT_S)

    def test_a_heartbeat_keeps_the_lock_fresh_while_the_run_lives(self):
        lock = collect_oa.RunLock(self.path, stale_s=100, heartbeat_s=0.05); lock.acquire(); self.addCleanup(lock.release)
        self.old(1000); time.sleep(0.5)
        self.assertLess(time.time() - self.path.stat().st_mtime, 5)                                                # refreshed by the heartbeat thread, with no call to touch()
        with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path, stale_s=100).acquire()
        self.assertEqual(c.exception.reason, 'locked')

    def test_a_stale_lock_whose_process_is_alive_is_not_taken_over(self):
        self.path.parent.mkdir(parents=True); self.path.write_text(json.dumps({'pid': os.getpid(), 'token': 'x'})); self.old()
        with self.assertRaises(collect_oa.Stop) as c: collect_oa.RunLock(self.path, stale_s=10).acquire()
        self.assertEqual(c.exception.reason, 'locked'); self.assertIn('still running', str(c.exception)); self.assertTrue(self.path.exists())

    def test_a_stale_lock_whose_process_is_gone_is_taken_over(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass']); gone.wait()
        self.path.parent.mkdir(parents=True); self.path.write_text(json.dumps({'pid': gone.pid, 'token': 'old'})); self.old()
        lock = collect_oa.RunLock(self.path, stale_s=10); lock.acquire(); self.addCleanup(lock.release)
        self.assertEqual(json.loads(self.path.read_text())['token'], lock.token)
        self.assertFalse(fsutil.pid_alive(gone.pid)); self.assertTrue(fsutil.pid_alive(os.getpid())); self.assertFalse(fsutil.pid_alive(None)); self.assertFalse(fsutil.pid_alive(-5))

    def test_release_removes_only_a_lock_this_run_wrote(self):
        lock = collect_oa.RunLock(self.path); lock.acquire(); self.path.write_text(json.dumps({'pid': 1, 'token': 'someone-else'})); lock.release()
        self.assertTrue(self.path.exists()); self.assertEqual(json.loads(self.path.read_text())['token'], 'someone-else')
        mine = collect_oa.RunLock(self.path.with_name('b.lock')); mine.acquire(); mine.release(); self.assertFalse(self.path.with_name('b.lock').exists())
        self.assertFalse(mine._thread.is_alive())


class Probe(Base):
    def test_the_probe_reads_the_field_list_and_sends_only_an_application_of_yours(self):
        hub = self.hub(rows=rows_for(), fields=['hasRej101', 'legalSectionCode'])
        out = collect_oa.probe(self.data)
        self.assertEqual(out['requests'], 1); self.assertIn('hasRej103', out['fields_missing_that_this_tool_reads'])
        out = collect_oa.probe(self.data, '99999999'); self.assertIn('not in your frames', out['application'])
        out = collect_oa.probe(self.data, '10000001'); self.assertEqual(out['application_result']['row_kinds'], {'rejection': 1, 'objection': 1})
        self.assertEqual(out['application_result']['statutes_seen'], ['103'])
        self.assertEqual([c['body'].get('application_numbers') for c in hub.calls() if c['body'].get('action') == 'records'], [['10000001']])


class Adapter(unittest.TestCase):
    def test_statute_level_only_and_labels(self):
        a = oa_adapter.get()
        n = a.normalise
        self.assertEqual([n({'legalSectionCode': c})['statute'] for c in ('102', '103', '112', 'Other', '', '112(b)', '35 U.S.C. 101', None)], ['102', '103', '112', 'other', None, '112', '101', None])
        self.assertEqual([n({'actionTypeCategory': c})['row_kind'] for c in ('rejected', 'objected', 'allowed', 'cancelled', '', 'zz')], ['rejection', 'objection', 'allowed', 'cancelled', 'unlabelled', 'other'])
        r = n({'patentApplicationNumber': '10000001', 'cite103EQ1': 1, 'cite103GT3': 2, 'hasRejDP': 1, 'hasRej101': '1', 'submissionDate': '2011-01-10T00:00:00'})
        self.assertEqual((r['cite103_eq1'], r['cite103_gt3'], r['has_rejdp'], r['has_rej101'], r['date']), (1, 2, 1, 1, '2011-01-10'))
        with self.assertRaises(oa_adapter.AdapterError): oa_adapter.get('nope')
        with self.assertRaises(oa_adapter.AdapterError): a.parse_page({'text': '{"response": {"numFound": 3}}'})        # no docs: never "zero records"
        self.assertEqual(a.parse_page({'text': '{"response": {"numFound": 0, "docs": []}}'}), (0, []))


if __name__ == '__main__':
    unittest.main()
