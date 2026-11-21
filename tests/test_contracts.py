import copy, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import patent_genius as pg
BASE=Path(__file__).resolve().parents[1]/'examples'
class Contracts(unittest.TestCase):
 def setUp(self):
  self.m=pg.load(BASE/'manifest.json');self.p=pg.load(BASE/'coverage.json');self.p['requirements']=self.p['requirements'][:1];self.d=(BASE/'draft.md').read_text(encoding='utf-8');self.uid=self.m['documents'][0]['units'][0]['id']
  self.p.update(claim_scope='claims_present',claims=[{'id':'1','status':'proposed','depends_on':[],'limitations':[{'id':'1.a','support_id':'s1'}],'combination_support_id':'combo'}],support_records=[self.record('s1','limitation'),self.record('combo','combination')])
 def record(self,ident,kind): return {'id':ident,'kind':kind,'claim_id':'1','limitation_ids':['1.a'],'basis':'disclosure_only','status':'mapped_for_review','evidence':[self.uid],'draft_span':[0,20],'written_description_review':'not_reviewed','enablement_review':'not_reviewed'}
 def result(self): return pg.audit(self.m,BASE,self.p,self.d)
 def test_well_formed_is_mechanical_only(self):
  r=self.result();self.assertEqual(r['errors'],[]);self.assertFalse(r['semantic_support_verified']);self.assertFalse(r['legal_sufficiency_verified'])
 def test_combination_missing(self): self.p['support_records'].pop();self.assertTrue(self.result()['errors'])
 def test_limitation_missing(self): self.p['claims'][0]['limitations'][0]['support_id']='absent';self.assertTrue(self.result()['errors'])
 def test_later_cannot_supply_as_filed(self): self.m['documents'][0]['temporal_role']='later_material';self.p['support_records'][0]['basis']='as_filed';self.assertTrue(self.result()['errors'])
 def test_unfiled_cannot_supply_as_filed(self): self.p['support_records'][0]['basis']='as_filed';self.assertTrue(self.result()['errors'])
 def test_as_filed_pin_required(self): self.m['documents'][0]['temporal_role']='as_filed';self.assertTrue(self.result()['errors'])
 def test_as_filed_pin_permits_record_check_only(self):
  self.m['documents'][0].update(temporal_role='as_filed',original_sha256='a'*64,filing_date='2026-01-01');self.p['support_records'][0]['basis']='as_filed';self.assertEqual(self.result()['errors'],[])
 def test_cross_matter_support(self): self.p['matter']='different';self.assertTrue(self.result()['errors'])
 def test_typed_lane_required(self): del self.m['documents'][0]['lane'];self.assertTrue(self.result()['errors'])
 def test_lane_id_mismatch(self): self.m['documents'][0]['lane']='public_exemplar';self.assertTrue(self.result()['errors'])
 def test_unknown_support(self): self.p['support_records'][0]['status']='unknown';self.assertTrue(self.result()['errors'])
 def test_missing_review_status(self): del self.p['support_records'][0]['enablement_review'];self.assertTrue(self.result()['errors'])
 def test_issue_omitted(self): self.p['issues']=[{'id':'103','status':'omitted','reason':'pending practitioner decision'}];self.assertTrue(self.result()['errors'])
 def test_claim_dependency_cycle(self): self.p['claims'][0]['depends_on']=['1'];self.assertTrue(self.result()['errors'])
 def test_claim_dependency_missing(self): self.p['claims'][0]['depends_on']=['2'];self.assertTrue(self.result()['errors'])
 def test_claim_status(self): self.p['claims'][0]['status']='allowed-looking';self.assertTrue(self.result()['errors'])
 def test_exact_locator_required(self): del self.m['documents'][0]['units'][0]['source_locator'];self.assertTrue(self.result()['errors'])
 def test_boolean_offsets_rejected(self): self.m['documents'][0]['units'][0]['start']=False;self.assertTrue(self.result()['errors'])
 def test_public_cannot_supply_support_with_valid_ids(self):
  d=self.m['documents'][0];old=self.uid;d.update(id='public:example',lane='public_exemplar',temporal_role='not_client_disclosure',application_id='00000000');u=d['units'][0];u['id']=d['id']+':'+pg.digest((d['sha256']+':0:'+str(u['end'])).encode())[:24]
  for r in self.p['support_records']: r['evidence']=[u['id']]
  self.assertTrue(any('public/authority/cross-matter' in e for e in self.result()['errors']))
 def test_warnings_block(self): self.m['documents'][0]['normalization']['warnings']=['page omitted'];self.assertTrue(self.result()['errors'])
 def test_wrong_manifest_shape(self): self.m['documents']={};self.assertTrue(self.result()['errors'])
 def test_wrong_plan_shape(self): self.p['claims']={};self.assertTrue(self.result()['errors'])
if __name__=='__main__': unittest.main()
