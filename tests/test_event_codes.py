"""What happened after an office action: the anchor on the action's own event (+-3 days, rejection or mail code), never the action's own event as
its "next event", the split between "no later event" and "later events, none recognised", code lists as data checked against the official USPTO list,
abandonment rules (ABN9 is not one, AFWC is labelled apart, a rescinded abandonment is not final), the coverage report's unanchored actions and
unrecognised codes with official descriptions, and the wording about the codes in every place a reader meets the result."""
import csv, datetime, hashlib, json, tempfile, unittest
from pathlib import Path
from unittest import mock

import pg_db
import synthetic as syn
from hubenv import provider_row as pr
from pg_tool import oa_outcomes as oo, similar, stats

ROOT = Path(__file__).resolve().parents[1]
with open(ROOT / 'pg_tool' / 'data' / 'uspto_event_codes.csv', encoding='utf-8-sig', newline='') as _f: OFFICIAL = {r['event_cd']: r['event_desc'] for r in csv.DictReader(_f)}
D = lambda date, seq, code: (date, seq, code)


class Data(unittest.TestCase):
    def test_the_official_list_ships_with_its_provenance_and_every_code_in_use_is_in_it(self):
        raw = (ROOT / 'pg_tool' / 'data' / 'uspto_event_codes.csv').read_bytes(); cfg = json.loads((ROOT / 'pg_tool' / 'event_codes.json').read_text(encoding='utf-8'))
        o = cfg['official_list']
        self.assertEqual(hashlib.sha256(raw).hexdigest(), o['csv_sha256']); self.assertEqual(o['rows'], len(OFFICIAL)); self.assertEqual(len(OFFICIAL), 2194)
        self.assertEqual(o['zip_sha256'], 'd3dc72afcd38a0e677f116faf979179ec34e0dbc0520e5f9e95c39091ba85c57'); self.assertEqual(o['retrieved'], '2026-11-21')
        self.assertTrue(o['source_url'].startswith('https://data.uspto.gov/')); self.assertIn('public domain', o['licence'])
        for c, v in cfg['codes'].items():
            self.assertIn(c, OFFICIAL, c); self.assertEqual(v['meaning'], OFFICIAL[c], c); self.assertTrue(v['confirmed']); self.assertEqual(v['source'], 'USPTO PatEx 2022 event_codes.csv'); self.assertIn(v['role'], cfg['roles'], c)
        self.assertNotIn('ABN.', OFFICIAL); self.assertNotIn('ABN.', cfg['codes']); self.assertIsNone(oo.role_of('ABN.')); self.assertIn('ABN.', cfg['not_in_the_official_list'])
        for c in ('CTNF', 'MCTNF', 'CTFR', 'MCTFR', 'EXIA', 'EXAT', 'EXAP', 'EXAC', 'EXIE', 'EXET', 'EXEP', 'EXEC', 'EXIN', 'A...', 'A.NE', 'RCEX', 'N/AP', 'AP.B', 'MN/=.', 'RAFC', 'APCA', 'APCR', 'APCP', 'APCD', 'PET.'):
            self.assertIn(c, cfg['codes'], c); self.assertTrue(oo.is_confirmed(c), c)
        self.assertEqual(cfg['codes']['A.NE']['meaning'], 'Response after Final Action'); self.assertNotIn('not entered', json.dumps(cfg).lower()); self.assertEqual(cfg['codes']['MCTFR']['meaning'], 'Mail Final Rejection (PTOL - 326)')
        self.assertFalse(oo.is_confirmed('ZZZZ'))

    def test_abandonment_roles_follow_the_official_list(self):
        for c in ('ABN2', 'ABN3', 'ABN4', 'ABN5', 'ABN6', 'ABN7', 'ABN8', 'ABN10', 'MABN', 'MABN2', 'MABN3', 'MABN4', 'MABN5', 'MABN6', 'MABN7', 'MABN10', 'NABN', 'EABN', 'EXABN'): self.assertEqual(oo.role_of(c), 'abandonment', c)
        self.assertEqual(oo.role_of('ABN9'), 'disposal_for_rce'); self.assertNotIn(oo.role_of('ABN9'), oo.TURNING_ROLES)           # a disposal for an RCE is not an abandonment
        self.assertEqual(oo.role_of('AFWC'), 'abandonment_for_continuation'); self.assertIn('abandonment_for_continuation', oo.TURNING_ROLES)
        for c in ('NRAB', 'N421', 'MNRAB', 'RABN', 'J520', 'MM327-C', 'M327-C', 'ODPD17', 'MODPD17'): self.assertEqual(oo.role_of(c), 'abandonment_rescinded', c)
        for c in ('ABN1', 'ABNP', 'ABNX', 'ABN/', 'ABNF', 'ABNC', 'MABNP', 'WABN', 'WPABN'): self.assertEqual(oo.role_of(c), 'preexam_abandonment', c); self.assertNotIn('preexam_abandonment', oo.TURNING_ROLES)
        self.assertIsNone(oo.role_of('ABN11'))                                                                                        # in the official list, no role in this tool

    def test_the_wording_about_the_codes_is_the_same_in_the_note_the_docs_and_the_tool_description(self):
        self.assertIn('checked against the USPTO PatEx 2022 list', similar.ACTION_NOTE); self.assertIn("tool's own mapping", similar.ACTION_NOTE)
        doc = (ROOT / 'docs' / 'SIMILAR.md').read_text(encoding='utf-8'); self.assertIn('checked against the USPTO PatEx 2022 list', doc); self.assertIn("tool's own mapping", doc)
        c = json.loads((ROOT / 'pg_tool' / 'contract.json').read_text(encoding='utf-8'))['tools']['pg_similar_office_actions']['description']; self.assertIn('checked against the USPTO PatEx 2022 list', c); self.assertIn("tool's own mapping", c)
        for t in (similar.ACTION_NOTE, doc, c, oo.__doc__): self.assertNotIn('not yet verified', t)
        notice = (ROOT / 'NOTICE').read_text(encoding='utf-8'); self.assertIn('PatEx 2022', notice); self.assertIn('public domain', notice)


class Anchor(unittest.TestCase):
    """The real pattern (measured on collected data): the record date of a rejection is the date of the MAILING event; the action event (CTNF, CTFR)
    is usually 1 to 9 or more days earlier; the provider's array order is not chronological."""
    def test_the_record_date_is_the_mail_event_and_the_action_event_lags_it_by_days(self):
        for lag in (0, 1, 4, 7, 9):
            ct = (datetime.date(2020, 1, 20) - datetime.timedelta(days=lag)).isoformat()
            ev = [D(ct, 0, 'CTNF'), D('2020-01-20', 1, 'MCTNF'), D('2020-02-15', 2, 'A...'), D('2020-03-01', 3, 'MN/=.')]
            r = oo.derive(ev, '2020-01-20', 'CTNF', 'granted')
            self.assertEqual((r['anchor_quality'], r['anchor_code'], r['anchor_date'], r['next_event'], r['next_event_state'], r['amendment_before_next']), ('mail_exact', 'MCTNF', '2020-01-20', 'MN/=.', 'recognised', 1), lag)
        self.assertEqual(oo.derive(ev, '2020-01-21', 'CTNF', 'x')['anchor_quality'], 'mail_near'); self.assertEqual(oo.derive(ev, '2020-01-19', 'CTNF', 'x')['anchor_quality'], 'mail_near')
        self.assertEqual(oo.derive(ev, '2020-01-22', 'CTNF', 'x')['anchor_quality'], 'ct_near')   # 2 days from the mail event: the action event 9 days before the mail is within 14 days of the date

    def test_the_paired_action_event_before_the_mail_event_is_never_its_next_event(self):
        for lag in (1, 4, 7):                                        # nothing after the action: the CT event must not be taken for "the next event"
            ct = (datetime.date(2020, 1, 20) - datetime.timedelta(days=lag)).isoformat()
            r = oo.derive([D(ct, 0, 'CTFR'), D('2020-01-20', 1, 'MCTFR')], '2020-01-20', 'CTFR', 'pending'); self.assertEqual((r['next_event_state'], r['next_event'], r['later_events']), ('no_later_event', None, 0), lag)
            r = oo.derive([D('2020-01-20', 0, 'MCTFR'), D(ct, 1, 'CTFR'), D('2020-05-01', 2, 'CTNF')], '2020-01-20', 'CTFR', 'pending'); self.assertEqual((r['next_event'], r['next_event_date']), ('CTNF', '2020-05-01'), lag)

    def test_the_action_event_anchors_when_there_is_no_mail_event_within_14_days_before_to_1_day_after(self):
        ev = [D('2020-01-10', 0, 'CTNF'), D('2020-03-01', 1, 'MN/=.')]
        for od, ok in (('2020-01-11', True), ('2020-01-10', True), ('2020-01-09', True), ('2020-01-24', True), ('2020-01-25', False), ('2020-01-08', False)):
            r = oo.derive(ev, od, 'CTNF', 'x'); self.assertEqual(r['anchor_quality'], 'ct_near' if ok else 'unanchored', od)
        r = oo.derive(ev, '2020-01-20', 'CTNF', 'x'); self.assertEqual((r['anchor_code'], r['next_event']), ('CTNF', 'MN/=.'))
        r = oo.derive([D('2020-01-10', 0, 'CTNF'), D('2020-01-18', 1, 'MCTNF'), D('2020-03-01', 2, 'MN/=.')], '2020-01-14', 'CTNF', 'x')
        self.assertEqual((r['anchor_quality'], r['next_event'], r['later_events']), ('ct_near', 'MN/=.', 1))                  # the mailing event after the action event is its own, not what came next

    def test_unanchored_means_nothing_is_claimed(self):
        u = oo.derive([D('2020-01-10', 0, 'CTNF'), D('2020-03-01', 1, 'MN/=.')], '2020-06-14', 'CTNF', 'x')
        self.assertEqual((u['anchor_quality'], u['next_event'], u['next_event_state'], u['interview_before_next'], u['rce_or_appeal_next']), ('unanchored', None, 'unanchored', None, None))

    def test_a_second_action_is_a_later_action(self):
        ev = [D('2020-01-08', 0, 'CTNF'), D('2020-01-10', 1, 'MCTNF'), D('2020-02-08', 2, 'CTFR'), D('2020-02-10', 3, 'MCTFR')]
        r = oo.derive(ev, '2020-02-10', 'CTFR', 'x'); self.assertEqual((r['anchor_date'], r['anchor_quality'], r['next_event_state']), ('2020-02-10', 'mail_exact', 'no_later_event'))
        r = oo.derive(ev, '2020-01-10', 'CTNF', 'x'); self.assertEqual((r['next_event'], r['next_event_date']), ('CTFR', '2020-02-08'))                  # the second action is what came next

    def test_the_providers_array_order_is_never_used(self):
        base = [D('2020-01-10', 0, 'MCTNF'), D('2020-02-01', 1, 'EXIN'), D('2020-02-20', 2, 'A...'), D('2020-03-01', 3, 'MN/=.')]
        want = oo.derive(base, '2020-01-10', 'CTNF', 'x')
        for order in (base[::-1], [base[2], base[0], base[3], base[1]], [base[3], base[1], base[0], base[2]]):
            got = oo.derive([(d, i, c) for i, (d, _s, c) in enumerate(order)], '2020-01-10', 'CTNF', 'x'); self.assertEqual(got, want)
        self.assertEqual((want['interview_before_next'], want['amendment_before_next'], want['next_event']), (1, 1, 'MN/=.'))

    def test_events_on_the_same_date_are_simultaneous_and_count_as_before_the_turning_point(self):
        ev = [D('2020-01-10', 0, 'MCTNF'), D('2020-03-01', 1, 'MN/=.'), D('2020-03-01', 2, 'EXIN'), D('2020-03-01', 3, 'A...')]
        r = oo.derive(ev, '2020-01-10', 'CTNF', 'x'); self.assertEqual((r['interview_before_next'], r['amendment_before_next'], r['next_event']), (1, 1, 'MN/=.'))
        r = oo.derive(list(reversed(ev)), '2020-01-10', 'CTNF', 'x'); self.assertEqual((r['interview_before_next'], r['amendment_before_next']), (1, 1))
        r = oo.derive([D('2020-01-10', 0, 'MCTNF'), D('2020-03-02', 1, 'EXIN'), D('2020-03-01', 2, 'MN/=.')], '2020-01-10', 'CTNF', 'x'); self.assertEqual(r['interview_before_next'], 0)   # a later date is not before it
        r = oo.derive([D('2020-01-10', 0, 'MCTNF'), D('2020-01-10', 1, 'EXIN'), D('2020-03-01', 2, 'MN/=.')], '2020-01-10', 'CTNF', 'x'); self.assertEqual(r['interview_before_next'], 0)   # on the anchor date: not counted as later

    def test_the_turning_point_within_a_date_follows_a_fixed_role_precedence(self):
        order = ['RCEX', 'N/AP', 'MN/=.', 'AFWC', 'ABN2', 'CTFR']
        self.assertEqual(oo.PRECEDENCE, ('rce', 'appeal', 'allowance', 'abandonment_for_continuation', 'abandonment', 'office_action', 'mail_office_action'))
        for i in range(len(order) - 1):
            ev = [D('2020-01-10', 0, 'MCTNF')] + [D('2020-04-01', k + 1, c) for k, c in enumerate(reversed(order[i:]))]
            self.assertEqual(oo.derive(ev, '2020-01-10', 'CTNF', 'x')['next_event'], order[i], order[i:])
        ev = [D('2020-01-10', 0, 'MCTNF'), D('2020-04-01', 1, 'ABN3'), D('2020-04-01', 2, 'ABN2')]; self.assertEqual(oo.derive(ev, '2020-01-10', 'CTNF', 'x')['next_event'], 'ABN2')   # then the code, alphabetically

    def test_no_later_event_is_not_the_same_as_later_events_none_recognised(self):
        base = [D('2020-01-10', 0, 'MCTNF')]
        self.assertEqual(oo.derive(base, '2020-01-10', 'CTNF', 'pending')['next_event_state'], 'no_later_event')
        r = oo.derive(base + [D('2020-02-01', 1, 'ZZZZ'), D('2020-02-02', 2, 'ABN.'), D('2020-02-03', 3, 'ABN9')], '2020-01-10', 'CTNF', 'pending')
        self.assertEqual((r['next_event_state'], r['later_events'], r['next_event']), ('later_events_none_recognised', 3, None))                   # ABN. does not exist; ABN9 is a disposal for an RCE, not an abandonment
        self.assertEqual(oo.derive(base + [D('2020-02-01', 1, 'MABN4')], '2020-01-10', 'CTNF', 'abandoned')['next_event'], 'MABN4')
        self.assertEqual(oo.derive(base + [D('2020-02-01', 1, 'AFWC')], '2020-01-10', 'CTNF', 'abandoned')['next_event'], 'AFWC')                 # labelled apart from other abandonments
        self.assertEqual(oo.derive(base + [D('2020-02-01', 1, 'ABNP')], '2020-01-10', 'CTNF', 'x')['next_event_state'], 'later_events_none_recognised')   # pre-exam abandonment is not an outcome of an action

    def test_a_rescinded_abandonment_is_skipped_and_the_search_goes_on(self):
        ev = [D('2020-01-10', 0, 'MCTNF'), D('2020-08-01', 1, 'ABN2')]
        r = oo.derive(ev, '2020-01-10', 'CTNF', 'abandoned'); self.assertEqual((r['next_event'], r['abandonment_rescinded']), ('ABN2', 0))
        r = oo.derive(ev + [D('2020-09-01', 2, 'NRAB')], '2020-01-10', 'CTNF', 'pending')
        self.assertEqual((r['next_event'], r['abandonment_rescinded'], r['next_event_state']), (None, 1, 'later_events_none_recognised'))            # undone, and nothing since
        r = oo.derive(ev + [D('2020-09-01', 2, 'NRAB'), D('2020-10-01', 3, 'EXIN'), D('2020-11-01', 4, 'MCTFR'), D('2021-02-01', 5, 'MN/=.')], '2020-01-10', 'CTNF', 'granted')
        self.assertEqual((r['next_event'], r['next_event_date'], r['abandonment_rescinded'], r['interview_before_next']), ('MCTFR', '2020-11-01', 1, 1))   # the next turning point after the rescission
        self.assertIsNone(oo.derive([D('2020-01-10', 0, 'MCTNF'), D('2020-02-01', 1, 'MN/=.')], '2020-01-10', 'CTNF', 'x')['abandonment_rescinded'])
        r = oo.derive(ev + [D('2020-09-01', 2, 'NRAB'), D('2020-12-01', 3, 'ABN2')], '2020-01-10', 'CTNF', 'abandoned'); self.assertEqual((r['next_event'], r['next_event_date'], r['abandonment_rescinded']), ('ABN2', '2020-12-01', 1))

    def test_interview_and_amendment_before_the_turning_point(self):
        ev = [D('2020-01-10', 0, 'MCTNF'), D('2020-02-01', 1, 'EXIN'), D('2020-02-05', 2, 'A...'), D('2020-03-01', 3, 'RCEX'), D('2020-04-01', 4, 'EXIN')]
        r = oo.derive(ev, '2020-01-10', 'CTNF', 'x'); self.assertEqual((r['next_event'], r['interview_before_next'], r['amendment_before_next'], r['rce_or_appeal_next']), ('RCEX', 1, 1, 1))

    def test_reply_and_supplemental_codes_have_roles_that_their_official_descriptions_support(self):
        for c in ('A...', 'A.NE', 'ELC.', 'A.QU', 'AAF.', 'A.IR', 'SA..', 'SAFR', 'SANF'): self.assertEqual(oo.role_of(c), 'amendment', c); self.assertIn(c, OFFICIAL)
        for c in ('SR..', 'SFR.', 'SRNF', 'REST', 'RRES', 'SRES'): self.assertEqual(oo.role_of(c), 'office_action', c)
        for c in ('MSFR.', 'MSRNF', 'MSRES'): self.assertEqual(oo.role_of(c), 'mail_office_action', c)
        self.assertEqual(oo.role_of('N/AP-NOA'), 'appeal')
        self.assertEqual(OFFICIAL['ELC.'], 'Response to Election / Restriction Filed'); self.assertEqual(OFFICIAL['SFR.'], 'Supplemental Final Rejection')
        ev = [D('2020-01-10', 0, 'MCTNF'), D('2020-02-01', 1, 'ELC.'), D('2020-03-01', 2, 'MN/=.')]; self.assertEqual(oo.derive(ev, '2020-01-10', 'CTNF', 'x')['amendment_before_next'], 1)


def folder(root):
    d = Path(root); syn.write_frame(d)
    rows = [pr('10000001', 'D1', 'CTNF', '2011-01-10', '103'), pr('10000002', 'D2', 'CTNF', '2011-02-10', '103'), pr('10000003', 'D3', 'CTNF', '2011-03-10', '102')]
    syn.gz_jsonl(d / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz', rows)
    ev = {'10000001': [['2011-01-10', 'CTNF'], ['2011-02-01', 'EXIN'], ['2011-03-01', 'MN/=.']],
          '10000002': [['2011-05-20', 'CTNF']],                                                  # 100 days from the action's date: not its event
          '10000003': [['2011-03-10', 'CTNF'], ['2011-04-01', 'ZZZZ'], ['2011-04-02', 'RESC']]}
    syn.gz_jsonl(d / 'corpus' / 'frame-events' / f'{syn.FRAME_ID}.jsonl.gz', [{'application_number': a, 'examiner': syn.EXAMINER, 'art_unit': '1234', 'parents': [], 'events': e} for a, e in ev.items()])
    (d / 'corpus' / 'frame-events' / f'{syn.FRAME_ID}.event-codes.json').write_text(json.dumps({'ZZZZ': 'A code from the provider that this tool has not heard of'}), encoding='utf-8')
    return d


class Coverage(unittest.TestCase):
    def test_the_report_lists_unanchored_actions_unrecognised_codes_with_official_descriptions(self):
        with tempfile.TemporaryDirectory() as t:
            d = folder(t); pg_db.build(data_dir=str(d), say=lambda *_: None)
            out = pg_db.report(d / 'corpus' / 'pg.db', 'coverage', min_n=1)
            self.assertIn('unanchored office action', out); self.assertIn('10000002 / D2', out); self.assertIn('office actions, event anchor: unanchored', out); self.assertIn('office actions, event anchor: ct_near', out)
            self.assertIn('event code with no role in pg_tool/event_codes.json: RESC', out); self.assertIn(OFFICIAL['RESC'], out)                       # an official code the tool gives no role: the official description
            self.assertIn('event code with no role in pg_tool/event_codes.json: ZZZZ', out); self.assertIn('NOT in the official USPTO list', out); self.assertIn('A code from the provider that this tool has not heard of', out)
            self.assertIn('checked against the USPTO PatEx 2022 list', out); self.assertIn('later_events_none_recognised', out); self.assertNotIn('not yet verified', out)
            con = pg_db.connect_ro(d / 'corpus' / 'pg.db')
            try:
                self.assertEqual(con.execute("SELECT anchor_quality, next_event_state FROM oa_outcomes WHERE application='10000002'").fetchall(), [('unanchored', 'unanchored')])
                self.assertEqual(con.execute("SELECT anchor_quality, next_event_state, later_events FROM oa_outcomes WHERE application='10000003'").fetchall(), [('ct_near', 'later_events_none_recognised', 2)])
                self.assertEqual(con.execute("SELECT description, in_official_list, role FROM event_codes WHERE code='CTNF'").fetchall(), [(OFFICIAL['CTNF'], 1, 'office_action')])
                self.assertEqual(con.execute("SELECT in_official_list FROM event_codes WHERE code='ZZZZ'").fetchall(), [(0,)])
            finally: con.close()

    def test_the_database_anchors_on_the_mail_event_with_events_in_mixed_order(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t); syn.write_frame(d)
            syn.gz_jsonl(d / 'corpus' / 'oa-rejections' / f'{syn.FRAME_ID}.jsonl.gz', [pr('10000004', 'D4', 'CTNF', '2011-04-20', '103')])
            ev = [['2011-06-01', 'MN/=.'], ['2011-04-20', 'MCTNF'], ['2011-05-01', 'EXIN'], ['2011-04-16', 'CTNF'], ['2011-05-20', 'A...']]       # the provider's order is not chronological
            syn.gz_jsonl(d / 'corpus' / 'frame-events' / f'{syn.FRAME_ID}.jsonl.gz', [{'application_number': '10000004', 'examiner': syn.EXAMINER, 'art_unit': '1234', 'parents': [], 'events': ev}])
            pg_db.build(data_dir=str(d), say=lambda *_: None)
            con = pg_db.connect_ro(d / 'corpus' / 'pg.db')
            try:
                self.assertEqual(con.execute("SELECT anchor_quality, anchor_code, anchor_date, next_event, interview_before_next, amendment_before_next, next_event_state FROM oa_outcomes").fetchall(),
                                 [('mail_exact', 'MCTNF', '2011-04-20', 'MN/=.', 1, 1, 'recognised')])
            finally: con.close()

    def test_the_finders_action_labels_use_only_anchored_actions(self):
        with tempfile.TemporaryDirectory() as t:
            d = folder(t); pg_db.build(data_dir=str(d), say=lambda *_: None)
            with mock.patch.object(stats, 'MIN_N_FLOOR', 1):
                res = similar.find(d / 'corpus' / 'pg.db', {'statutes': ['103', '102']}, weights=similar.effective_weights(None), min_score=0.0, min_n=1)
            lab = res['applicant_action_labels']
            self.assertEqual(lab['families_with_anchored_event_data'], 2); self.assertEqual(lab['families_unanchored_or_without_event_data'], 1)
            self.assertEqual((lab['no_later_event'], lab['later_events_none_recognised']), (0, 1))
            self.assertIn('checked against the USPTO PatEx 2022 list', lab['note'])


if __name__ == '__main__':
    unittest.main()
