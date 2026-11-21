import json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import patent_genius as pg, contracts
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'examples'
class ProsecutionContracts(unittest.TestCase):
 def setUp(self):
  self.m=pg.load(BASE/'manifest.json')
  self.pub={'id':'public:a1','lane':'public_exemplar','temporal_role':'not_client_disclosure','application_id':'16000001','source_locator':'https://example.invalid/a1.pdf','status':'needs_ocr','document_type':'CTNF','technologies':[],'cpcs':[],'units':[],'normalization':{'provider':'none','warnings':[],'quality':['ocr pending']}}
  self.rep=dict(self.pub,id='public:r1',document_type='REM',normalization={'provider':'none','warnings':[],'quality':[]})
  self.m['documents']+= [self.pub,self.rep]
  self.action={'id':'e-act','application_id':'16000001','document_id':'public:a1','event_type':'CTNF','event_kind':'action','date':'2022-05-09','mailing_date':'2022-05-09'}
  self.reply={'id':'e-rep','application_id':'16000001','document_id':'public:r1','event_type':'REM','event_kind':'reply_component','date':'2022-09-09','receipt_date':'2022-09-09','responds_to':'e-act','pairing_evidence':[{'type':'reply_identifies_action','document_id':'public:r1','page':1}]}
  self.m['events']=[self.action,self.reply]
 def errs(self): return contracts.manifest_errors(self.m)
 def ready(self,**kw):
  self.pub.update(status='ready',sha256='a'*64,text_path='x',units=[{'id':'u','source_locator':'p1','start':0,'end':1}]); self.pub.update(kw)
 def test_v2_rejected(self): self.m['schema_version']=2; self.assertTrue(self.errs())
 def test_valid_pairing_passes(self): self.assertEqual(self.errs(),[])
 def test_quality_notes_do_not_block(self): self.ready(); self.assertFalse(any('warnings' in e for e in self.errs()))
 def test_blocking_warning_blocks(self): self.pub['normalization']['warnings']=['page omitted']; self.assertTrue(any('blocking' in e for e in self.errs()))
 def test_pairing_requires_evidence(self): del self.reply['pairing_evidence']; self.assertTrue(any('pairing_evidence' in e for e in self.errs()))
 def test_pairing_cross_application_rejected(self): self.reply['application_id']='16000002'; self.assertTrue(any('crosses applications' in e for e in self.errs()))
 def test_pairing_by_date_alone_needs_review(self):
  self.reply['pairing_evidence']=[{'type':'date_consistency','document_id':'public:r1'}]; self.assertTrue(any('reviewed' in e for e in self.errs()))
  self.reply['review_status']='reviewed'; self.assertEqual(self.errs(),[])
 def test_reply_before_action_rejected(self): self.reply['receipt_date']='2022-01-01'; self.assertTrue(any('before the action' in e for e in self.errs()))
 def test_unknown_responds_to(self): self.reply['responds_to']='nope'; self.assertTrue(any('unknown event' in e for e in self.errs()))
 def test_public_requires_application_id(self): del self.pub['application_id']; self.assertTrue(any('application_id' in e for e in self.errs()))
 def test_round_incomplete_cannot_be_complete(self):
  self.m['rounds']=[{'id':'r1','action_event':'e-act','reply_events':['e-rep'],'checklist':{'expected':['REM','CLM'],'present':['REM'],'missing':['CLM']},'review_status':'reviewed','complete':True}]
  self.assertTrue(any('cannot be complete' in e for e in self.errs()))
 def test_round_complete_needs_review(self):
  self.m['rounds']=[{'id':'r1','action_event':'e-act','reply_events':['e-rep'],'checklist':{'expected':['REM'],'present':['REM'],'missing':[]},'review_status':'unreviewed','complete':True}]
  self.assertTrue(any('cannot be complete' in e for e in self.errs()))
  self.m['rounds'][0]['review_status']='reviewed'; self.assertEqual(self.errs(),[])
 def test_round_reply_must_answer_action(self):
  self.m['rounds']=[{'id':'r1','action_event':'e-act','reply_events':['e-act'],'checklist':{'expected':[],'present':[],'missing':[]},'review_status':'reviewed','complete':False}]
  self.assertTrue(any('does not respond' in e for e in self.errs()))
 def test_page_status_blocks_ready(self):
  self.ready(pages=[{'page':1,'status':'ocr_low_confidence'}]); self.assertTrue(any('not usable' in e for e in self.errs()))
 def test_unit_bound_to_current_run(self):
  self.ready(extraction_runs=[{'run_id':'old','extractor':'t','version':'1','text_sha256':'b'*64},{'run_id':'new','extractor':'t','version':'2','text_sha256':'a'*64,'supersedes_run_id':'old'}])
  self.pub['units'][0]['run_id']='old'; self.assertTrue(any('current extraction run' in e for e in self.errs()))
  self.pub['units'][0]['run_id']='new'; self.assertFalse(any('extraction run' in e for e in self.errs()))
 def test_ready_text_must_match_a_run(self):
  self.ready(extraction_runs=[{'run_id':'old','extractor':'t','version':'1','text_sha256':'b'*64}]); self.pub['units'][0]['run_id']='old'
  self.assertTrue(any('matches no extraction run' in e for e in self.errs()))
 def test_claim_version_rules(self):
  self.m['claim_versions']=[{'version_id':'v1','application_id':'16000001','document_id':'public:r1','pending_at_event_ids':['e-act'],'claims':[{'number':1,'status':'pending','depends_on':[]},{'number':2,'status':'amended','depends_on':[1]}]}]
  self.assertEqual(self.errs(),[])
  self.m['claim_versions'][0]['claims'][1]['depends_on']=[3]; self.assertTrue(any('depends on missing' in e for e in self.errs()))
  self.m['claim_versions'][0]['claims'][1].update(depends_on=[1],status='allowed-looking'); self.assertTrue(any('claim version status' in e for e in self.errs()))
 def test_claim_version_supersedes_unknown(self):
  self.m['claim_versions']=[{'version_id':'v2','application_id':'16000001','document_id':'public:r1','claims':[{'number':1,'status':'granted','depends_on':[]}],'supersedes_version_id':'v1'}]
  self.assertTrue(any('supersedes_version_id unknown' in e for e in self.errs()))
if __name__=='__main__': unittest.main()
