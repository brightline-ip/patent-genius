import hashlib, json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frame_build import build, compact_cpc, cpc_in

def rec(app,fd,cpcs,typ='UTL',parents=(),examiner=None):
 return {'applicationNumberText':app,'applicationMetaData':{'filingDate':fd,'cpcClassificationBag':list(cpcs),'applicationTypeCode':typ,**({'examinerNameText':examiner} if examiner else {})},
         'parentContinuityBag':[{'parentApplicationNumberText':p,'parentApplicationFilingDate':d,'claimParentageTypeCode':'CON'} for p,d in parents]}

class FrameBuild(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name)
  self.plan={'frame_id':'t','date_convention':'test; half-open','windows':[{'id':'w1','from':'2024-09-01','to':'2024-10-01','basis':'filing_date'}],
   'cpc':{'hierarchy_version':'2026.08','strata':{'ai':{'seed_symbols':['G06N3/02'],'expanded_symbols':['G06N3/00 main group'],'match_prefixes':['G06N3/']}}},
   'queries':[{'query_id':'q1','stratum':'ai','window_id':'w1'}]}
 def tearDown(self): self.tmp.cleanup()
 def harvest(self,qid,records,count=None,error=None):
  d=self.base/'public-originals'/'frames'/'t'/qid; d.mkdir(parents=True)
  raw=json.dumps({'count':count if count is not None else len(records),'patentFileWrapperDataBag':records}).encode()
  (d/'page-000000.json').write_bytes(raw)
  rc={'route':'uspto','q':'Q','page_limit':100,'retrieved_at':'2026-10-16T00:00:00Z','provider_count':count if count is not None else len(records),'max_rows':1000,
      'truncated':count is not None and count>len(records),'error':error,
      'pages':[{'offset':0,'limit':100,'rows':len(records),'response_sha256':hashlib.sha256(raw).hexdigest(),'raw_path':'x/page-000000.json'}]}
  (d/'receipt.json').write_text(json.dumps(rc)); return d
 def test_compact_and_boundary(self):
  self.assertEqual(compact_cpc('G06N   3/08'),'G06N3/08')
  self.assertTrue(cpc_in('G06N3/08',['G06N3/'])); self.assertFalse(cpc_in('G06N30/00',['G06N3/'])); self.assertFalse(cpc_in('G06N123/1',['G06N3/']))
  self.assertTrue(cpc_in('G06N20/00',['G06N']))
 def test_valid_frame_with_postfilter_and_family(self):
  self.harvest('q1',[rec('18000001','2024-09-05',['G06N   3/08']),rec('18000002','2024-09-06',['G06N 123/10']),
                     rec('18000003','2024-09-07',['G06N   3/04'],parents=[('17000001','2021-01-01'),('16000001','2019-01-01')]),
                     rec('18000004','2024-09-08',['G06N   3/04'],typ='DES'),rec('18000005','2024-10-01',['G06N   3/04'])])
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[])
  by={r['application_number']:r for r in f['rows']}
  self.assertTrue(by['18000001']['eligible']); self.assertIn('post-filter',by['18000002']['exclusion_reason'])
  self.assertEqual(by['18000003']['family_key'],'16000001'); self.assertIn('utility',by['18000004']['exclusion_reason'])
  self.assertNotIn('18000005',by); self.assertTrue(any(x['application_number']=='18000005' and 'outside window' in x['reason'] for x in f['exclusions']))
  self.assertEqual(f['funnel'],{'candidates':5,'deduplicated':4,'eligible':2,'excluded':2,'families':2,'selected':0})
 def test_examiner_name_is_kept_on_the_row(self):
  self.harvest('q1',[rec('18000001','2024-09-05',['G06N   3/08'],examiner='ROE, RICK'),rec('18000002','2024-09-06',['G06N   3/08'])])
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[])
  by={r['application_number']:r for r in f['rows']}; self.assertEqual(by['18000001']['examiner'],'ROE, RICK'); self.assertIsNone(by['18000002']['examiner'])
 def test_frame_id_must_be_a_valid_file_name(self):
  for bad in ('con','NUL','a b','..'):
   self.plan['frame_id']=bad; f,errs=build(self.plan,self.base); self.assertTrue(errs,bad)
 def test_page_tamper_detected(self):
  d=self.harvest('q1',[rec('18000001','2024-09-05',['G06N   3/08'])]); (d/'page-000000.json').write_bytes(b'{}')
  self.assertTrue(any('hash mismatch' in e for e in build(self.plan,self.base)[1]))
 def test_truncation_noted(self):
  self.harvest('q1',[rec('18000001','2024-09-05',['G06N   3/08'])],count=50)
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[]); self.assertTrue(f['queries'][0]['truncated'])
  self.assertTrue(any('truncated' in n for n in f['notes']))
 def test_harvest_error_blocks(self):
  self.harvest('q1',[],error='offset 0: n8n returned an empty response')
  self.assertTrue(any('harvest error' in e for e in build(self.plan,self.base)[1]))
 def test_prefix_must_end_at_boundary(self):
  self.plan['cpc']['strata']['ai']['match_prefixes']=['G06N3']; self.harvest('q1',[])
  self.assertTrue(any('main-group' in e for e in build(self.plan,self.base)[1]))
 def test_official_expansion_and_historical_prefix(self):
  ex={'source':{'sha256':'f'*64},'strata':{'ai':{'symbols':['G06N3/04','G06N3/08'],'historical_prefixes':['G06N99']}}}
  raw=json.dumps(ex).encode(); (self.base/'ex.json').write_bytes(raw)
  self.plan['cpc']['strata']['ai']={'seed_symbols':['G06N3/02'],'expanded_symbols':[],'expansion':{'file':'ex.json','stratum':'ai','sha256':hashlib.sha256(raw).hexdigest()}}
  self.harvest('q1',[rec('18000001','2024-09-05',['G06N   3/08']),rec('18000002','2024-09-05',['G06N   3/12']),rec('18000003','2024-09-05',['G06N  99/00'])])
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[])
  self.assertEqual([r['eligible'] for r in f['rows']],[True,False,True]); self.assertEqual(f['cpc']['strata']['ai']['expanded_symbols'],['G06N3/04','G06N3/08'])
  (self.base/'ex.json').write_bytes(raw+b' ')
  self.assertTrue(any('hash mismatch' in e for e in build(self.plan,self.base)[1]))
 def test_prefix_only_wireless_and_iot_expansion(self):
  ex={'source':{'sha256':'e'*64},'strata':{
   'wireless':{'symbols':[],'historical_prefixes':['H04W','H04B7/','H04L5/','H04L27/']},
   'iot':{'symbols':[],'historical_prefixes':['G16Y','H04W4/70']}}}
  raw=json.dumps(ex).encode(); (self.base/'prefix.json').write_bytes(raw)
  expansion=lambda stratum: {'seed_symbols':[],'expanded_symbols':[],'expansion':{
   'file':'prefix.json','stratum':stratum,'sha256':hashlib.sha256(raw).hexdigest()}}
  self.plan['cpc']['strata']={'wireless':expansion('wireless'),'iot':expansion('iot')}
  self.plan['queries']=[{'query_id':'q1','stratum':'wireless','window_id':'w1'},
                        {'query_id':'q2','stratum':'iot','window_id':'w1'}]
  self.harvest('q1',[rec('18000001','2024-09-05',['H04W 72/04']),rec('18000002','2024-09-05',['H04B 7/185']),
                     rec('18000003','2024-09-05',['H04L 5/00']),rec('18000004','2024-09-05',['H04L 27/26']),
                     rec('18000005','2024-09-05',['H04B 8/00']),rec('18000006','2024-09-05',['H04L 28/00'])])
  self.harvest('q2',[rec('18000007','2024-09-05',['G16Y 20/10']),rec('18000008','2024-09-05',['H04W 4/70']),
                     rec('18000009','2024-09-05',['G16X 20/10']),rec('18000010','2024-09-05',['H04W 4/71'])])
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[])
  by={r['application_number']:r for r in f['rows']}
  self.assertTrue(all(by[a]['eligible'] for a in ('18000001','18000002','18000003','18000004','18000007','18000008')))
  self.assertTrue(all(not by[a]['eligible'] for a in ('18000005','18000006','18000009','18000010')))
  self.assertEqual(f['cpc']['strata']['wireless']['expanded_symbols'],ex['strata']['wireless']['historical_prefixes'])
  self.assertEqual(f['cpc']['strata']['iot']['expanded_symbols'],ex['strata']['iot']['historical_prefixes'])
  (self.base/'prefix.json').write_bytes(raw+b' ')
  self.assertEqual(sum('hash mismatch' in e for e in build(self.plan,self.base)[1]),2)
 def test_dedup_across_queries(self):
  self.plan['queries'].append({'query_id':'q2','stratum':'ai','window_id':'w1'})
  r=rec('18000001','2024-09-05',['G06N   3/08']); self.harvest('q1',[r]); self.harvest('q2',[r])
  f,errs=build(self.plan,self.base); self.assertEqual(errs,[]); self.assertEqual(f['rows'][0]['query_ids'],['q1','q2'])
  self.assertEqual((f['funnel']['candidates'],f['funnel']['deduplicated']),(2,1))
if __name__=='__main__': unittest.main()
