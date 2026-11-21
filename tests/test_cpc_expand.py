import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cpc_expand import parse, expand

LINES='''G\t\tPHYSICS
G06N\t\tCOMPUTING ARRANGEMENTS
G06N3/00\t0\tbiological models
G06N3/02\t1\tNeural networks
G06N3/04\t2\tArchitecture
G06N3/045\t3\tCombinations of networks
G06N3/08\t2\tLearning methods
G06N3/12\t1\tgenetic models
G06N5/00\t0\tknowledge-based
G06N5/02\t1\tKnowledge representation
G06N30/00\t0\tsynthetic'''.splitlines()

class CpcExpand(unittest.TestCase):
 def setUp(self): self.s=parse(LINES)
 def test_subgroup_stops_at_sibling(self): self.assertEqual(expand(self.s,'G06N3/02'),['G06N3/02','G06N3/04','G06N3/045','G06N3/08'])
 def test_main_group_stops_at_next_main_group(self): self.assertEqual(expand(self.s,'G06N3/00')[-1],'G06N3/12')
 def test_leaf(self): self.assertEqual(expand(self.s,'G06N3/045'),['G06N3/045'])
 def test_subclass(self): self.assertEqual(len(expand(self.s,'G06N')),9)
 def test_no_string_prefix_leak(self): self.assertNotIn('G06N30/00',expand(self.s,'G06N3/00'))
 def test_unknown_symbol(self):
  with self.assertRaises(KeyError): expand(self.s,'G06N3/99')
  with self.assertRaises(KeyError): expand(self.s,'H10W')
if __name__=='__main__': unittest.main()
