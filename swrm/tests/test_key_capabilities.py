import argparse,contextlib,importlib.machinery,importlib.util,io,unittest
from pathlib import Path
from unittest import mock
p=Path(__file__).resolve().parents[1]/'bin'/'swrm';loader=importlib.machinery.SourceFileLoader('fixture_swrm',str(p));spec=importlib.util.spec_from_loader(loader.name,loader);xs=importlib.util.module_from_spec(spec);loader.exec_module(xs)
class Tests(unittest.TestCase):
 def setUp(self):
  self.args=argparse.Namespace(name='L1',prompt='task',task_file=None,title=None,budget_minutes=5,base=None,verify=None,verify_baseline=False,card_type=None,rework_of=None,paths=[],read_only=False,scope='repo',idempotency_key='key')
 def run_with(self,call):
  with mock.patch.object(xs,'validate_card_type'),mock.patch.object(xs,'load_policy',return_value={'run':{'timeout_seconds':300}}),mock.patch.object(xs,'lanes_by_name',return_value=[{'name':'L1','backend':'codex'}]),mock.patch.object(xs,'lane_call',side_effect=call),mock.patch.object(xs,'check_budget',side_effect=AssertionError('budget must not run')),contextlib.redirect_stdout(io.StringIO()):xs.cmd_lane_assign(self.args)
 def test_old_live_supervisor_is_never_sent_assignment(self):
  ops=[]
  def call(name,req):ops.append(req['op']);return {'ok':True,'state':{}}
  with self.assertRaises(SystemExit):self.run_with(call)
  self.assertEqual(ops,['ping'])
 def test_existing_key_lookup_precedes_budget_check(self):
  ops=[]
  def call(name,req):
   ops.append(req['op']);return {'ok':True,'capabilities':{'durable_assignment_keys':1}} if req['op']=='ping' else {'ok':True,'found':True,'id':'original','reused':True}
  self.run_with(call);self.assertEqual(ops,['ping','assignment_lookup'])
 def test_supervisor_replaced_after_capability_check_never_gets_legacy_assign(self):
  ops=[]
  def call(name,req):
   ops.append(req['op'])
   if req['op']=='ping': return {'ok':True,'capabilities':{'durable_assignment_keys':1}}
   if req['op']=='assignment_lookup': return {'ok':True,'found':False}
   return {'ok':False,'error':'unknown lane control operation'}
  with mock.patch.object(xs,'validate_card_type'),mock.patch.object(xs,'load_policy',return_value={'run':{'timeout_seconds':300}}),mock.patch.object(xs,'lanes_by_name',return_value=[{'name':'L1','backend':'codex'}]),mock.patch.object(xs,'lane_call',side_effect=call),mock.patch.object(xs,'check_budget'),contextlib.redirect_stdout(io.StringIO()):
   with self.assertRaises(SystemExit):xs.cmd_lane_assign(self.args)
  self.assertEqual(ops,['ping','assignment_lookup','assign_keyed'])
 def test_concurrent_supervisors_migrate_old_schema_conservatively(self):
  import sqlite3,tempfile
  from concurrent.futures import ThreadPoolExecutor
  with tempfile.TemporaryDirectory() as tmp:
   db=sqlite3.connect(str(Path(tmp)/'lane-assignment-keys.sqlite3'))
   db.execute('CREATE TABLE assignments (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, lane TEXT NOT NULL, run_id TEXT NOT NULL, created REAL NOT NULL)')
   db.execute('INSERT INTO assignments VALUES (?,?,?,?,?)',('key','fingerprint','L1','run',1));db.commit();db.close()
   def open_db(_):
    db=xs.assignment_key_db();v=db.execute('SELECT phase FROM assignments').fetchone()[0];db.close();return v
   with mock.patch.object(xs,'DATA',tmp), ThreadPoolExecutor(max_workers=2) as pool:
    self.assertEqual(list(pool.map(open_db,range(2))),['reserved','reserved'])
 def test_budget_rejection_releases_only_the_unaccepted_key(self):
  import json,socket,tempfile
  with tempfile.TemporaryDirectory() as tmp:
   left,right=socket.socketpair()
   class Server:
    def accept(self):return right,None
   server=Server()
   req={'op':'assign_keyed','idempotency_key':'budget-key','run_id':'budget-run','task':'task','budget_minutes':5}
   left.sendall((json.dumps(req)+'\n').encode())
   with mock.patch.object(xs,'DATA',tmp),mock.patch.object(xs,'RUNS',str(Path(tmp)/'runs')),mock.patch.object(xs.select,'select',return_value=([server],[],[])),mock.patch.object(xs,'jload',return_value={'state':'idle'}),mock.patch.object(xs,'validate_card_type'),mock.patch.object(xs,'load_policy',return_value={'run':{'timeout_seconds':300}}),mock.patch.object(xs,'admit_budget',side_effect=xs.BudgetExceeded({'scope':'per_run'})):
    xs.lane_control_messages(server,{'name':'L1','backend':'codex'}, {},None,{},[])
    reply=json.loads(left.recv(4096));self.assertEqual(reply['error'],'budget_exceeded_daily')
    db=xs.assignment_key_db();self.assertEqual(db.execute('SELECT count(*) FROM assignments').fetchone()[0],0);db.close()
   left.close()
 def test_budget_rejection_rechecks_key_after_concurrent_acceptance(self):
  ops=[]
  def call(name,req):
   ops.append(req['op'])
   if req['op']=='ping':return {'ok':True,'capabilities':{'durable_assignment_keys':1}}
   if ops.count('assignment_lookup')==1:return {'ok':True,'found':False}
   return {'ok':True,'found':True,'id':'first-run','reused':True,'assigned':True}
  out=io.StringIO()
  with mock.patch.object(xs,'validate_card_type'),mock.patch.object(xs,'load_policy',return_value={'run':{'timeout_seconds':300}}),mock.patch.object(xs,'lanes_by_name',return_value=[{'name':'L1','backend':'codex'}]),mock.patch.object(xs,'lane_call',side_effect=call),mock.patch.object(xs,'check_budget',side_effect=xs.BudgetExceeded({'scope':'daily'})),contextlib.redirect_stdout(out):xs.cmd_lane_assign(self.args)
  import json
  self.assertEqual(json.loads(out.getvalue())['id'],'first-run')
  self.assertEqual(ops,['ping','assignment_lookup','assignment_lookup'])
 def test_same_key_concurrent_budget_interleaving_returns_one_run(self):
  import threading
  barrier=threading.Barrier(2);accepted=threading.Event();guard=threading.Lock();counts={};ops=[];errors=[]
  def call(name,req):
   op=req['op'];thread=threading.current_thread().name
   with guard:
    ops.append(op);counts[thread]=counts.get(thread,0)+(op=='assignment_lookup');n=counts[thread]
   if op=='ping':return {'ok':True,'capabilities':{'durable_assignment_keys':1}}
   if op=='assignment_lookup' and n==1:
    barrier.wait(timeout=5);return {'ok':True,'found':False}
   if op=='assignment_lookup':return {'ok':True,'found':True,'id':'first-run','reused':True,'assigned':True}
   if op=='assign_keyed':accepted.set();return {'ok':True,'id':'first-run','assigned':True}
   raise AssertionError(op)
  def budget(*args):
   if threading.current_thread().name=='second':
    assert accepted.wait(5);raise xs.BudgetExceeded({'scope':'daily'})
  def run():
   try:xs.cmd_lane_assign(self.args)
   except BaseException as exc:errors.append(exc)
  with mock.patch.object(xs,'validate_card_type'),mock.patch.object(xs,'load_policy',return_value={'run':{'timeout_seconds':300}}),mock.patch.object(xs,'lanes_by_name',return_value=[{'name':'L1','backend':'codex'}]),mock.patch.object(xs,'lane_call',side_effect=call),mock.patch.object(xs,'check_budget',side_effect=budget),contextlib.redirect_stdout(io.StringIO()):
   threads=[threading.Thread(target=run,name=n) for n in ('first','second')]
   for t in threads:t.start()
   for t in threads:t.join(10)
  self.assertFalse(errors);self.assertFalse(any(t.is_alive() for t in threads))
  self.assertEqual(ops.count('assign_keyed'),1);self.assertEqual(ops.count('assignment_lookup'),3)
if __name__=='__main__':unittest.main()
