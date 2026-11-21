import hashlib, json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import compact
sha=lambda b: hashlib.sha256(b).hexdigest()

class Compact(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  self.app=self.root/'public-originals'/'16000001'; run=self.app/'ocr'/'r1'; run.mkdir(parents=True)
  self.pdf=b'%PDF-fake'; (self.app/'DOC1.pdf').write_bytes(self.pdf); (self.app/'DOC2.pdf').write_bytes(b'%PDF-no-ocr')
  text='page one\fpage two'.encode()
  (run/'DOC1.txt').write_bytes(text)
  (run/'DOC1.pages.json').write_text(json.dumps({'document_id':'DOC1','original_sha256':sha(self.pdf),'text_sha256':sha(text),
     'pages':[{'page':1,'status':'ocr_ok','mean_confidence':90},{'page':2,'status':'ocr_ok','mean_confidence':80}]}))
 def tearDown(self): self.tmp.cleanup()
 def test_normalize_page_anchored(self):
  compact.normalize(self.app,'r1',root=self.root)
  md=(self.root/'corpus/normalized/16000001/DOC1.md').read_text(encoding='utf-8')
  self.assertIn('## Page 2',md); self.assertIn('page two',md); self.assertIn(sha(self.pdf),md)
 def test_prune_dry_run_then_apply_with_tombstone(self):
  compact.normalize(self.app,'r1',root=self.root)
  r=compact.prune_originals(self.app,root=self.root)
  self.assertFalse(r['applied']); self.assertTrue((self.app/'DOC1.pdf').exists())
  self.assertEqual([x['document_id'] for x in r['refused']],['DOC2'])
  r=compact.prune_originals(self.app,apply=True,root=self.root)
  self.assertFalse((self.app/'DOC1.pdf').exists()); self.assertTrue((self.app/'DOC2.pdf').exists())
  t=json.loads((self.app/'pruned.json').read_text()); self.assertEqual(t['pruned'][0]['original_sha256'],sha(self.pdf))
  self.assertIn('acquire_round.mjs 16000001 1 DOC1',t['pruned'][0]['reacquire'])
 def test_prune_refuses_changed_original(self):
  compact.normalize(self.app,'r1',root=self.root); (self.app/'DOC1.pdf').write_bytes(b'changed')
  r=compact.prune_originals(self.app,apply=True,root=self.root)
  self.assertTrue((self.app/'DOC1.pdf').exists()); self.assertIn('different bytes',r['refused'][0]['reason'])
 def test_prune_refuses_edited_markdown(self):
  compact.normalize(self.app,'r1',root=self.root); (self.root/'corpus/normalized/16000001/DOC1.md').write_text('edited')
  r=compact.prune_originals(self.app,apply=True,root=self.root); self.assertTrue((self.app/'DOC1.pdf').exists())
 def test_ocr_text_tamper_skips_that_document_and_records_it(self):
  (self.app/'ocr'/'r1'/'DOC1.txt').write_bytes(b'x')
  text=b'fine'; run=self.app/'ocr'/'r1'; (run/'DOC3.txt').write_bytes(text)
  (run/'DOC3.pages.json').write_text(json.dumps({'document_id':'DOC3','original_sha256':sha(b'p'),'text_sha256':sha(text),'pages':[{'page':1,'status':'ocr_ok'}]}))
  import io, contextlib
  with contextlib.redirect_stderr(io.StringIO()) as err: idx=compact.normalize(self.app,'r1',root=self.root)
  self.assertEqual(sorted(idx['documents']),['DOC3'])                                  # the good one was done; the tampered one was not touched
  fails=json.loads((self.root/'corpus/normalized/16000001/compact-failures.json').read_text())['failed']
  self.assertIn('DOC1',fails); self.assertIn('hash mismatch',fails['DOC1']); self.assertIn('DOC1 skipped',err.getvalue())
  self.assertFalse((self.root/'corpus/normalized/16000001/DOC1.md').exists())
  self.assertEqual([p.name for p in (self.root/'corpus/normalized/16000001').glob('*.tmp*')],[])    # the index was replaced whole (no temp file left)
 def test_one_bad_xml_archive_does_not_stop_the_others(self):
  import io, tarfile, contextlib
  xml=(Path(__file__).resolve().parent/'fixtures'/'uspto-xml'/'SYNTHETIC-CTNF.xml').read_bytes()
  def tar(path, members):
   buf=io.BytesIO()
   with tarfile.open(fileobj=buf,mode='w') as t:
    for name,data in members: ti=tarfile.TarInfo(name); ti.size=len(data); t.addfile(ti,io.BytesIO(data))
   path.write_bytes(buf.getvalue())
  tar(self.app/'GOOD1.tar',[('16000001/GOOD1/Non-Final Rejection.xml',xml)]); tar(self.app/'BAD1.tar',[('16000001/BAD1/Non-Final Rejection.xml',b'<not xml')])
  with contextlib.redirect_stderr(io.StringIO()): done=compact.normalize_xml(self.app,root=self.root)
  self.assertEqual(done,['GOOD1']); fails=json.loads((self.root/'corpus/normalized/16000001/compact-failures.json').read_text())['failed']; self.assertEqual(list(fails),['BAD1'])
  self.assertIn('GOOD1',json.loads((self.root/'corpus/normalized/16000001/index.json').read_text())['documents'])
 def test_events_are_sorted_by_date_then_code_and_the_providers_order_is_not_kept(self):
  qdir=self.root/'public-originals'/'frames'/'f2'/'q1'; qdir.mkdir(parents=True)
  ev=[('ZZ99','2025-01-02'),('CTNF','2025-01-02'),('MCTNF','2025-01-02'),('AAAA','2025-01-01')]
  raw=json.dumps({'count':1,'patentFileWrapperDataBag':[{'applicationNumberText':'18000002','applicationMetaData':{},'eventDataBag':[{'eventCode':c,'eventDescriptionText':'d','eventDate':d} for c,d in ev],'parentContinuityBag':[]}]}).encode()
  (qdir/'page-000000.json').write_bytes(raw)
  frame={'frame_id':'f2','queries':[{'query_id':'q1','pages':[{'offset':0,'raw_path':'public-originals/frames/f2/q1/page-000000.json','response_sha256':sha(raw)}]}]}
  fp=self.root/'f2.json'; fp.write_text(json.dumps(frame)); compact.compact_frame(fp,root=self.root)
  import gzip; row=json.loads(gzip.decompress((self.root/'corpus/frame-events/f2.jsonl.gz').read_bytes()).decode().splitlines()[0])
  self.assertEqual([c for _,c in row['events']],['AAAA','CTNF','MCTNF','ZZ99'])              # by date then code: the provider's array order is not chronological, so it is not kept
 def test_compact_frame_apply_deletes_only_inside_the_frames_folder(self):
  victim=self.root/'precious.txt'; victim.write_text('keep')
  qdir=self.root/'public-originals'/'frames'/'f3'/'q1'; qdir.mkdir(parents=True); page=qdir/'page-000000.json'; page.write_text('{}')
  for raw_path in ('../../../precious.txt','precious.txt','public-originals/frames/other/q1/x.json'):
   frame={'frame_id':'f3','queries':[{'query_id':'q1','pages':[{'offset':0,'raw_path':raw_path,'response_sha256':sha(b'keep')}]}]}
   fp=self.root/'f3.json'; fp.write_text(json.dumps(frame))
   with self.assertRaises(SystemExit) as c: compact.compact_frame(fp,apply=True,root=self.root)
   self.assertIn('outside',str(c.exception)); self.assertTrue(victim.exists()); self.assertTrue(page.exists())
  frame={'frame_id':'f3','queries':[{'query_id':'../../..','pages':[]}]}; fp.write_text(json.dumps(frame))
  with self.assertRaises(SystemExit): compact.compact_frame(fp,apply=True,root=self.root)
  frame={'frame_id':'CON','queries':[]}; fp.write_text(json.dumps(frame))
  with self.assertRaises(SystemExit): compact.compact_frame(fp,apply=True,root=self.root)
 def test_compact_frame_keeps_events_and_prunes_pages(self):
  qdir=self.root/'public-originals'/'frames'/'f1'/'q1'; qdir.mkdir(parents=True)
  raw=json.dumps({'count':1,'patentFileWrapperDataBag':[{'applicationNumberText':'18000001','applicationMetaData':{'examinerNameText':'X'},
     'eventDataBag':[{'eventCode':'CTNF','eventDescriptionText':'Non-Final Rejection','eventDate':'2025-01-02'},{'eventCode':'A...','eventDescriptionText':'Response','eventDate':'2025-03-01'}],'parentContinuityBag':[]}]}).encode()
  (qdir/'page-000000.json').write_bytes(raw)
  frame={'frame_id':'f1','queries':[{'query_id':'q1','pages':[{'offset':0,'limit':50,'rows':1,'response_sha256':sha(raw),'raw_path':'public-originals/frames/f1/q1/page-000000.json'}]}]}
  fp=self.root/'f.json'; fp.write_text(json.dumps(frame))
  r=compact.compact_frame(fp,root=self.root); self.assertFalse(r['applied']); self.assertTrue((qdir/'page-000000.json').exists())
  import gzip; row=json.loads(gzip.decompress((self.root/'corpus/frame-events/f1.jsonl.gz').read_bytes()).decode().splitlines()[0]); self.assertEqual(row['events'][0],['2025-01-02','CTNF'])
  r=compact.compact_frame(fp,apply=True,root=self.root); self.assertFalse((qdir/'page-000000.json').exists()); self.assertTrue((qdir/'pruned.json').exists())
  with self.assertRaises(SystemExit): compact.compact_frame(fp,root=self.root)
 def test_xml_archive_supersedes_ocr_and_prunes_with_xml_tombstone(self):
  import io, tarfile
  compact.normalize(self.app,'r1',root=self.root)
  xml=(Path(__file__).resolve().parent/'fixtures'/'uspto-xml'/'SYNTHETIC-CTNF.xml').read_bytes()
  buf=io.BytesIO()
  with tarfile.open(fileobj=buf,mode='w') as t:
   for name,data in [('16000001/DOC1/Non-Final Rejection.xml',xml),('16000001/DOC1/x_svg.zip',b'PK\x03\x04')]:
    ti=tarfile.TarInfo(name); ti.size=len(data); t.addfile(ti,io.BytesIO(data))
  (self.app/'DOC1.tar').write_bytes(buf.getvalue())
  self.assertEqual(compact.normalize_xml(self.app,root=self.root),['DOC1'])
  idx=json.loads((self.root/'corpus/normalized/16000001/index.json').read_text())['documents']['DOC1']
  self.assertEqual(idx['source'],'uspto-xmlarchive'); self.assertIn('ocr_run',idx['superseded'])
  self.assertIn('native USPTO XML text',(self.root/'corpus/normalized/16000001/DOC1.md').read_text(encoding='utf-8'))
  compact.normalize(self.app,'r1',root=self.root)  # OCR re-normalization must not clobber the XML rendering
  self.assertEqual(json.loads((self.root/'corpus/normalized/16000001/index.json').read_text())['documents']['DOC1']['source'],'uspto-xmlarchive')
  r=compact.prune_originals(self.app,apply=True,root=self.root)
  self.assertFalse((self.app/'DOC1.tar').exists())
  t=json.loads((self.app/'pruned.json').read_text())['pruned']
  self.assertTrue(any(p['document_id']=='DOC1' and p['reacquire'].endswith('DOC1@xml') and p['xml_sha256']==idx['xml_sha256'] for p in t))
 def test_bundled_xml_parts_render_matching_type_first(self):
  import io, tarfile
  fx=Path(__file__).resolve().parent/'fixtures'/'uspto-xml'
  buf=io.BytesIO()
  with tarfile.open(fileobj=buf,mode='w') as t:
   for name,data in [('16000001/DOC2/a.SPEC.XML',(fx/'SYNTHETIC-CTNF.xml').read_bytes()),('16000001/DOC2/a.REM.XML',(fx/'SYNTHETIC-REM.xml').read_bytes())]:
    ti=tarfile.TarInfo(name); ti.size=len(data); t.addfile(ti,io.BytesIO(data))
  (self.app/'DOC2.tar').write_bytes(buf.getvalue())
  compact.normalize_xml(self.app,root=self.root)
  e=json.loads((self.root/'corpus/normalized/16000001/index.json').read_text())['documents']['DOC2']
  self.assertEqual([(p['file'],p['code'],p['role']) for p in e['parts']],[('DOC2.md','REM','primary'),('DOC2.CTNF.md','CTNF','bundled')])
  self.assertIn('## REMARKS',(self.root/'corpus/normalized/16000001/DOC2.md').read_text(encoding='utf-8'))
  other=(self.root/'corpus/normalized/16000001/DOC2.CTNF.md').read_text(encoding='utf-8')
  self.assertIn('part_role: "bundled"',other); self.assertNotIn('## REMARKS',other)
  self.assertIn('component: "office_action"',other)
if __name__=='__main__': unittest.main()
