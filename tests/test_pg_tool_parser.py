"""pg_tool.oa_parse on synthetic office-action text (newly written fixtures, not copied from any action)."""
import unittest
from pg_tool import oa_parse

AIA_FINAL = """Notice of Pre-AIA or AIA Status
The present application, filed on or after March 16, 2013, is being examined under the first inventor to file provisions of the AIA.

Claim Rejections - 35 USC § 112
Claims 3 and 9 are rejected under 35 U.S.C. 112(b) or 35 U.S.C. 112 (pre-AIA), second paragraph, as being indefinite for failing to particularly point out the invention.
Claim 12 is rejected under 35 U.S.C. 112(a) or 35 U.S.C. 112 (pre-AIA), first paragraph, as failing to comply with the written description requirement.

Claim Rejections - 35 USC § 101
Claims 1-12 are rejected under 35 U.S.C. 101 because the claimed invention is directed to an abstract idea without significantly more.

Claim Rejections - 35 USC § 103
Claims 1-4, 6-8 and 10 are rejected under 35 U.S.C. 103 as being unpatentable over Alder et al. (US 2019/0123456) in view of Birch (US 10,234,567).
Claim 5 is rejected under 35 U.S.C. 103 as being unpatentable over Alder et al. (US 2019/0123456) in view of Birch (US 10,234,567), and further in view of Cole (WO 2018/111111).

Double Patenting
Claims 1-12 are provisionally rejected on the ground of nonstatutory double patenting as being unpatentable over claims 1-10 of copending Application No. 17/999,999.

Claim Objections
Claim 11 is objected to because of the following informalities: a typographical error. Appropriate correction is required.

Allowable Subject Matter
Claims 13-14 are objected to as being dependent upon a rejected base claim, but would be allowable if rewritten in independent form including all of the limitations of the base claim and any intervening claims.

Response to Arguments
Applicant's arguments filed 01/02/2024 have been fully considered. Claims 1-12 are rejected under 35 U.S.C. 102(a)(1) as discussed in the previous action, and that rejection is maintained in modified form above.
The rejection of claims 15-16 under 35 U.S.C. 112(b) is withdrawn. Claims 15-16 are rejected under 35 U.S.C. 112(d) as withdrawn in view of the amendment.

Applicant's amendment necessitated the new ground(s) of rejection presented in this Office action. Accordingly, THIS ACTION IS MADE FINAL. See MPEP § 706.07(a).
A SHORTENED STATUTORY PERIOD FOR REPLY TO THIS FINAL ACTION IS SET TO EXPIRE THREE MONTHS FROM THE MAILING DATE OF THIS ACTION.
"""

PRE_AIA = """The present application is being examined under the pre-AIA first to invent provisions.
Claims 1, 2 & 4 are rejected under 35 U.S.C. 102(b) as being anticipated by Dunn (US 5,555,555).
Claims 1-6 are rejected under 35 U.S.C. 103(a) as being unpatentable over Dunn in view of Ellis.
Claims 7 to 8 are rejected under 35 U.S.C. 112, second paragraph, as being indefinite.
This action is non-final.
"""

RESTRICTION = """Election/Restrictions
Restriction to one of the following inventions is required under 35 U.S.C. 121:
I. Claims 1-10, drawn to a semiconductor device, classified in H10D 30/00.
II. Claims 11-20, drawn to a method of making a semiconductor device, classified in H10D 84/00.
"""


class Parser(unittest.TestCase):
    def test_claim_expansion(self):
        self.assertEqual(oa_parse.expand_claims('Claims 1-4, 6-8, 11 and 13'), [1, 2, 3, 4, 6, 7, 8, 11, 13])
        self.assertEqual(oa_parse.expand_claims('Claims 2, 3 & 4'), [2, 3, 4])
        self.assertEqual(oa_parse.expand_claims('Claims 1 – 3'), [1, 2, 3])
        self.assertEqual(oa_parse.expand_claims('Claim 5000'), [])

    def test_aia_final(self):
        r = oa_parse.parse(AIA_FINAL, application_number='17000000', document_id='DOC1', mail_date='2024-03-01')
        a, f = r['analysis'], r['facts']
        self.assertEqual(f['finality'], 'final'); self.assertEqual(f['aia_status'], 'aia')
        self.assertEqual(a['action']['shortened_statutory_period_months'], 3)
        st = [(x['statute'], x['claims']) for x in a['rejections']]
        self.assertIn(('112(b)', [3, 9]), st); self.assertIn(('112(a)', [12]), st); self.assertIn(('101', list(range(1, 13))), st)
        self.assertIn(('103', [1, 2, 3, 4, 6, 7, 8, 10]), st); self.assertIn(('103', [5]), st)
        dp = next(x for x in a['rejections'] if x['statute'].startswith('double'))
        self.assertEqual(dp['statute'], 'double_patenting_nonstatutory'); self.assertTrue(dp.get('provisional'))
        r5 = next(x for x in a['rejections'] if x['claims'] == [5])
        self.assertEqual(len(r5['reference_ids']), 3)
        kinds = {x['id']: x['kind'] for x in a['references']}
        self.assertEqual(kinds['Alder'], 'us_publication'); self.assertEqual(kinds['Birch'], 'us_patent'); self.assertEqual(kinds['Cole'], 'foreign')
        self.assertTrue(a['allowable_subject_matter']['indicated']); self.assertEqual(a['allowable_subject_matter']['claims'], [13, 14])
        self.assertTrue(any(o.get('allowable_if_rewritten') for o in a['objections']))
        self.assertTrue(any(o['kind'] == 'claim' and o.get('categories') == ['informality_typo_grammar'] for o in a['objections']))

    def test_recap_and_withdrawn_are_not_live_grounds(self):
        r = oa_parse.parse(AIA_FINAL)
        self.assertFalse(any(x['statute'] == '102' for x in r['analysis']['rejections']))
        self.assertFalse(any(x['statute'] == '112(d)' for x in r['analysis']['rejections']))
        reasons = {x['reason'] for x in r['not_recognised']['ignored_rejection_sentences']}
        self.assertEqual(reasons, {'recap', 'withdrawn'})

    def test_offsets_point_into_input(self):
        r = oa_parse.parse(AIA_FINAL)
        for x in r['analysis']['rejections']:
            lo = x['locator']; self.assertTrue(AIA_FINAL[lo['char_start']:lo['char_end']].lower().startswith('claim'))

    def test_pre_aia_forms(self):
        r = oa_parse.parse(PRE_AIA)
        st = {x['statute']: x for x in r['analysis']['rejections']}
        self.assertEqual(r['facts']['aia_status'], 'pre_aia'); self.assertEqual(r['facts']['finality'], 'non_final')
        self.assertEqual(st['102']['claims'], [1, 2, 4]); self.assertIn('102(b)', st['102']['statutory_subsection'])
        self.assertEqual(st['103']['claims'], [1, 2, 3, 4, 5, 6]); self.assertEqual(st['112(b)']['claims'], [7, 8])

    def test_restriction_groups(self):
        r = oa_parse.parse(RESTRICTION)
        res = r['analysis']['restriction']
        self.assertEqual(res['kind'], 'restriction')
        self.assertEqual([g['claims'] for g in res['groups']], [list(range(1, 11)), list(range(11, 21))])
        self.assertEqual(r['analysis']['action']['kind'], 'restriction_election')

    def test_heading_only_fallback_and_honesty(self):
        t = 'Claim Rejections - 35 USC 103\nThe claims are rejected for reasons stated in the attached form.\n'
        r = oa_parse.parse(t)
        x = r['analysis']['rejections'][0]
        self.assertEqual((x['statute'], x['claims']), ('103', []))
        self.assertEqual(r['annotations'][x['id']]['basis'], 'heading_only')
        self.assertEqual(r['analysis']['provenance']['method'], 'rule_extracted'); self.assertFalse(r['analysis']['provenance']['human_verified'])

    def test_unrecognised_text(self):
        r = oa_parse.parse('This is a grocery list.')
        self.assertFalse(r['analysis']['rejections']); self.assertTrue(r['not_recognised']['notes'])
        self.assertEqual(r['facts']['finality'], 'unknown')

    def test_summary_lines_give_unassigned_claims(self):
        t = 'Claim(s) 1-5 is/are rejected.\nClaims 1-3 are rejected under 35 U.S.C. 103 as being unpatentable over Fox.\n'
        r = oa_parse.parse(t)
        self.assertEqual(r['not_recognised']['rejected_claims_without_ground'], [4, 5])


if __name__ == '__main__':
    unittest.main()
