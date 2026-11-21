import io, sys, tarfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xml_to_md import parse, to_markdown, xml_members

FIX = Path(__file__).resolve().parent / 'fixtures' / 'uspto-xml'


def tar_of(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == 'sym': info.type = tarfile.SYMTYPE; info.linkname = '/etc/passwd'; t.addfile(info)
            else: info.size = len(data); t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class XmlToMd(unittest.TestCase):
    def test_outgoing_native_text(self):
        md, sha = to_markdown((FIX / 'SYNTHETIC-CTNF.xml').read_bytes(), app='99000001', doc='SYNTHCTNF0001', member='m', archive_sha256='0' * 64)
        self.assertIn('native USPTO XML text', md); self.assertIn('Response to RCE', md)
        self.assertNotIn('07-42-05', md)  # form paragraph numbers are dropped
        self.assertEqual(len(sha), 64)

    def test_incoming_ocr_labelled_with_headings(self):
        md, _ = to_markdown((FIX / 'SYNTHETIC-REM.xml').read_bytes(), app='99000001', doc='SYNTHREM0001', member='m', archive_sha256='0' * 64)
        self.assertIn('USPTO OCR of an incoming filing', md); self.assertIn('## REMARKS', md); self.assertIn('## STATUS OF CLAIMS', md)
        self.assertIn('document_code: "REM"', md); self.assertIn('component: "remarks"', md); self.assertIn('§', md)  # section sign survives

    def test_dtd_and_entities_refused(self):
        for bad in [b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>', b'<!ENTITY x SYSTEM "file:///etc/passwd"><x/>']:
            with self.assertRaises(ValueError): parse(bad)

    def test_archive_members_bounded_and_identity_bound(self):
        good = (FIX / 'SYNTHETIC-REM.xml').read_bytes()
        t = tar_of([('99000001/D1/a.REM.XML', good, 'f'), ('99000001/D1/a.REM_svg.zip', b'PK\x03\x04', 'f'),
                    ('../evil.xml', b'<x/>', 'f'), ('/abs.xml', b'<x/>', 'f'), ('99000001/D1/link.xml', b'', 'sym'),
                    ('99000001/OTHER/b.xml', b'<x/>', 'f'), ('99000001/D1/deep/c.xml', b'<x/>', 'f')])
        self.assertEqual([n for n, _ in xml_members(t, '99000001', 'D1')], ['a.REM.XML'])
        with self.assertRaises(ValueError): xml_members(tar_of([('99000001/D1/a.zip', b'PK', 'f')]), '99000001', 'D1')
        many = tar_of([(f'99000001/D1/{i}.xml', b'<x/>', 'f') for i in range(51)])
        with self.assertRaises(ValueError): xml_members(many, '99000001', 'D1')

    def test_many_image_members_do_not_count_against_the_xml_member_cap(self):
        good = (FIX / 'SYNTHETIC-REM.xml').read_bytes()
        many_images = tar_of([('99000001/D1/a.REM.XML', good, 'f')] + [(f'99000001/D1/formula-{i}.svg', b'<svg/>', 'f') for i in range(325)])
        self.assertEqual([n for n, _ in xml_members(many_images, '99000001', 'D1')], ['a.REM.XML'])   # 326 members, one XML
        too_many = tar_of([('99000001/D1/a.REM.XML', good, 'f')] + [(f'99000001/D1/x{i}.svg', b'<svg/>', 'f') for i in range(2000)])
        with self.assertRaises(ValueError): xml_members(too_many, '99000001', 'D1')                  # header parsing is still bounded

    def test_docx_entry_cap_allows_documents_with_many_embedded_objects(self):
        import zipfile
        from xml_to_md import docx_parts
        W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        def docx(extra):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('word/document.xml', f'<w:document {W}><w:body><w:p><w:r><w:t>CLAIMS</w:t></w:r></w:p><w:p><w:r><w:t>A widget.</w:t></w:r></w:p></w:body></w:document>')
                for i in range(extra): z.writestr(f'word/media/image{i}.png', b'x')
            return buf.getvalue()
        docx_parts(docx(286), app='1', doc='D', inventory_code='APP.TEXT', original_sha256='0')      # 287 entries
        with self.assertRaises(ValueError): docx_parts(docx(2001), app='1', doc='D', inventory_code='APP.TEXT', original_sha256='0')


class Components(unittest.TestCase):
    NS = 'xmlns:uspat="urn:us:gov:doc:uspto:patent" xmlns:com="http://www.wipo.int/standards/XMLSchema/ST96/Common" xmlns:pat="http://www.wipo.int/standards/XMLSchema/ST96/Patent"'

    def doc(self, root, code, body):
        return (f'<?xml version="1.0"?><uspat:{root} {self.NS}><uspat:DocumentMetadata><uspat:DocumentCode>{code}</uspat:DocumentCode>'
                f'<uspat:OfficialDate>2026-01-02</uspat:OfficialDate></uspat:DocumentMetadata>{body}</uspat:{root}>').encode()

    def test_claims_render_per_claim_with_status_dependency_and_markup(self):
        from xml_to_md import archive_parts
        x = self.doc('ClaimsDocument', 'CLM', '<uspat:Claims><uspat:Claim com:id="CLM-00001"><pat:ClaimNumber>1</pat:ClaimNumber>'
                     '<pat:ClaimStatusCategory>Currently amended</pat:ClaimStatusCategory><uspat:ClaimText>1. A widget comprising:'
                     '<uspat:ClaimText>a <com:Ins>blue </com:Ins><com:Del>red </com:Del>part.</uspat:ClaimText></uspat:ClaimText></uspat:Claim>'
                     '<uspat:Claim com:id="CLM-00002"><pat:ClaimNumber>2</pat:ClaimNumber><pat:ClaimStatusCategory>Original</pat:ClaimStatusCategory>'
                     '<uspat:ClaimText>2. The widget of <uspat:ClaimReference com:idrefs="CLM-00001">claim 1</uspat:ClaimReference>, round.</uspat:ClaimText></uspat:Claim></uspat:Claims>')
        md = archive_parts([('a.CLM.XML', x)], app='1', doc='D', inventory_code='CLM', archive_sha256='0')[0]['markdown']
        self.assertIn('### Claim 1 (Currently amended)', md); self.assertIn('### Claim 2 (Original) - depends on claim 1', md)
        self.assertIn('a <ins>blue </ins>~~red ~~part.', md); self.assertIn('component: "claims"', md)

    def test_content_overrides_contradicting_code_and_duplicates_are_flagged(self):
        from xml_to_md import archive_parts
        spec = ''.join(f'<uspat:Heading>{h}</uspat:Heading><uspat:P>text {i} about the widget assembly</uspat:P>' for i, h in enumerate(
            ['FIELD', 'BACKGROUND', 'SUMMARY', 'DETAILED DESCRIPTION']))
        parts = archive_parts([('x.REM.XML', self.doc('IncomingDocument', 'REM', spec)), ('x.SPEC.XML', self.doc('SpecificationDocument', 'SPEC', spec))],
                              app='1', doc='D', inventory_code='REM', archive_sha256='0')
        self.assertEqual([(p['code'], p['component'], p['role']) for p in parts], [('REM', 'specification', 'primary'), ('SPEC', 'specification', 'bundled')])
        self.assertTrue(any('component_conflict' in w for w in parts[0]['warnings']))
        self.assertTrue(any('near-duplicate' in w for w in parts[1]['warnings']))

    def test_real_remarks_keep_remarks_label(self):
        from xml_to_md import archive_parts
        p = archive_parts([('m', (FIX / 'SYNTHETIC-REM.xml').read_bytes())], app='99000001', doc='SYNTHREM0001', inventory_code='REM', archive_sha256='0')[0]
        self.assertEqual((p['component'], p['warnings']), ('remarks', []))

    def test_docx_split_into_spec_claims_abstract_and_deleted_revisions_dropped(self):
        import zipfile
        from xml_to_md import docx_parts
        W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        paras = ['Title', 'BACKGROUND', 'Some description.', 'CLAIMS', 'A widget.', 'ABSTRACT', 'A short abstract.']
        body = ''.join(f'<w:p><w:r><w:t>{t}</w:t></w:r></w:p>' for t in paras) + '<w:p><w:del><w:r><w:delText>gone</w:delText></w:r></w:del></w:p>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z: z.writestr('word/document.xml', f'<w:document {W}><w:body>{body}</w:body></w:document>')
        parts = docx_parts(buf.getvalue(), app='1', doc='D', inventory_code='APP.TEXT', original_sha256='0')
        self.assertEqual([(p['code'], p['component']) for p in parts], [('SPEC', 'specification'), ('CLM', 'claims'), ('ABST', 'abstract')])
        self.assertIn('A widget.', parts[1]['markdown']); self.assertNotIn('A widget.', parts[0]['markdown'])
        self.assertNotIn('gone', ''.join(p['markdown'] for p in parts))
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, 'w') as z: z.writestr('word/document.xml', '<!DOCTYPE x [<!ENTITY a "b">]><x/>')
        with self.assertRaises(ValueError): docx_parts(bomb.getvalue(), app='1', doc='D', inventory_code='APP.TEXT', original_sha256='0')


if __name__ == '__main__': unittest.main()
