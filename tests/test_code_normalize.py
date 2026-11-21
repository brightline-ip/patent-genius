import unittest
from xml_to_md import normalize_code, classify


class CodeNormalize(unittest.TestCase):
    def test_hex_escaped_member_names_and_underscores(self):
        self.assertEqual(normalize_code('AP+2eB'), 'AP.B')
        self.assertEqual(normalize_code('AP_B'), 'AP.B')                  # known COMPONENT code
        self.assertEqual(normalize_code('N_AP', 'N/AP'), 'N/AP')          # matches the inventory spelling
        self.assertEqual(normalize_code('A_NE_AFCP'), 'A.NE.AFCP')
        self.assertEqual(normalize_code('APP_TEXT'), 'APP.TEXT')
        self.assertEqual(normalize_code('TRAN_LET'), 'TRAN.LET')

    def test_existing_codes_unchanged(self):
        for c in ('CTNF', 'REM', 'A...', 'CLM', 'NOA', 'CNTA', 'SPEC', 'XYZ_UNKNOWN'):
            self.assertEqual(normalize_code(c), c)
        self.assertIsNone(normalize_code(None))

    def test_classify_appeal_brief_member(self):
        parsed = {'meta': {}, 'kind': 'IncomingDocument', 'blocks': ['## Appeal Brief', 'The examiner erred.']}
        code, component, source, _ = classify('16872589.08-17-2022.L6XXPJ15GREENX0.AP+2eB.XML', parsed, 'AP.B')
        self.assertEqual(classify('x.01-01-2020.D.CTNF.XML', {'meta': {'DocumentCode': 'AP_B'}, 'kind': 'IncomingDocument', 'blocks': []}, 'AP.B')[:3], ('AP.B', 'appeal_brief', 'metadata'))
        self.assertEqual((code, component, source), ('AP.B', 'appeal_brief', 'member_name'))


if __name__ == '__main__':
    unittest.main()
