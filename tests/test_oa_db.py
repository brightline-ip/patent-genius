"""The database from collected office-action rows: statute-level mapping, labelled rows, the provider flags, outcomes derived from events,
the parsed full-text documents and their link to the provider rows, collection_runs, and the coverage report. All data is synthetic."""
import gzip, json, sqlite3, tempfile, unittest
from pathlib import Path

import pg_db
import synthetic as syn
from hubenv import provider_row as pr
from pg_tool import collect_oa, oa_outcomes

TEXT_1 = ('Application No. 10/000,001   Art Unit 1234\nExaminer: Roe, Rick   Notification Date: 01/12/2011\n\n'
          'Claim Rejections - 35 USC 103\nClaims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale (US 2019/0000001) in view of Ives. THIS ACTION IS MADE FINAL.\n')


def build_folder(root):
    d = Path(root); syn.write_frame(d)
    rows = []
    rows += [pr('10000001', 'D1', 'CTNF', '2011-01-10', '102', paragraph=1), pr('10000001', 'D1', 'CTNF', '2011-01-10', '103', paragraph=2),
             pr('10000001', 'D1', 'CTNF', '2011-01-10', 'Other', paragraph=3), pr('10000001', 'D1', 'CTNF', '2011-01-10', '', action='objected', paragraph=4)]
    rows += [pr('10000002', 'D2', 'CTNF', '2011-02-10', 'Other', paragraph=1, hasRejDP=1), pr('10000002', 'D2b', 'CTFR', '2011-08-10', '103', paragraph=1)]
    rows += [pr('10000003', 'D3', 'CTNF', '2011-03-10', '101', paragraph=1, aliceIndicator=1)]
    rows += [pr('10000004', 'D4', 'CTNF', '2012-03-10', '112(b)', paragraph=1)]
    rows += [pr('10000005', 'D5', 'CTNF', '2012-05-10', '103', action='allowed', paragraph=1)]       # an action with no rejection at all
    rows += [pr('99999999', 'DX', 'CTNF', '2012-05-10', '103')]                                          # not in any of your frames
    syn.gz_jsonl(d / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz', rows)
    (d / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.collected.json').write_text(json.dumps({'adapter': 'legacy-rejections-v2'}), encoding='utf-8')
    ev = {'10000001': [['2011-01-10', 'CTNF'], ['2011-02-01', 'EXAT'], ['2011-03-01', 'MN/=.']],
          '10000002': [['2011-02-10', 'CTNF'], ['2011-08-10', 'CTFR'], ['2012-02-01', 'MABN2']],
          '10000003': [['2011-03-10', 'CTNF'], ['2011-04-01', 'A...'], ['2011-05-01', 'RCEX'], ['2011-06-01', 'MN/=.']],
          '10000004': [['2012-03-10', 'CTNF']], '10000005': [['2012-05-10', 'CTNF']]}
    syn.gz_jsonl(d / 'corpus' / 'frame-events' / f'{syn.FRAME_ID}.jsonl.gz', [{'application_number': a, 'examiner': syn.EXAMINER, 'art_unit': syn.ART_UNIT, 'parents': [], 'events': e} for a, e in ev.items()])
    syn.write_document(d, '10000001', 'DOC00000001', 'CTNF', '2011-01-12', TEXT_1)                        # two days after the provider's date: linked
    syn.write_document(d, '10000002', 'DOC00000002', 'CTNF', '2011-02-20', TEXT_1)                        # ten days after: not linked
    collect_oa.record_run(d, {'run_id': 'abc123', 'kind': 'oa-rejections', 'started_at': '2026-01-01T00:00:00+00:00', 'finished_at': '2026-01-01T00:01:00+00:00', 'scope_id': 'f' * 64,
                              'requests': 4, 'applications': 5, 'status': 'complete', 'note': 'adapter legacy-rejections-v2'})
    return d


class OaDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.data = build_folder(cls.tmp.name)
        cls.report = pg_db.build(data_dir=str(cls.data), say=lambda *_: None)
        cls.con = sqlite3.connect(f'file:{(cls.data / "corpus" / "pg.db").as_posix()}?mode=ro', uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close(); cls.tmp.cleanup()

    def q(self, sql, *a): return self.con.execute(sql, a).fetchall()

    def test_schema_version_and_steps(self):
        self.assertEqual(self.q("SELECT value FROM meta WHERE key='version'"), [('2.3',)])
        self.assertEqual(self.report['status']['outcomes'], 'loaded'); self.assertEqual(self.report['status']['parse'], 'loaded')

    def test_statute_is_statute_level_and_rows_are_labelled(self):
        self.assertEqual(self.q("SELECT statute, row_kind FROM oa_paragraphs WHERE application='10000001' ORDER BY paragraph_number"),
                         [('102', 'rejection'), ('103', 'rejection'), ('other', 'rejection'), (None, 'objection')])
        self.assertEqual(self.q("SELECT statute FROM oa_paragraphs WHERE application='10000004'"), [('112',)])         # 112(b) is read at statute level
        self.assertEqual(self.q("SELECT COUNT(*) FROM oa_paragraphs WHERE application='99999999'"), [(0,)])

    def test_an_action_with_no_rejection_row_is_an_action_with_no_statute(self):
        self.assertEqual(self.q("SELECT statute_set, rej103 FROM office_actions WHERE application='10000005'"), [('', 0)])      # an 'allowed' row is not a 103 rejection

    def test_provider_flags_are_kept_and_double_patenting_comes_from_its_flag_on_a_rejected_row(self):
        self.assertEqual(self.q("SELECT has_rejdp FROM oa_paragraphs WHERE application='10000002' AND oa_document='D2'"), [(1,)])
        self.assertEqual(self.q("SELECT rejdp, statute_set FROM office_actions WHERE application='10000002' AND oa_document='D2'"), [(1, 'dp')])
        self.assertEqual(self.q("SELECT has_rej103 FROM oa_paragraphs WHERE application='10000001' AND paragraph_number=2"), [(1,)])
        self.assertEqual(self.q("SELECT statute_set, alice FROM office_actions WHERE application='10000003'"), [('101', 1)])
        self.assertEqual(self.q("SELECT statute_set, is_final FROM office_actions WHERE application='10000001'"), [('102,103', 0)])

    def test_examiner_is_the_one_on_the_application_record_now(self):
        self.assertEqual(self.q("SELECT DISTINCT examiner FROM office_actions"), [(syn.EXAMINER,)])

    def test_outcomes_come_from_events_only_and_follow_the_stated_rules(self):
        got = {(a, d): rest for a, d, *rest in self.q('SELECT application, oa_document, next_event, next_event_date, interview_before_next, amendment_before_next, rce_or_appeal_next, application_outcome FROM oa_outcomes')}
        self.assertEqual(got[('10000001', 'D1')], ['MN/=.', '2011-03-01', 1, 0, 0, 'granted'])
        self.assertEqual(got[('10000002', 'D2')], ['CTFR', '2011-08-10', 0, 0, 0, 'abandoned'])
        self.assertEqual(got[('10000002', 'D2b')], ['MABN2', '2012-02-01', 0, 0, 0, 'abandoned'])
        self.assertEqual(got[('10000003', 'D3')], ['RCEX', '2011-05-01', 0, 1, 1, 'granted'])
        self.assertEqual(got[('10000004', 'D4')], [None, None, 0, 0, None, 'abandoned'])         # nothing followed yet: no next event, no verdict on rce_or_appeal

    def test_derive_handles_same_day_events_and_unknown_codes(self):
        ev = [('2020-01-01', 0, 'CTNF'), ('2020-01-01', 1, 'EXIN'), ('2020-01-01', 2, 'XXXX'), ('2020-02-01', 3, 'N/AP')]
        d = oa_outcomes.derive(ev, '2020-01-01', 'CTNF', 'pending')
        self.assertEqual((d['next_event'], d['interview_before_next'], d['rce_or_appeal_next']), ('N/AP', 0, 1))      # an interview on the anchor's own date is not counted as later
        self.assertIsNone(oa_outcomes.derive(ev, None, 'CTNF', 'pending'))
        self.assertEqual(oa_outcomes.statute_level('112(b)'), '112'); self.assertEqual(oa_outcomes.statute_level('double_patenting_nonstatutory'), 'dp'); self.assertIsNone(oa_outcomes.statute_level('other'))

    def test_full_text_documents_are_parsed_and_linked_by_application_code_and_date(self):
        self.assertEqual(self.q('SELECT oa_document, document_id, day_diff FROM oa_links'), [('D1', 'DOC00000001', 2)])           # the 10-day gap is not linked
        row = self.q("SELECT statute_set, art_unit, examiner, mail_date, parser_version FROM oa_parsed WHERE oa_document='DOC00000001'")[0]
        self.assertEqual(row[0], '103'); self.assertEqual(row[1], '1234'); self.assertEqual(row[3], '2011-01-12'); self.assertEqual(row[4], '1.1.0')
        cites = self.q("SELECT reference_normalized FROM oa_citations WHERE oa_document='DOC00000001' ORDER BY reference")
        self.assertIn(('US20190000001',), cites); self.assertIn((None,), cites)               # a surname-only reference has no document number and is never matched

    def test_collection_runs_are_loaded_with_counts_only(self):
        self.assertEqual(self.q('SELECT run_id, kind, requests, applications, status FROM collection_runs'), [('abc123', 'oa-rejections', 4, 5, 'complete')])

    def test_the_coverage_report_shows_labels_agreement_and_the_link_rate(self):
        out = pg_db.report(self.data / 'corpus' / 'pg.db', 'coverage', min_n=1)
        self.assertIn('office-action rows: rejection', out); self.assertIn('office-action rows: objection', out); self.assertIn('office-action rows: allowed', out)
        self.assertIn('parser vs provider, statute 102', out); self.assertIn('provider only 1', out)
        self.assertIn('office actions with a full-text document', out)

    def test_report_data_suppresses_small_groups_and_rejects_unknown_names(self):
        db = self.data / 'corpus' / 'pg.db'
        r = pg_db.report_data(db, 'overview', min_n=3); self.assertEqual((r['suppressed'], len(r['rows'])), (0, 2))        # two groups of 3 applications
        r = pg_db.report_data(db, 'overview', min_n=4); self.assertEqual((r['suppressed'], len(r['rows'])), (2, 0))        # both are below 4: left out even though this report's SQL does not filter
        with self.assertRaises(ValueError): pg_db.report_data(self.data / 'corpus' / 'pg.db', 'nope')

    def test_a_build_with_no_events_says_outcomes_were_not_collected(self):
        with tempfile.TemporaryDirectory() as t:
            d = build_folder(t)
            for p in (d / 'corpus' / 'frame-events').glob('*'): p.unlink()
            rep = pg_db.build(data_dir=str(d), say=lambda *_: None)
            self.assertEqual(rep['status']['outcomes'], 'not collected'); self.assertIn('oa_outcomes', [m['dataset'] for m in rep['missing']])


if __name__ == '__main__':
    unittest.main()
