import copy, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import patent_genius as pg
BASE=Path(__file__).resolve().parents[1]/"examples"
class Checks(unittest.TestCase):
 def setUp(self): self.m=pg.load(BASE/"manifest.json"); self.p=pg.load(BASE/"coverage.json"); self.d=(BASE/"draft.md").read_text(encoding="utf-8")
 def test_valid_inventory(self): self.assertEqual(pg.validate(self.m,BASE)[0],[])
 def test_unknown_blocks(self): self.assertEqual(pg.audit(self.m,BASE,self.p,self.d)["status"],"REVIEW_REQUIRED")
 def test_missing_source_blocks(self): self.m["documents"][0]["text_path"]="missing"; self.assertTrue(pg.audit(self.m,BASE,self.p,self.d)["errors"])
 def test_hash_tamper(self): self.m["documents"][0]["sha256"]="0"*64; self.assertTrue(pg.validate(self.m,BASE)[0])
 def test_ocr_blocks(self): self.m["documents"][0]["status"]="needs_ocr"; self.assertTrue(pg.validate(self.m,BASE)[0])
 def test_namespace(self): self.m["documents"][0]["id"]="public:disclosure"; self.assertTrue(pg.audit(self.m,BASE,self.p,self.d)["errors"])
 def test_quote(self): self.p["quotations"][0]["text"]="invented quotation"; self.assertIn("quotation mismatch",pg.audit(self.m,BASE,self.p,self.d)["errors"])
 def test_traversal(self): self.m["documents"][0]["text_path"]="../outside"; self.assertTrue(pg.validate(self.m,BASE)[0])
 def test_mechanical_only(self): self.p["requirements"]=self.p["requirements"][:1]; r=pg.audit(self.m,BASE,self.p,self.d); self.assertEqual(r["status"],"MECHANICAL_CHECKS_ONLY"); self.assertFalse(r["legal_sufficiency_verified"])
 def test_cross_matter(self):
  self.p["matter"]="other"; self.assertTrue(pg.audit(self.m,BASE,self.p,self.d)["errors"])
 def test_broken_pair(self):
  self.m["events"]=[{"id":"response","document_id":"absent","responds_to":"missing"}]; self.assertTrue(pg.validate(self.m,BASE)[0])
 def test_sections(self): self.assertTrue(pg.audit(self.m,BASE,self.p,"")["errors"])
if __name__=="__main__": unittest.main()
