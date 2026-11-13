import hashlib,importlib.machinery,importlib.util,json,os,sqlite3,subprocess,sys,time,unittest
from pathlib import Path
from test_lane import LaneTests

class IdempotentLaneTests(LaneTests):
 def test_retry_returns_original_run_and_conflict_is_rejected(self):
  self.start_lane()
  first=json.loads(self.cli('lane','assign','L1','--prompt','LANE_TEST_WAIT durable assignment','--idempotency-key','dispatch/card-A/build').stdout)
  second=json.loads(self.cli('lane','assign','L1','--prompt','LANE_TEST_WAIT durable assignment','--idempotency-key','dispatch/card-A/build').stdout)
  self.assertEqual(first['id'],second['id']);self.assertTrue(second['reused'])
  bad=self.cli('lane','assign','L1','--prompt','different task','--idempotency-key','dispatch/card-A/build',check=False)
  self.assertNotEqual(bad.returncode,0);self.assertIn('conflicts',bad.stdout)
  db=sqlite3.connect(self.data/'lane-assignment-keys.sqlite3');self.assertEqual(db.execute('select count(*) from assignments').fetchone()[0],1)
 def test_durable_reserved_key_does_not_execute_again_after_crash(self):
  self.start_lane();task='LANE_TEST_WAIT reserved before crash';key='dispatch/card-B/build'
  request={'task':task,'title':task,'verify':None,'verify_baseline':False,'budget_minutes':5.0,'base':None,'card_type':None,'rework_of':None,'paths':[],'read_only':False,'scope':'repo','lane':'L1','thinking':None,'evalid_role':None,'evalid_stage':None,'evalid_assignment_index':None,'artifact_sink':None,'evalid_output':None,'evalid_repair_round':None}
  fp=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
  code = """import importlib.machinery,importlib.util,os,signal,sys
loader=importlib.machinery.SourceFileLoader('swrm_fixture',sys.argv[1]);spec=importlib.util.spec_from_loader(loader.name,loader);module=importlib.util.module_from_spec(spec);loader.exec_module(module)
module.assignment_key_reserve(sys.argv[2],sys.argv[3],'L1','crash-reserved-1')
os.kill(os.getpid(),signal.SIGKILL)
"""
  killed=subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'bin'/'swrm'),key,fp],env=self.env)
  self.assertEqual(killed.returncode,-9)
  result=self.cli('lane','assign','L1','--prompt',task,'--budget-minutes','5','--idempotency-key',key,check=False)
  self.assertNotEqual(result.returncode,0);r=json.loads(result.stdout)
  self.assertEqual(r['id'],'crash-reserved-1');self.assertEqual(r['state'],'uncertain');self.assertFalse(r['assigned']);self.assertFalse((self.data/'runs'/'crash-reserved-1').exists())
 def test_reserved_key_with_queued_status_is_still_uncertain(self):
  self.start_lane();task='LANE_TEST_WAIT queued window';key='dispatch/queued/key'
  request={'task':task,'title':task,'verify':None,'verify_baseline':False,'budget_minutes':5.0,'base':None,'card_type':None,'rework_of':None,'paths':[],'read_only':False,'scope':'repo','lane':'L1','thinking':None,'evalid_role':None,'evalid_stage':None,'evalid_assignment_index':None,'artifact_sink':None,'evalid_output':None,'evalid_repair_round':None}
  fp=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
  db=sqlite3.connect(self.data/'lane-assignment-keys.sqlite3');db.execute('CREATE TABLE assignments (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, lane TEXT NOT NULL, run_id TEXT NOT NULL, created REAL NOT NULL, phase TEXT NOT NULL)');db.execute('INSERT INTO assignments VALUES (?,?,?,?,?,?)',(key,fp,'L1','reserved-run',time.time(),'reserved'));db.commit();db.close()
  d=self.data/'runs'/'reserved-run';d.mkdir(parents=True);(d/'status.json').write_text(json.dumps({'id':'reserved-run','state':'queued'}))
  result=self.cli('lane','assign','L1','--prompt',task,'--budget-minutes','5','--idempotency-key',key,check=False)
  r=json.loads(result.stdout);self.assertNotEqual(result.returncode,0);self.assertEqual(r['state'],'uncertain');self.assertFalse(r['assigned'])
 def test_key_survives_supervisor_restart(self):
  self.start_lane();key='dispatch/restart/build';task='LANE_TEST_WAIT restart invariant'
  first=json.loads(self.cli('lane','assign','L1','--prompt',task,'--idempotency-key',key).stdout)
  self.cli('lane','down','L1',check=False,timeout=45);self.start_lane()
  second=json.loads(self.cli('lane','assign','L1','--prompt',task,'--idempotency-key',key).stdout)
  self.assertEqual(first['id'],second['id']);self.assertTrue(second['reused'])
 def test_parallel_same_key_creates_one_run(self):
  self.start_lane();key='dispatch/concurrent/build';task='LANE_TEST_WAIT parallel invariant'
  cli=Path(__file__).resolve().parents[1]/'bin'/'swrm'
  procs=[subprocess.Popen([sys.executable,str(cli),'lane','assign','L1','--prompt',task,'--idempotency-key',key],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(6)]
  results=[]
  for p in procs:
   out,err=p.communicate(timeout=30);self.assertEqual(p.returncode,0,err+' '+out);results.append(json.loads(out))
  self.assertEqual(len({x['id'] for x in results}),1)
  db=sqlite3.connect(self.data/'lane-assignment-keys.sqlite3');self.assertEqual(db.execute('select count(*) from assignments').fetchone()[0],1)
 def test_required_key_enforced_at_supervisor_socket(self):
  self.env['OPENSWARM_LANE_REQUIRE_IDEMPOTENCY']='1';self.start_lane()
  cap=json.loads(self.cli('lane','capabilities','L1').stdout)['capabilities'];self.assertEqual(cap['durable_assignment_keys'],1);self.assertTrue(cap['requires_idempotency_key'])
  denied=self.cli('lane','assign','L1','--prompt','unkeyed',check=False)
  self.assertNotEqual(denied.returncode,0);self.assertIn('idempotency key required',denied.stdout)
  path=self.data/'lanes'/'L1'/'ctl.sock'
  if sys.platform=='darwin' and len(os.fsencode(path))>103:
   digest=hashlib.sha256(os.fsencode(os.path.realpath(path))).hexdigest()[:32];path=Path('/tmp')/f'swrm-{os.getuid()}-{digest}.sock'
  import socket
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
   sock.connect(str(path));sock.sendall((json.dumps({'op':'assign','run_id':'bypass-1','task':'unkeyed raw request'})+'\n').encode());answer=json.loads(sock.recv(10000))
  self.assertFalse(answer['ok']);self.assertIn('required',answer['error'])
  ok=json.loads(self.cli('lane','assign','L1','--prompt','keyed task','--idempotency-key','dispatch/test/keyed').stdout)
  self.assertTrue(ok['ok'])
 def test_same_host_different_lanes_do_not_duplicate(self):
  cfg=json.loads((self.data/'lanes.json').read_text());lane=dict(cfg['lanes'][0]);lane['name']='L2';cfg['lanes'].append(lane);(self.data/'lanes.json').write_text(json.dumps(cfg))
  self.cli('lane','up','L1','L2')
  self.wait_for(lambda:len([r for r in json.loads(self.cli('lane','status').stdout) if r.get('state')=='idle'])==2,timeout=20,description='both lanes idle')
  cli=Path(__file__).resolve().parents[1]/'bin'/'swrm';task='LANE_TEST_WAIT cross lane';key='dispatch/crosslane/build'
  ps=[subprocess.Popen([sys.executable,str(cli),'lane','assign',name,'--prompt',task,'--idempotency-key',key],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for name in ('L1','L2')]
  results=[]
  for p in ps:
   out,err=p.communicate(timeout=30);results.append((p.returncode,json.loads(out)))
  self.assertEqual(sorted(rc for rc,_ in results),[0,1]);self.assertEqual(sum('conflicts' in obj.get('error','') for _,obj in results),1)
  db=sqlite3.connect(self.data/'lane-assignment-keys.sqlite3');self.assertEqual(db.execute('select count(*) from assignments').fetchone()[0],1)
 def test_fleet_exec_preserves_idempotency_end_to_end(self):
  self.env['OPENSWARM_LANE_REQUIRE_IDEMPOTENCY']='1';self.start_lane()
  cli=Path(__file__).resolve().parents[1]/'bin'/'swrm';(self.data/'fleet.json').write_text(json.dumps({'hosts':[{'name':'local-test','kind':'local','swrm':str(cli)}]}))
  args=('fleet','exec','local-test','--','lane','assign','L1','--prompt','LANE_TEST_WAIT fleet integration','--idempotency-key','dispatch/fleet/build')
  one=json.loads(self.cli(*args,timeout=30).stdout);two=json.loads(self.cli(*args,timeout=30).stdout)
  self.assertEqual(one['id'],two['id']);self.assertTrue(two['reused'])
if __name__=='__main__':unittest.main()
