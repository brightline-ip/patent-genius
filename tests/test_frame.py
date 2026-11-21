import copy, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frame_contracts import frame_errors
ROOT=Path(__file__).resolve().parents[1]
class Frame(unittest.TestCase):
 def setUp(self): self.f=json.loads((ROOT/'examples/frame.json').read_text(encoding='utf-8'))
 def test_example_valid(self): self.assertEqual(frame_errors(self.f),[])
 def test_overlapping_windows(self): self.f['windows'].append({'id':'w3','from':'2020-01-01','to':'2022-01-01','basis':'filing_date'}); self.assertTrue(any('overlap' in e for e in frame_errors(self.f)))
 def test_row_outside_window(self): self.f['rows'][0]['filing_date']='2016-01-01'; self.assertTrue(any('outside window' in e for e in frame_errors(self.f)))
 def test_boundary_belongs_to_recent_window_only(self):
  self.f['rows'][0]['filing_date']='2021-10-16'; self.assertEqual(frame_errors(self.f),[])
  self.f['rows'][0]['query_ids']=['q-ai-w2']; self.assertTrue(any('outside window' in e for e in frame_errors(self.f)))
 def test_duplicate_application(self): self.f['rows'].append(copy.deepcopy(self.f['rows'][0])); self.assertTrue(any('duplicate application' in e for e in frame_errors(self.f)))
 def test_funnel_must_match_rows(self): self.f['funnel']['eligible']+=1; self.assertTrue(any('funnel' in e for e in frame_errors(self.f)))
 def test_truncated_query_must_say_so(self): self.f['queries'][0]['provider_count']=5000; self.assertTrue(any('truncated' in e for e in frame_errors(self.f)))
 def test_seed_cannot_enter_random_selection(self):
  self.f['rows'][0].update(source='seed_panel',seed_reason='well-known case'); self.assertTrue(any('seed_panel row cannot enter' in e for e in frame_errors(self.f)))
 def test_random_selection_needs_seed(self): del self.f['selection']['seed']; self.assertTrue(any('rng and seed' in e for e in frame_errors(self.f)))
 def test_selected_must_be_eligible(self): self.f['rows'][0].update(eligible=False,exclusion_reason='test'); self.assertTrue(any('not eligible' in e for e in frame_errors(self.f)))
 def test_excluded_needs_reason(self): self.f['rows'][1].update(eligible=False); self.assertTrue(any('exclusion_reason' in e for e in frame_errors(self.f)))
 def test_unknown_coverage_needs_descriptive_note(self): self.f['notes']=[]; self.assertTrue(any('descriptive' in e for e in frame_errors(self.f)))
 def test_unknown_stratum(self): self.f['rows'][0]['strata']=['quantum']; self.assertTrue(any('unknown stratum' in e for e in frame_errors(self.f)))
 def test_gzip_roundtrip_is_deterministic(self):
  import tempfile, gzip
  from frame_contracts import dump_frame, load_frame
  with tempfile.TemporaryDirectory() as t:
   a=Path(t)/'a.json.gz'; b=Path(t)/'b.json.gz'; dump_frame(self.f,a); dump_frame(self.f,b)
   self.assertEqual(load_frame(a),self.f); self.assertEqual(a.read_bytes(),b.read_bytes())
if __name__=='__main__': unittest.main()
