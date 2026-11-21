"""pg_db.py (schema v2): value parsing, read-only guard, report rendering, optional steps, builds on an empty data folder, a
user-chosen field name, filing years before 2015, skipped applications, atomic swap and progress files. All data is synthetic."""
import gzip, json, os, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_db
import synthetic as syn
from pg_tool import paths


class Helpers(unittest.TestCase):
    def test_lit_reads_reprs(self):
        self.assertIsNone(pg_db.lit('None'))
        self.assertEqual(pg_db.lit("['A01B1/00', 'A01C1/00']"), ['A01B1/00', 'A01C1/00'])
        self.assertEqual(pg_db.lit({'a': 1}), {'a': 1})
        self.assertIs(pg_db.lit('True'), True)
        self.assertEqual(pg_db.lit('3686'), '3686')

    def test_outcome(self):
        self.assertEqual(pg_db.outcome_of('Patented Case', '11223344'), 'granted')
        self.assertEqual(pg_db.outcome_of('Abandoned  --  Failure to Respond to an Office Action', None), 'abandoned')
        self.assertEqual(pg_db.outcome_of('Notice of Allowance Mailed -- Application Received in Office of Publications', None), 'allowed_not_yet_granted')
        self.assertEqual(pg_db.outcome_of('Non Final Action Mailed', None), 'pending')

    def test_render_keeps_years_plain(self):
        con = sqlite3.connect(':memory:'); self.addCleanup(con.close)
        out = pg_db.render(con.execute('SELECT 2021 AS year, 12345 AS n, NULL AS x'), 'md')
        self.assertIn('| 2021 | 12,345 |  |', out)
        self.assertEqual(pg_db.render(con.execute('SELECT 1 AS a'), 'csv').splitlines(), ['a', '1'])

    def test_sql_is_read_only(self):
        with self.assertRaises(SystemExit):
            pg_db.main(['sql', 'DELETE FROM events', '--db', str(Path(tempfile.gettempdir()) / 'nope.db')])

    def test_years_argument(self):
        self.assertIsNone(pg_db.parse_years('all')); self.assertIsNone(pg_db.parse_years(None))
        self.assertEqual(pg_db.parse_years('2012'), (2012, 2012)); self.assertEqual(pg_db.parse_years('2008-2011'), (2008, 2011))
        for bad in ('2012-2008', 'abc', '12'):
            with self.assertRaises(SystemExit): pg_db.parse_years(bad)


class Features(unittest.TestCase):
    """build_features and compare_rows on a tiny synthetic database."""
    def setUp(self):
        self.con = sqlite3.connect(':memory:'); self.addCleanup(self.con.close)
        self.con.executescript(pg_db.SCHEMA)
        apps = [('A1', 2018, 'granted'), ('A2', 2018, 'abandoned'), ('A3', 2019, 'granted'), ('A4', 2019, 'abandoned'),
                ('A5', 2024, 'pending')]
        for a, y, o in apps:
            self.con.execute('INSERT INTO applications (application, filing_year, art_unit, examiner, outcome) VALUES (?,?,?,?,?)', (a, y, '2121', 'DOE, J', o))
            self.con.execute('INSERT INTO memberships VALUES (?,?,?)', (a, 'f1', 'agri'))
        ev = [('A1', 0, '2019-01-01', 'CTNF'), ('A1', 1, '2019-03-01', 'EXAT'), ('A1', 2, '2019-04-01', 'MN/=.'),
              ('A2', 0, '2019-01-01', 'CTNF'), ('A2', 1, '2019-06-01', 'CTFR'), ('A2', 2, '2020-01-01', 'MABN2'),
              ('A3', 0, '2020-01-01', 'MN/=.'), ('A4', 0, '2020-01-01', 'CTNF'), ('A4', 1, '2020-02-01', 'EXET')]
        self.con.executemany('INSERT INTO events VALUES (?,?,?,?)', ev)
        self.con.executemany('INSERT INTO office_actions (application, oa_document, rej101) VALUES (?,?,?)',
                             [('A1', 'd1', 1), ('A2', 'd2', 1), ('A4', 'd4', 0)])
        pg_db.build_features(self.con)

    def test_feature_flags(self):
        f = {r[0]: r[1:] for r in self.con.execute('SELECT application, allowed, interview, interview_applicant, interview_examiner, '
                                                   'final_rejection, first_action_allowance, rej101, has_oa_data FROM app_features')}
        self.assertEqual(f['A1'], (1, 1, 1, 0, 0, 0, 1, 1))
        self.assertEqual(f['A2'], (0, 0, 0, 0, 1, 0, 1, 1))
        self.assertEqual(f['A3'], (1, 0, 0, 0, 0, 1, 0, 0))   # allowed with no rejection; no OA data
        self.assertEqual(f['A4'][2:4], (0, 1))                 # examiner-initiated interview only
        self.assertIsNone(f['A5'][0])                          # pending: excluded from allowance rates

    def test_compare_rows(self):
        rows = pg_db.compare_rows(self.con, 'interview', 'all', '', 'field', (2015, 2020), 1)
        self.assertEqual(rows, [{'group': 'agri', 'with_n': 2, 'with_pct': 50.0, 'without_n': 2, 'without_pct': 50.0,
                                 'diff_pts': 0.0, 'ci95_pts': rows[0]['ci95_pts']}])
        r101 = pg_db.compare_rows(self.con, 'interview_applicant', 'rej101', '', 'field', (2015, 2020), 1)
        self.assertEqual((r101[0]['with_n'], r101[0]['with_pct'], r101[0]['without_n'], r101[0]['without_pct']), (1, 100.0, 1, 0.0))
        self.assertEqual(pg_db.compare_rows(self.con, 'interview', 'all', '', 'field', (2015, 2020), 3), [])  # min_n

    def test_population_parser(self):
        self.assertEqual(pg_db.population_sql('all'), '1')
        self.assertEqual(pg_db.population_sql('rej101+not:final_rejection'), 'f.rej101=1 AND f.final_rejection=0')
        with self.assertRaises(SystemExit): pg_db.population_sql('nonsense')

    def test_years_default_is_every_year_in_the_database(self):
        everything = pg_db.compare_rows(self.con, 'interview', 'all', '', 'field', None, 1)
        self.assertEqual(everything[0]['with_n'] + everything[0]['without_n'], 4)        # 2018 and 2019 are both counted
        only_2019 = pg_db.compare_rows(self.con, 'interview', 'all', '', 'field', (2019, 2019), 1)
        self.assertEqual(only_2019[0]['with_n'] + only_2019[0]['without_n'], 2)
        self.assertEqual(pg_db.resolve_years(self.con, None), (2018, 2024))

    def test_skipped_applications_leave_denominators(self):
        self.con.execute("INSERT INTO skipped_applications VALUES ('A1', 'f1', 'oa', 'test')"); pg_db.build_features(self.con)
        rows = pg_db.compare_rows(self.con, 'interview', 'all', '', 'field', None, 1)
        self.assertEqual(rows[0]['with_n'] + rows[0]['without_n'], 3)


class BuildBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.db = self.data / 'corpus' / 'pg.db'

    def build(self, **kw):
        out = []
        rep = pg_db.build(self.db, self.data, say=out.append, **kw)
        return rep

    def q(self, sql, *args):
        con = pg_db.connect_ro(self.db)
        try: return con.execute(sql, args).fetchall()
        finally: con.close()


class EmptyDataFolder(BuildBase):
    def test_build_on_an_empty_data_folder_succeeds(self):
        rep = self.build()
        self.assertTrue(self.db.exists())
        self.assertEqual({m['dataset'] for m in rep['missing']}, {'scope', 'frames', 'events', 'oa_rejections', 'documents', 'ptab'})
        self.assertEqual(self.q('SELECT COUNT(*) FROM applications'), [(0,)])
        for name in pg_db.REPORTS:   # every named report runs on an empty database
            self.assertIn('|', pg_db.report(self.db, name, 'md', '', 30), name)
        cov = pg_db.report(self.db, 'coverage')
        self.assertIn('not collected', cov); self.assertIn('| frames | not collected |', cov)
        for name in ('compare', ):
            self.assertIn('no group meets', pg_db.compare(self.db, 'interview'))

    def test_every_step_is_optional_and_steps_can_be_chosen(self):
        syn.make_data_folder(self.data)
        rep = self.build(steps=['frames'])
        self.assertEqual(rep['status']['oa'], 'skipped (not requested)')
        self.assertEqual(self.q('SELECT COUNT(*) FROM office_actions'), [(0,)])
        with self.assertRaises(ValueError): self.build(steps=['frames', 'nonsense'])

    def test_new_tables_exist_and_owner_only_tables_do_not(self):
        self.build()
        names = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('scope', 'frames', 'oa_paragraphs', 'office_actions', 'oa_citations', 'oa_parsed', 'oa_outcomes', 'collection_runs', 'skipped_applications'):
            self.assertIn(t, names)
        for t in ('episodes', 'argument_units', 'claim_structures', 'holdings', 'cafc_opinions', 'cafc_patents'):
            self.assertNotIn(t, names)
        for t in ('oa_citations', 'oa_outcomes', 'oa_parsed', 'collection_runs'):
            self.assertEqual(self.q(f'SELECT COUNT(*) FROM {t}'), [(0,)])
        cols = {r[1] for r in self.q('PRAGMA table_info(collection_runs)')}
        self.assertFalse(cols & {'query', 'features', 'text', 'claims', 'examiner'})   # no query features are ever stored with a run

    def test_data_folder_inside_the_install_folder_is_refused(self):
        with self.assertRaises(paths.PathError): pg_db.build(None, paths.INSTALL_DIR / 'scratch-data', say=lambda *_: None)
        self.assertFalse((paths.INSTALL_DIR / 'scratch-data').exists())


class CustomFieldAndEarlyYears(BuildBase):
    def setUp(self):
        super().setUp(); syn.make_data_folder(self.data); self.rep = self.build()

    def test_loaded_with_a_custom_field_and_pre_2015_years(self):
        self.assertEqual(self.q('SELECT DISTINCT field FROM memberships'), [('agri',)])
        self.assertEqual(self.q('SELECT filing_year, COUNT(*) FROM applications GROUP BY 1 ORDER BY 1'), [(2010, 3), (2011, 3)])
        fr = self.q('SELECT frame_id, fields, window_from, window_to, date_basis, eligible FROM frames')
        self.assertEqual(fr, [('batch-one', 'agri', '2010-01-01', '2012-01-01', 'filing_date', 6)])
        self.assertEqual(self.q('SELECT examiner FROM applications LIMIT 1'), [(syn.EXAMINER,)])

    def test_year_comes_from_each_row_not_from_the_frame_name(self):
        # one frame holds two filing years; a frame id that looks nothing like y<year>-<field> is accepted
        self.assertEqual(self.q('SELECT COUNT(DISTINCT filing_year) FROM applications WHERE application IN (SELECT application FROM memberships WHERE frame_id=?)', 'batch-one'), [(2,)])

    def test_default_years_cover_pre_2015_filings_and_explicit_years_filter(self):
        everything = pg_db.report(self.db, 'overview', 'md', '', 1)
        self.assertIn('| agri | 2010 | 3 |', everything); self.assertIn('| agri | 2011 | 3 |', everything)
        only = pg_db.report(self.db, 'overview', 'md', '', 1, years=(2011, 2011))
        self.assertNotIn('| agri | 2010 |', only); self.assertIn('| agri | 2011 | 3 |', only)
        self.assertIn('| agri |', pg_db.report(self.db, 'art_units', 'md', 'agri', 1))   # reports that used a fixed 2015-2020 window
        self.assertIn('ROE, RICK', pg_db.report(self.db, 'examiners', 'md', 'agri', 1))
        self.assertIn('ROE, RICK', pg_db.report(self.db, 'examiner_101', 'md', 'agri', 1))

    def test_office_action_columns(self):
        r = self.q("SELECT doc_code, art_unit, examiner, field, cpc_group, cpc_subclass, is_final, statute_set FROM office_actions WHERE application='10000001'")
        self.assertEqual(r, [('CTNF', syn.ART_UNIT, syn.EXAMINER, 'agri', 'A01B1', 'A01B', 0, '102,103')])
        self.assertEqual(self.q("SELECT is_final FROM office_actions WHERE oa_document='D2b'"), [(1,)])
        self.assertEqual(self.q("SELECT paragraph_number, cite103_max FROM oa_paragraphs WHERE application='10000001' ORDER BY paragraph_number"), [(1, 2), (2, 2)])

    def test_rows_for_applications_in_no_frame_are_listed_and_not_counted(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM oa_paragraphs WHERE application='99999999'"), [(0,)])
        self.assertIn(('99999999', 'batch-one', 'oa'), self.q('SELECT application, frame_id, step FROM skipped_applications'))
        self.assertIn('in none of your frames', pg_db.report(self.db, 'coverage'))

    def test_coverage_report_gives_the_denominators(self):
        cov = pg_db.report(self.db, 'coverage')
        for text in ('| applications | 6 |', 'filing year 2010', 'filing year 2011', '| office-action rejections |', '2011-01-10 to 2012-05-10'):
            self.assertIn(text, cov)

    def test_read_only_readers(self):
        con = pg_db.connect_ro(self.db); self.addCleanup(con.close)
        with self.assertRaises(sqlite3.OperationalError): con.execute('DELETE FROM events')
        with self.assertRaises(sqlite3.OperationalError): con.execute('CREATE TABLE x (a)')
        with self.assertRaises(SystemExit): pg_db.main(['sql', 'DELETE FROM events', '--db', str(self.db)])


class SkippedApplications(BuildBase):
    def test_malformed_rows_and_collector_skips_are_listed_and_excluded(self):
        bad = [syn.frame_row('1000', '2010-01-01'), syn.frame_row('10000009', 'not-a-date')]
        syn.write_frame(self.data, extra_rows=bad); syn.write_events(self.data); syn.write_oa(self.data)
        (self.data / 'corpus' / 'oa-rejections' / 'batch-one.skipped.json').write_text(json.dumps({'applications': [{'application': '10000005', 'reason': 'provider returned an error'}]}), encoding='utf-8')
        self.build()
        sk = self.q('SELECT application, step, reason FROM skipped_applications ORDER BY step, application')
        reasons = {(a, s) for a, s, _ in sk}
        self.assertIn(('1000', 'frames'), reasons); self.assertIn(('10000009', 'frames'), reasons); self.assertIn(('10000005', 'oa'), reasons)
        self.assertEqual(self.q("SELECT COUNT(*) FROM applications WHERE application IN ('1000','10000009')"), [(0,)])
        self.assertEqual(self.q("SELECT skipped FROM app_features WHERE application='10000005'"), [(1,)])
        con = pg_db.connect_ro(self.db); self.addCleanup(con.close)
        n_all = pg_db.compare_rows(con, 'rej103', 'all', '', 'field', None, 1)
        self.assertEqual(sum(r['with_n'] + r['without_n'] for r in n_all), 4)   # 5 decided applications minus the skipped one


class Swap(BuildBase):
    def test_swap_retries_when_the_file_is_busy(self):
        syn.make_data_folder(self.data); self.build()
        before = self.db.read_bytes()
        real, calls = os.replace, []
        def flaky(a, b):
            calls.append(1)
            if len(calls) <= 2: raise PermissionError('in use')
            return real(a, b)
        with mock.patch('pg_db.os.replace', flaky), mock.patch('pg_db.time.sleep'):
            self.build()
        self.assertGreater(len(calls), 2); self.assertTrue(self.db.exists())

    def test_failed_swap_keeps_the_old_database_and_removes_the_new_file(self):
        syn.make_data_folder(self.data); self.build(); before = self.db.read_bytes()
        with mock.patch('pg_db.os.replace', side_effect=PermissionError('in use')), mock.patch('pg_db.time.sleep'):
            with self.assertRaises(PermissionError): self.build()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual([p.name for p in self.db.parent.glob('pg.db.building*')], [])

    def test_a_crashing_step_leaves_the_old_database_alone(self):
        syn.make_data_folder(self.data); self.build(); before = self.db.read_bytes()
        (self.data / 'corpus' / 'frame-events' / 'batch-one.jsonl.gz').write_bytes(b'not a gzip file')
        with self.assertRaises(Exception): self.build()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual([p.name for p in self.db.parent.glob('pg.db.building*')], [])

    def test_a_reader_keeps_working_during_a_build(self):
        syn.make_data_folder(self.data); self.build()
        con = pg_db.connect_ro(self.db); self.addCleanup(con.close)
        con.execute('SELECT COUNT(*) FROM applications').fetchone()
        # build into a second file in the same folder: the first stays readable throughout
        other = self.data / 'corpus' / 'other.db'
        pg_db.build(other, self.data, say=lambda *_: None)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM applications').fetchone(), (6,))


class ProgressFiles(BuildBase):
    def test_progress_file_is_complete_json_and_has_no_temp_leftovers(self):
        syn.make_data_folder(self.data); self.build()
        prog = self.db.with_name('pg.progress.json')
        d = json.loads(prog.read_text(encoding='utf-8'))
        self.assertTrue(d['finished']); self.assertEqual(d['counts']['frames'], 6)
        self.assertEqual([p.name for p in self.db.parent.glob('*.tmp*')], [])

    def test_atomic_write_replaces_whole_file_or_leaves_it_alone(self):
        p = self.data / 'x.json'; pg_db.atomic_write_text(p, '{"a": 1}\n')
        with mock.patch('pg_db.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): pg_db.atomic_write_text(p, '{"a": 2}\n')
        self.assertEqual(json.loads(p.read_text(encoding='utf-8')), {'a': 1})


class FrameNames(BuildBase):
    def test_invalid_frame_file_names_are_ignored_and_valid_ones_loaded(self):
        syn.write_frame(self.data, 'my_frame-2')
        syn.write_frame(self.data, 'Bad Name', apps=[('10000099', '2010-01-01', 'Patented Case', '8000099')])   # not a valid frame id
        self.build()
        self.assertEqual(self.q('SELECT frame_id FROM frames'), [('my_frame-2',)])
        self.assertEqual(pg_db.valid_frame_stem('con.json.gz'), False)
        self.assertEqual(pg_db.valid_frame_stem('nul.jsonl.gz'), False)
        self.assertEqual(pg_db.valid_frame_stem('y2010-agri.json.gz'), True)


if __name__ == '__main__':
    unittest.main()
