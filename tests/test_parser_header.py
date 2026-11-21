"""Parser 1.1: header facts with how each was found, reference normalisation to document numbers (names are never matchable),
the shipped sample passing the schema, and pg_assess_office_action returning partial when the schema check fails."""
import copy, json, unittest
from pathlib import Path
from unittest import mock

from pg_tool import oa_parse, service
from pg_tool.validate import errors as schema_errors

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (ROOT / 'examples' / 'sample' / 'office-action.txt').read_text(encoding='utf-8')
SCHEMA = json.loads((ROOT / 'schemas' / 'office-action-analysis.schema.json').read_text(encoding='utf-8'))


class Header(unittest.TestCase):
    def test_sample_header_and_how(self):
        h = oa_parse.extract_header(SAMPLE)
        self.assertEqual(h['art_unit']['value'], '9999'); self.assertIn('Art Unit', h['art_unit']['how'])
        self.assertEqual(h['application_number']['value'], '99000001'); self.assertIn('Application No.', h['application_number']['how'])
        self.assertEqual(h['mail_date']['value'], '2026-01-15'); self.assertIn('Notification Date', h['mail_date']['how'])
        self.assertEqual(h['examiner']['value'], 'Alex Example')
        for k, v in h.items():
            s, e = v['offsets']; self.assertTrue(SAMPLE[s:e])      # offsets point into the text
        self.assertEqual(SAMPLE[h['art_unit']['offsets'][0]:h['art_unit']['offsets'][1]], '9999')

    def test_other_layouts(self):
        t = 'Application/Control Number: 16/123,456\nArt Unit: 2121\nExaminer: DOE, JANE\nDATE MAILED: 03/04/2024\n\nbody\n/JANE DOE/\nPrimary Examiner, Art Unit 2121\n'
        h = oa_parse.extract_header(t)
        self.assertEqual((h['application_number']['value'], h['art_unit']['value'], h['mail_date']['value'], h['examiner']['value']), ('16123456', '2121', '2024-03-04', 'DOE, JANE'))
        self.assertEqual(h['examiner']['other_values'], ['JANE DOE'])         # the signature block spells the name differently: both are reported
        self.assertEqual(oa_parse.extract_header('Mail Date: January 5, 2024\n')['mail_date']['value'], '2024-01-05')
        self.assertEqual(oa_parse.extract_header('Notification Date 2024-02-29')['mail_date']['value'], '2024-02-29')

    def test_signature_block_only(self):
        h = oa_parse.extract_header('Claims 1-3 are rejected.\n\n/Alex Example/\nPrimary Examiner, Art Unit 1234\n')
        self.assertEqual(h['examiner']['value'], 'Alex Example'); self.assertIn('signature block', h['examiner']['how'])
        self.assertEqual(h['art_unit']['value'], '1234'); self.assertIn('Art Unit', h['art_unit']['how'])

    def test_supervisory_examiner_is_not_the_examiner(self):
        h = oa_parse.extract_header('Examiner: Real Person\n\n/Boss Person/\nSupervisory Patent Examiner, Art Unit 1234\n')
        self.assertEqual(h['examiner']['value'], 'Real Person'); self.assertNotIn('other_values', h['examiner'])
        self.assertNotIn('examiner', oa_parse.extract_header('/Boss Person/\nSupervisory Patent Examiner, Art Unit 1234\n'))

    def test_impossible_dates_and_garbage_are_not_extracted(self):
        for t in ('Notification Date: 02/30/2024', 'Notification Date: 13/01/2024', 'Notification Date: Foo 5, 2024', 'Art Unit abc', 'Mailing date of this action'):
            h = oa_parse.extract_header(t); self.assertEqual(h, {}, t)
        self.assertEqual(oa_parse.extract_header(''), {}); self.assertEqual(oa_parse.extract_header(None), {})

    def test_conflicting_values_are_reported_not_hidden(self):
        h = oa_parse.extract_header('Art Unit: 1111\nPage 2 Art Unit: 2222\n')
        self.assertEqual(h['art_unit']['value'], '1111'); self.assertEqual(h['art_unit']['other_values'], ['2222'])

    def test_supplied_mail_date_wins_and_the_text_value_is_kept(self):
        r = oa_parse.parse(SAMPLE, mail_date='2026-02-01')
        self.assertEqual(r['analysis']['action']['mail_date'], '2026-02-01')
        self.assertEqual(r['header']['mail_date']['text_value'], '2026-01-15'); self.assertIn('supplied', r['header']['mail_date']['how'])

    def test_parse_uses_header_for_display_fields_only(self):
        r = oa_parse.parse(SAMPLE)
        a = r['analysis']
        self.assertEqual((a['application']['art_unit'], a['application']['examiner'], a['action']['mail_date']), ('9999', 'Alex Example', '2026-01-15'))
        self.assertEqual(a['application']['number'], '00000000')   # the application number in the text never becomes the request's application
        self.assertNotIn('mail date not determined', ' '.join(r['not_recognised']['notes']))
        self.assertIn('no provider or network query', r['header']['note'])
        r2 = oa_parse.parse(SAMPLE, application_number='99000001'); self.assertTrue(r2['header']['application_number']['matches_request'])
        r3 = oa_parse.parse(SAMPLE, application_number='11111111'); self.assertFalse(r3['header']['application_number']['matches_request'])
        self.assertIn('mail date not determined', ' '.join(oa_parse.parse('Claim 1 is rejected under 35 U.S.C. 103 as being unpatentable over Roe.')['not_recognised']['notes']))


class References(unittest.TestCase):
    def test_numbers_are_normalised(self):
        cases = {'Example (US 2019/0000001)': 'US20190000001', 'US 2019/0123456 A1': 'US20190123456', 'US20190123456 A1': 'US20190123456',
                 'Sample (US 10,000,002)': 'US10000002', 'US 7,123,456 B2': 'US7123456', 'U.S. Pat. No. 6,123,456': 'US6123456', '10,000,002': 'US10000002',
                 'US RE12,345': 'USRE12345', 'Demo (WO 2018/000003)': 'WO2018000003', 'WO2018/000003 A1': 'WO2018000003', 'JP 2009-123456 A': 'JP2009123456',
                 'EP 1 234 567 A1': 'EP1234567', 'KR 10-2015-0001234': 'KR1020150001234', 'CN 109123456 A': 'CN109123456'}
        for cit, num in cases.items():
            r = oa_parse.normalize_reference(cit); self.assertEqual(r['document_number'], num, cit); self.assertTrue(r['matchable'])
        self.assertEqual(oa_parse.normalize_reference('US 10,000,002')['document_number'], oa_parse.normalize_reference('U.S. Patent No. 10000002')['document_number'])

    def test_surname_only_references_are_unnormalised_and_never_matchable(self):
        for cit in ('Smith', 'Smith et al.', 'Jones, "Soil sensing", 2019', 'AAPA', 'Applicant admitted prior art', '', None, 'US 2019/012345 A1'):
            r = oa_parse.normalize_reference(cit)
            self.assertIsNone(r['document_number'], cit); self.assertFalse(r['matchable'], cit)
            self.assertIn('name only', r['how'])
        self.assertEqual(oa_parse.normalize_reference('Applicant admitted prior art')['kind'], 'applicant_admitted_prior_art')

    def test_references_in_an_analysis_carry_match_basis_and_matchable_numbers_skip_names(self):
        t = ('Claims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Roe in view of Example (US 2019/0000001) and Smith.\n'
             'Claim 4 is rejected under 35 U.S.C. 102 as being anticipated by Roe.')
        a = oa_parse.parse(t)['analysis']
        by = {r['id']: r for r in a['references']}
        self.assertEqual(by['Roe']['match_basis'], 'name_only'); self.assertNotIn('document_number', by['Roe'])
        self.assertEqual(by['Example']['match_basis'], 'document_number'); self.assertEqual(by['Example']['document_number'], 'US20190000001')
        self.assertEqual(oa_parse.matchable_numbers(a['references']), {'US20190000001'})
        self.assertEqual(oa_parse.matchable_numbers([{'document_number': 'US1', 'match_basis': 'name_only'}]), set())   # a name-only entry never contributes
        self.assertEqual(schema_errors(a, SCHEMA), [])


class SampleAndPartial(unittest.TestCase):
    def test_the_shipped_sample_passes_the_schema(self):
        e = service.call('pg_assess_office_action', {'text': SAMPLE})
        self.assertEqual(e['status'], 'ok'); self.assertTrue(e['data']['schema_conformance']['valid'], e['data']['schema_conformance']['errors'])
        self.assertEqual(e['warnings'], [])
        self.assertEqual(e['data']['parser']['version'], '1.1.0')
        e = service.call('pg_assess_office_action', {'text': SAMPLE.replace('Notification Date: 01/15/2026', '')})
        self.assertTrue(e['data']['schema_conformance']['valid'])       # a mail date is optional: it is extracted when present

    def test_a_schema_failure_returns_partial_with_a_warning(self):
        real = oa_parse.parse
        def broken(*a, **k):
            r = real(*a, **k); r['analysis']['rejections'][0]['statute'] = 'not-a-statute'; return r
        with mock.patch.object(oa_parse, 'parse', broken):
            e = service.call('pg_assess_office_action', {'text': SAMPLE})
        self.assertEqual(e['status'], 'partial'); self.assertFalse(e['data']['schema_conformance']['valid'])
        self.assertTrue(e['warnings']); self.assertIn('does not conform', e['warnings'][0])
        self.assertTrue(e['data']['schema_conformance']['errors'])

    def test_the_example_analysis_still_validates(self):
        self.assertEqual(schema_errors(json.loads((ROOT / 'examples' / 'oa-analysis.example.json').read_text(encoding='utf-8')), SCHEMA), [])


if __name__ == '__main__':
    unittest.main()
