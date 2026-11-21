import gzip, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from hubenv import clean_environ
ROOT=Path(__file__).resolve().parents[1]; MOD=ROOT/'request_budget.mjs'; FRAMES=r'^y(201[2-3])-agri$'

def clean_env(): return {k:v for k,v in os.environ.items() if not k.startswith('PG_BUDGET_')}

class RequestBudget(unittest.TestCase):
 def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name); self.ledger=self.base/'budget.jsonl'; self.lock=self.base/'harvest.lock'
 def tearDown(self): self.tmp.cleanup()
 def init(self,cap=2,baseline=1):
  r=subprocess.run(['node',str(MOD),'init',str(self.ledger),str(cap),str(baseline),'approved test'],capture_output=True,text=True); self.assertEqual(r.returncode,0,r.stderr)
 def invoke(self,script,frame='y2012-agri',stamp='held'):
  self.lock.write_text('held'); env={**os.environ,'PG_BUDGET_FRAMES':FRAMES,'PG_BUDGET_LEDGER':str(self.ledger),'PG_BUDGET_LOCK_STAMP':stamp,'PG_BUDGET_LOCK_PATH':str(self.lock)}
  return subprocess.run(['node','--input-type=module','-e',script,MOD.as_uri(),frame],capture_output=True,text=True,env=env)
 def test_boundary_crash_reservation_and_schema(self):
  self.init(); code="const {requestBudget,isBudgetStop}=await import(process.argv[1]);let b=requestBudget(process.argv[2]);b.reserve('test',{n:1});try{b.reserve('test',{n:2})}catch(e){console.log(isBudgetStop(e),e.code)}"
  r=self.invoke(code); self.assertEqual(r.returncode,0,r.stderr); self.assertIn('true BUDGET',r.stdout)
  rows=[json.loads(x) for x in self.ledger.read_text().splitlines()]; self.assertEqual([x.get('seq') for x in rows],[None,1]); self.assertEqual(rows[0]['cap'],2)
  again=self.invoke("const {requestBudget}=await import(process.argv[1]);requestBudget(process.argv[2]).reserve('after-crash')")
  self.assertNotEqual(again.returncode,0); self.assertEqual(len(self.ledger.read_text().splitlines()),2)
 def test_missing_wrong_stamp_and_corruption_fail_closed(self):
  self.init(cap=4); script="const {requestBudget}=await import(process.argv[1]);requestBudget(process.argv[2]).reserve('x')"
  self.assertNotEqual(self.invoke(script,stamp='wrong').returncode,0); self.assertNotEqual(self.invoke(script,stamp='').returncode,0)
  self.ledger.write_text(self.ledger.read_text()+'{bad'); self.assertNotEqual(self.invoke("const {requestBudget}=await import(process.argv[1]);requestBudget(process.argv[2]).reserve('x')").returncode,0)
 def test_inactive_ids_need_no_ledger_or_lock(self):
  r=subprocess.run(['node','--input-type=module','-e',"const {requestBudget}=await import(process.argv[1]);requestBudget(process.argv[2]).reserve('x');console.log('ok')",MOD.as_uri(),'y2099-agri'],capture_output=True,text=True,env={**clean_env(),'PG_BUDGET_FRAMES':FRAMES})
  self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(r.stdout.strip(),'ok')
 def test_active_missing_ledger_is_typed_at_reservation(self):
  r=subprocess.run(['node','--input-type=module','-e',"const {requestBudget,isBudgetStop}=await import(process.argv[1]);let b=requestBudget('y2013-agri');try{b.reserve('x')}catch(e){console.log(isBudgetStop(e));process.exit(75)}",MOD.as_uri()],capture_output=True,text=True,env={**clean_env(),'PG_BUDGET_FRAMES':FRAMES})
  self.assertEqual(r.returncode,75,r.stderr); self.assertEqual(r.stdout.strip(),'true')
 def test_off_by_default_even_for_frame_names_a_pattern_would_match(self):
  r=subprocess.run(['node','--input-type=module','-e',"const {requestBudget}=await import(process.argv[1]);const b=requestBudget(process.argv[2]);b.reserve('x');console.log(b.active)",MOD.as_uri(),'y2013-agri'],capture_output=True,text=True,env=clean_env())
  self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(r.stdout.strip(),'false')
 def test_init_rejects_negative_cap_or_baseline(self):
  for cap,baseline in [(-1,0),(2,-1)]:
   r=subprocess.run(['node',str(MOD),'init',str(self.ledger),str(cap),str(baseline),'invalid'],capture_output=True,text=True)
   self.assertNotEqual(r.returncode,0); self.assertFalse(self.ledger.exists())
 def test_cached_reader_accepts_valid_child_growth(self):
  self.init(cap=5,baseline=0); code="const {requestBudget}=await import(process.argv[1]);const {spawnSync}=await import('node:child_process');let b=requestBudget(process.argv[2]);b.reserve('parent');let child=`const {requestBudget}=await import(process.argv[1]);requestBudget(process.argv[2]).reserve('child')`;let r=spawnSync(process.execPath,['--input-type=module','-e',child,process.argv[1],process.argv[2]],{env:process.env});if(r.status)throw Error(r.stderr);b.reserve('parent2');"
  r=self.invoke(code); self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(len(self.ledger.read_text().splitlines()),4)
 def test_zero_difference_second_call_is_refused_at_boundary(self):
  self.init(cap=4,baseline=0); self.lock.write_text('held'); counter=self.base/'calls.txt'
  stub=self.base/'stub_hub.py'   # stands in for the Hub gateway command: a count request works, anything else fails with an empty response
  stub.write_text("import json, os, sys\nx = json.loads(sys.stdin.read())\nopen(os.environ['COUNTER'], 'a').write('x')\nif x['body']['pagination']['limit'] == 1:\n  print(json.dumps({'status': 200, 'text': json.dumps({'count': 7, 'patentFileWrapperDataBag': []}), 'contentType': 'application/json'})); sys.exit(0)\nprint(json.dumps({'error': {'code': 'error', 'message': 'n8n returned an empty response'}})); sys.exit(1)\n")
  plan={'frame_id':'y2012-agri','windows':[{'id':'w1','from':'2024-01-01','to':'2024-01-08'},{'id':'w2','from':'2024-01-08','to':'2024-01-15'}],
        'queries':[{'query_id':'q1','stratum':'agri','window_id':'w1','q':'applicationMetaData.filingDate:[2024-01-01 TO 2024-01-07]'}, {'query_id':'q2','stratum':'agri','window_id':'w2','q':'applicationMetaData.filingDate:[2024-01-08 TO 2024-01-14]'}]}
  pp=self.base/'plan.json'; pp.write_text(json.dumps(plan)); q1=self.base/'public-originals'/'frames'/'y2012-agri'/'q1'; q1.mkdir(parents=True)
  (q1/'receipt.json').write_text(json.dumps({'stop_reason':'exhausted'}))
  env={**clean_environ(),'PG_TOOL_DATA_DIR':str(self.base),'PG_HUB_TEST_STUB':json.dumps([sys.executable,str(stub)]),'PG_ALLOW_TEST_STUB':'1','FRAME_RETRY_WAITS_MS':'0,0,0','COUNTER':str(counter),'PG_BUDGET_FRAMES':FRAMES,'PG_BUDGET_LEDGER':str(self.ledger),'PG_BUDGET_LOCK_STAMP':'held','PG_BUDGET_LOCK_PATH':str(self.lock)}
  r=subprocess.run(['node',str(ROOT/'frame_run.mjs'),str(pp),'100','20'],cwd=self.base,env=env,capture_output=True,text=True)
  self.assertEqual(r.returncode,75,r.stderr); self.assertEqual(counter.read_text(),'xxxx')

if __name__=='__main__': unittest.main()
