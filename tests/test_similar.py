"""The similar-office-action finder: honest scoring (missing = 0, tiers first), document numbers only, own family left out, one case per family,
open cases apart, intervals, applicant steps kept apart from outcomes, versioned and validated weights, nothing from the query text echoed."""
import json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_db
import lowfloor
import synthetic as syn
from pg_tool import local_data as ld, service, similar, stats as stats_mod
from test_oa_db import build_folder

QUERY = {'statutes': ['102', '103'], 'references': ['US20190000001'], 'examiner': 'ROE, RICK', 'art_unit': '1234', 'cpc': 'A01B1/00', 'finality': 'non_final'}


def extend(db):
    """Add synthetic applications and office actions to the built database, to exercise ranking and the summaries."""
    con = sqlite3.connect(db)
    def app(a, fam, au, ex, cpc, outcome, year=2012):
        con.execute('INSERT INTO applications (application, filing_year, art_unit, examiner, outcome, family_key, first_cpc) VALUES (?,?,?,?,?,?,?)', (a, year, au, ex, outcome, fam, cpc))
        con.execute("INSERT INTO memberships VALUES (?,?,?)", (a, 'batch-one', 'agri'))
    def oa(a, doc, date, au, ex, grp, sub, final, st, **kw):
        flags = {s: int(s in st.split(',')) for s in ('101', '102', '103', '112', 'dp')}
        con.execute('INSERT INTO office_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (a, doc, 'CTFR' if final else 'CTNF', date, au, ex, 'agri', grp, sub, None if final is None else int(final), st,
                    flags['101'], flags['102'], flags['103'], flags['112'], flags['dp'], kw.get('alice', 0), kw.get('mayo', 0), 0))
    def nxt(a, doc, event, interview=0, amend=0, rce=None, outcome=None, quality='mail_exact'):
        con.execute('INSERT OR REPLACE INTO oa_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (a, doc, event, '2013-01-01' if event else None, interview, amend, rce, outcome, quality, 'CTNF', '2012-01-01',
                    'recognised' if event else 'no_later_event', 1 if event else 0, None))
    # same family as 10000002 (pending) so two applications count once
    app('10000006x', '10000002', '1234', 'ROE, RICK', 'A01B1/00', 'pending'); oa('10000006x', 'F1', '2012-01-01', '1234', 'ROE, RICK', 'A01B1', 'A01B', 1, '103'); nxt('10000006x', 'F1', None)
    app('20000001', 'FAM-A', '1234', None, 'A01B1/00', 'granted'); oa('20000001', 'P1', '2012-02-01', '1234', None, 'A01B1', 'A01B', 0, '102,103'); nxt('20000001', 'P1', 'MN/=.', 1, 0, 0)
    app('20000002', 'FAM-B', None, None, None, 'abandoned'); oa('20000002', 'P2', '2012-03-01', None, None, None, None, 0, '102,103'); nxt('20000002', 'P2', 'CTFR', 0, 1, 0)
    app('20000003', 'FAM-C', '1235', None, 'A01B1/00', 'granted'); oa('20000003', 'P3', '2012-04-01', '1235', None, 'A01B1', 'A01B', 0, '102,103'); nxt('20000003', 'P3', 'RCEX', 0, 1, 1)
    app('20000004', 'FAM-D', '1234', None, 'A01B9/00', 'pending'); oa('20000004', 'P4', '2012-05-01', '1234', None, 'A01B9', 'A01B', 0, '102,103'); nxt('20000004', 'P4', None)
    app('20000005', 'FAM-E', '1234', 'ROE, RICK', 'A01B1/00', 'granted'); oa('20000005', 'P5', '2012-06-01', '1234', 'ROE, RICK', 'A01B1', 'A01B', 0, '102'); nxt('20000005', 'P5', 'MN/=.', 0, 0, 0)   # 102 only
    app('20000006', 'FAM-F', '1234', 'SMITH, SAM', 'G01N1/00', 'abandoned'); oa('20000006', 'P6', '2012-07-01', '1234', 'SMITH, SAM', 'G01N1', 'G01N', 1, '112'); nxt('20000006', 'P6', 'MABN2')   # shares no statute
    app('20000007', 'FAM-G', '1234', None, 'A01B1/00', 'granted'); oa('20000007', 'P7', '2012-08-01', '1234', None, 'A01B1', 'A01B', None, '102,103'); nxt('20000007', 'P7', 'MN/=.', 0, 0, 0)   # no examiner and no finality: a thin picture
    con.execute("INSERT INTO skipped_applications VALUES ('20000005', 'batch-one', 'oa', 'test: left out of every denominator')")
    con.commit(); con.close()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(lowfloor.patch_floor())   # tiny synthetic data: the floor of 5 has its own test
        cls.tmp = tempfile.TemporaryDirectory(); cls.data = build_folder(cls.tmp.name)
        pg_db.build(data_dir=str(cls.data), say=lambda *_: None); cls.db = cls.data / 'corpus' / 'pg.db'; extend(cls.db)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def find(self, q=QUERY, **kw):
        kw.setdefault('weights', similar.effective_weights(self.data)); kw.setdefault('min_score', 0.3); kw.setdefault('min_n', 1)
        return similar.find(self.db, q, **kw)

    def ids(self, res): return [(c['application'], c['oa_document']) for c in res['candidates']]


class Scoring(Base):
    def test_every_component_of_the_best_match_is_explained_and_missing_ones_score_zero(self):
        res = self.find(exclude_applications=['10000002'])
        top = res['candidates'][0]; self.assertEqual((top['application'], top['tier']), ('10000001', 'full'))
        comp = top['components']
        self.assertTrue(all(comp[c]['available'] and comp[c]['value'] == 1.0 for c in ('statute_set', 'references', 'examiner', 'art_unit', 'cpc', 'finality')))
        self.assertFalse(comp['ground_101']['available']); self.assertEqual(comp['ground_101']['value'], 0.0)       # no 101 on either side: counts nothing
        self.assertAlmostEqual(top['score'], 0.95, places=4); self.assertAlmostEqual(top['coverage'], 0.95, places=4)
        thin = next(c for c in res['candidates'] if c['application'] == '20000002')
        self.assertEqual(thin['tier'], 'partial'); self.assertFalse(thin['components']['examiner']['available'])
        self.assertAlmostEqual(thin['score'], 0.35 + 0.05, places=4)                                                  # statutes and finality only: the rest is 0, not averaged away

    def test_ranking_is_by_coverage_tier_first_then_score(self):
        res = self.find(exclude_applications=['10000001'], min_score=0.3)
        order = [(c['application'], c['tier']) for c in res['candidates']]
        tiers = [t for _, t in order]; rank = {'full': 0, 'high': 1, 'partial': 2, 'thin': 3}
        self.assertEqual(tiers, sorted(tiers, key=rank.get))
        by = {c['application']: c for c in res['candidates']}
        self.assertGreater(by['20000001']['score'], by['20000002']['score'])           # a candidate with more of the picture ranks above one with a thin picture
        high_scores = [c['score'] for c in res['candidates'] if c['tier'] == 'high']; self.assertEqual(high_scores, sorted(high_scores, reverse=True))

    def test_a_partial_tier_match_with_a_higher_score_still_ranks_below_a_high_tier_one(self):
        res = self.find(exclude_applications=['10000001'], min_score=0.3)
        by = {c['application']: c for c in res['candidates']}
        a, b = by['10000002'], by['20000007']
        self.assertEqual((a['tier'], b['tier']), ('high', 'partial')); self.assertGreater(b['score'], a['score'])      # the thin-picture match scores higher (0.55 vs 0.525) ...
        self.assertLess(self.ids(res).index(('10000002', 'D2b')), self.ids(res).index(('20000007', 'P7')))            # ... and still ranks below

    def test_statute_overlap_is_a_pool_filter_and_the_min_score_applies(self):
        res = self.find(min_score=0.0)
        self.assertNotIn('20000006', {c['application'] for c in res['candidates']})                       # shares no statute with the query
        self.assertEqual(self.find(min_score=0.9)['candidates_matching'], 1)

    def test_references_match_on_document_numbers_only(self):
        res = self.find({'statutes': ['103'], 'references': ['US20190000009']}, min_score=0.0)
        top = next(c for c in res['candidates'] if c['application'] == '10000001')
        self.assertEqual(top['components']['references']['value'], 0.0); self.assertTrue(top['components']['references']['available'])
        with self.assertRaises(similar.SimilarError): similar.clean_features({'references': ['Hale']}, {}, 'x')      # a surname is not a document number

    def test_the_examiner_is_labelled_and_matched_exactly_after_normalising(self):
        res = self.find({'statutes': ['102', '103'], 'examiner': '  roe,   rick '}, min_score=0.0)
        top = res['candidates'][0]; self.assertEqual(top['examiner_label'], 'examiner named on the application record now')
        self.assertEqual(top['components']['examiner']['value'], 1.0)
        self.assertIn('Examiner as named on the application record now', res['coverage']['examiner_note'])
        self.assertEqual(self.find({'statutes': ['102', '103'], 'examiner': 'ROE'}, min_score=0.0)['candidates'][0]['components']['examiner']['value'], 0.0)   # no partial names

    def test_art_unit_cpc_and_finality_partial_credit(self):
        res = self.find({'statutes': ['102', '103'], 'art_unit': '1234', 'cpc': 'A01B1/00', 'finality': 'final'}, min_score=0.0)
        by = {c['application']: c for c in res['candidates']}
        self.assertEqual(by['20000003']['components']['art_unit']['value'], 0.5)       # 1235 shares the work group (first three digits) with 1234
        self.assertEqual(by['20000004']['components']['cpc']['value'], 0.5)            # same subclass, different main group


class Summaries(Base):
    def test_own_application_and_its_family_are_left_out_and_families_count_once(self):
        res = self.find(exclude_applications=['10000002'])
        self.assertNotIn('10000002', {c['application'] for c in res['candidates']}); self.assertNotIn('10000006x', {c['application'] for c in res['candidates']})
        self.assertEqual(res['coverage']['excluded_applications'], 2)
        both = self.find(); fam = [c for c in both['candidates'] if c['application'] in ('10000002', '10000006x')]; self.assertEqual(len(fam), 2)
        self.assertLess(both['outcome_summary']['matched_families'], both['outcome_summary']['matched_actions'])        # two actions of one family: one case

    def test_open_cases_are_separate_and_never_counted_as_a_result(self):
        o = self.find()['outcome_summary']
        self.assertEqual(o['resolved_families'], o['allowed']['n']); self.assertEqual(o['allowed']['n'], o['allowed']['k'] + o['abandoned']['k'])
        self.assertGreaterEqual(o['open_families'], 1); self.assertIn('right-censored', o['note'])
        self.assertEqual(o['matched_families'], o['resolved_families'] + o['open_families'])

    def test_shares_carry_intervals_and_are_hidden_below_min_n(self):
        o = self.find(min_n=1)['outcome_summary']['allowed']; self.assertIsNotNone(o['share']); lo, hi = o['ci95']; self.assertLessEqual(lo, o['share']); self.assertGreaterEqual(hi, o['share'])
        o = self.find(min_n=500)['outcome_summary']['allowed']; self.assertIsNone(o['share']); self.assertIsNone(o['ci95']); self.assertIn('fewer than 500', o['note'])
        self.assertEqual(similar.wilson(0, 0), None); lo, hi = similar.wilson(5, 10); self.assertAlmostEqual((lo + hi) / 2, 0.5, delta=0.02); self.assertEqual(similar.wilson(10, 10)[1], 1.0)

    def test_the_summary_covers_every_match_not_only_the_listed_ones(self):
        full = self.find(limit=50); short = self.find(limit=1)
        self.assertEqual(len(short['candidates']), 1); self.assertEqual(short['outcome_summary'], full['outcome_summary']); self.assertEqual(short['candidates_matching'], full['candidates_matching'])

    def test_applicant_steps_are_apart_from_outcomes_and_never_crossed(self):
        res = self.find(); lab = res['applicant_action_labels']
        self.assertIn('not cross-tabulated', lab['note']); self.assertIn('what any argument said', lab['note'])
        for key in ('interview_before_next', 'amendment_before_next', 'rce_or_appeal_next'): self.assertIn('n', lab[key])
        self.assertFalse({'allowed', 'abandoned', 'resolved_families'} & set(lab))
        self.assertFalse({'interview_before_next', 'next_event_counts'} & set(res['outcome_summary']))
        self.assertGreaterEqual(lab['no_later_event'] + lab['later_events_none_recognised'], 1)

    def test_coverage_starts_are_shown_and_skipped_applications_are_left_out(self):
        res = self.find(min_score=0.0); c = res['coverage']
        self.assertEqual(c['coverage_starts'], '2011-01-10'); self.assertIn('earlier history', c['coverage_note']); self.assertEqual(c['skipped_applications_left_out'], 2)   # one from the collector's own list, one orphan row
        self.assertNotIn('20000005', {x['application'] for x in res['candidates']}); self.assertIn('not semantic or legal similarity', res['caveat'])

    def test_the_pool_can_be_capped_and_says_so(self):
        res = self.find(pool_cap=2, min_score=0.0); self.assertEqual(res['coverage']['pool_capped_at'], 2); self.assertEqual(res['coverage']['candidates_considered'], 2)
        self.assertIsNone(self.find(min_score=0.0)['coverage']['pool_capped_at'])

    def test_outcomes_can_be_left_out_and_results_are_deterministic(self):
        self.assertNotIn('outcome', self.find(include_outcomes=False)['candidates'][0])
        self.assertEqual(json.dumps(self.find(), sort_keys=True), json.dumps(self.find(), sort_keys=True))

    def test_require_drops_candidates_without_that_component(self):
        res = self.find(min_score=0.0, require=['examiner']); self.assertTrue(res['candidates']); self.assertTrue(all(c['components']['examiner']['value'] > 0 for c in res['candidates']))


class Weights(Base):
    def test_defaults_are_versioned_dated_and_echoed(self):
        w = similar.effective_weights(None); self.assertEqual((w['version'], w['dated'], w['source']), ('1.0', '2026-11-20', 'default'))
        self.assertAlmostEqual(w['total'], 1.0); self.assertEqual(set(w['weights']), set(similar.COMPONENTS)); self.assertIn('not fitted', w['basis'])
        res = self.find(); self.assertEqual(res['weights']['weights'], w['weights']); self.assertEqual(res['weights']['version'], '1.0')

    def test_a_user_file_and_a_request_can_override_and_both_are_echoed(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, 'similarity.json').write_text(json.dumps({'weights': {'examiner': 0.5}}), encoding='utf-8')
            w = similar.effective_weights(t); self.assertEqual(w['weights']['examiner'], 0.5); self.assertEqual(w['overridden'], ['examiner']); self.assertEqual(len(w['user_file_sha256']), 64)
            w = similar.effective_weights(t, {'cpc': 0.0}); self.assertEqual(w['overridden'], ['cpc', 'examiner']); self.assertIn('request', w['source'])

    def test_bad_weights_are_refused_with_a_reason(self):
        for bad in ({'nonsense': 0.1}, {'examiner': -1}, {'examiner': 2}, {'examiner': 'x'}, {'examiner': True}, {}, [], {'examiner': float('nan')}):
            with self.assertRaises(similar.SimilarError, msg=str(bad)): similar.effective_weights(None, bad) if bad != {} else similar.validate_weights(bad, 'w')
        with tempfile.TemporaryDirectory() as t:
            Path(t, 'similarity.json').write_text('{not json', encoding='utf-8')
            with self.assertRaises(similar.SimilarError): similar.effective_weights(t)
            Path(t, 'similarity.json').write_text(json.dumps({'weights': {k: 0 for k in similar.COMPONENTS}}), encoding='utf-8')
            with self.assertRaises(similar.SimilarError) as c: similar.effective_weights(t)
            self.assertIn('above 0', str(c.exception))

    def test_a_different_weight_changes_the_score_and_the_shipped_defaults_hold_nothing_from_data(self):
        a = self.find(); b = self.find(weights=similar.effective_weights(self.data, {'examiner': 0.0}))
        self.assertNotEqual(a['candidates'][0]['score'], b['candidates'][0]['score'])
        cfg = json.loads((Path(similar.__file__).with_name('similarity.json')).read_text(encoding='utf-8')); self.assertIn('judgement', cfg['basis'])


class Database(unittest.TestCase):
    def test_no_database_and_an_old_one_are_clear_errors(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(similar.SimilarError) as c: similar.find(Path(t) / 'none.db', QUERY, weights=similar.effective_weights(None));
            self.assertEqual(c.exception.code, 'not_found'); self.assertIn('db build', str(c.exception))
            old = Path(t) / 'old.db'; con = sqlite3.connect(old); con.execute('CREATE TABLE meta (key TEXT, value TEXT)'); con.execute("INSERT INTO meta VALUES ('version', '2.0')"); con.commit(); con.close()
            with self.assertRaises(similar.SimilarError) as c: similar.find(old, QUERY, weights=similar.effective_weights(None))
            self.assertIn('rebuild', str(c.exception))


class Tool(Base):
    OA = ('Application No. 10/000,002   Art Unit 1234\nExaminer: Roe, Rick   Notification Date: 05/06/2011\n\nClaim Rejections - 35 USC 103\n'
          'Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Hale (US 2019/0000001) in view of Ives. The ZZMARKERZZ widget is taught.\n')

    def setUp(self):
        for p in (mock.patch.object(ld, 'DATA', self.data), mock.patch.dict(os.environ, {'PG_TOOL_DATA_DIR': str(self.data)})): p.start(); self.addCleanup(p.stop)
        ld._cache.clear(); self.addCleanup(ld._cache.clear)

    def test_the_tool_reads_features_from_text_leaves_the_texts_own_application_out_and_echoes_none_of_the_text(self):
        env = service.call('pg_similar_office_actions', {'text': self.OA, 'min_score': 0.3, 'min_n': 1})
        self.assertEqual(env['status'], 'ok', env.get('error')); d = env['data']
        self.assertEqual(env['boundary']['classification'], 'caller-confidential'); self.assertFalse(env['boundary']['persisted_by_tool']); self.assertEqual(env['boundary']['live_calls'], 0)
        self.assertEqual(d['query']['features']['statutes'], ['103']); self.assertEqual(d['query']['features']['references'], ['US20190000001']); self.assertEqual(d['query']['features']['examiner'], 'ROE, RICK')
        self.assertIn('10000002', d['query']['excluded_applications']); self.assertNotIn('10000002', {c['application'] for c in d['candidates']})
        self.assertNotIn('ZZMARKERZZ', json.dumps(env)); self.assertNotIn('Hale', json.dumps(env))
        self.assertIn('not semantic or legal similarity', json.dumps(env))

    def test_features_only_and_validation(self):
        env = service.call('pg_similar_office_actions', {'features': {'statutes': ['103'], 'art_unit': '1234'}, 'min_score': 0.1, 'min_n': 1}); self.assertEqual(env['status'], 'ok', env.get('error'))
        self.assertEqual(env['boundary']['classification'], 'caller-confidential')
        for bad in ({}, {'features': {'statutes': ['999']}}, {'features': {'references': ['Hale']}}, {'features': {'art_unit': 'abcd'}}, {'text': 'x', 'weights': {'bogus': 1}}, {'text': 'x', 'limit': 500}):
            e = service.call('pg_similar_office_actions', bad); self.assertEqual(e['status'], 'error', bad); self.assertIn(e['error']['code'], ('invalid_input',))

    def test_a_missing_database_is_not_found(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(ld, 'DATA', Path(t)):
            e = service.call('pg_similar_office_actions', {'features': {'statutes': ['103']}}); self.assertEqual(e['status'], 'not_found'); self.assertIn('db build', e['error']['message'])


if __name__ == '__main__':
    unittest.main()
