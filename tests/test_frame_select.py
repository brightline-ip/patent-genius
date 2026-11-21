import copy, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frame_select import select
from frame_contracts import frame_errors
ROOT=Path(__file__).resolve().parents[1]

class FrameSelect(unittest.TestCase):
 def setUp(self):
  f=json.loads((ROOT/'examples/frame.json').read_text(encoding='utf-8'))
  f['selection']={'method':'none','selected':[]}; f['funnel']['selected']=0
  f['rows'].append({'application_number':'17000003','filing_date':'2022-02-01','cpc':['G06N3/04'],'strata':['ai'],'query_ids':['q-ai-w1'],'eligible':True,'family_key':'fam-1','family_basis':'synthetic','source':'frame'})
  f['funnel'].update(candidates=3,deduplicated=3,eligible=3)
  self.f=f; self.assertEqual(frame_errors(f),[])
 def test_deterministic_and_valid(self):
  a=select(self.f,'sel',7,{'ai':1}); b=select(self.f,'sel',7,{'ai':1})
  self.assertEqual(a['selection']['selected'],b['selection']['selected']); self.assertEqual(frame_errors(a),[])
  self.assertEqual(a['selection']['selected'][0]['selection_probability'],0.5); self.assertEqual(a['derived_from']['frame_id'],'example-synthetic-frame')
 def test_family_members_selected_together(self):
  sel=select(self.f,'sel',1,{'ai':2})['selection']['selected']
  self.assertEqual({c['family_key']:c['application_numbers'] for c in sel}['fam-1'],['17000001','17000003'])
 def test_target_above_population_caps(self):
  s=select(self.f,'sel',1,{'ai':99}); self.assertEqual(len(s['selection']['selected']),2); self.assertEqual(s['selection']['selected'][0]['selection_probability'],1.0)
 def test_source_not_mutated(self):
  before=copy.deepcopy(self.f); select(self.f,'sel',1,{'ai':1}); self.assertEqual(self.f,before)
 def test_seed_and_ineligible_rows_excluded(self):
  self.f['rows'][1].update(source='seed_panel',seed_reason='known'); self.f['rows'][0].update(eligible=False,exclusion_reason='x')
  self.f['funnel'].update(eligible=2,excluded=1,families=1); self.f['rows'][2]['family_key']='fam-3'
  self.f['funnel']['families']=2
  sel=select(self.f,'sel',1,{'ai':5})['selection']['selected']; self.assertEqual([c['family_key'] for c in sel],['fam-3'])
 def test_refuses_reselection_and_unknown_stratum(self):
  s=select(self.f,'sel',1,{'ai':1})
  with self.assertRaises(ValueError): select(s,'again',1,{'ai':1})
  with self.assertRaises(ValueError): select(self.f,'x',1,{'quantum':1})
if __name__=='__main__': unittest.main()
