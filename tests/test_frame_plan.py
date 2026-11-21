import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from frame_plan import plan, wildcard

class FramePlan(unittest.TestCase):
 def test_wildcards(self):
  self.assertEqual(wildcard('G06N3/02'),'G06N???3*'); self.assertEqual(wildcard('G06N20/00'),'G06N??20*')
  self.assertEqual(wildcard('G06V10/70'),'G06V??10*'); self.assertEqual(wildcard('H10B'),'H10B*')
 def test_weekly_windows_half_open_and_inclusive_query(self):
  with tempfile.TemporaryDirectory() as t:
   b=Path(t); f=b/'corpus/cpc/2026.08/expansions.json'; f.parent.mkdir(parents=True)
   f.write_text(json.dumps({'cpc_version':'2026.08','strata':{'s':{'seeds':['H10B'],'historical_prefixes':['H01L'],'symbols':['H10B10/00']}}}))
   p=plan('f','s','2024-09-01','2024-09-16',7,base=b)
   self.assertEqual([(w['from'],w['to']) for w in p['windows']],[('2024-09-01','2024-09-08'),('2024-09-08','2024-09-15'),('2024-09-15','2024-09-16')])
   self.assertTrue(p['queries'][0]['q'].endswith('filingDate:[2024-09-01 TO 2024-09-07]'))
   self.assertTrue(p['queries'][2]['q'].endswith('[2024-09-15 TO 2024-09-15]'))
   self.assertIn('cpcClassificationBag:H01L*',p['queries'][0]['q'])
   self.assertEqual(len(p['cpc']['strata']['s']['expansion']['sha256']),64)
if __name__=='__main__': unittest.main()
