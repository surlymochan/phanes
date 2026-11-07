#!/usr/bin/env python3
"""Capture and replay evalid evidence with a separately stored test manifest."""
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

SKIP = {'.git', 'node_modules', '__pycache__', '.pytest_cache', '.venv'}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def tests(root):
    result = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not Path(base, d).is_symlink())
        for name in sorted(files):
            p = Path(base, name); rel = p.relative_to(root).as_posix(); parts = rel.split('/')
            if rel.startswith(('docs/', 'specs/')): continue
            if p.suffix not in {'.py','.js','.mjs','.cjs','.ts','.tsx','.jsx','.go','.rs','.java','.kt','.rb','.php'}: continue
            recognized = any(x in {'test','tests','spec','__tests__'} for x in parts[:-1]) or bool(re.search(r'(?:\.test|\.spec|_test)\.[^/]+$', name)) or (name.startswith('test_') and name.endswith('.py'))
            if recognized:
                if p.is_symlink() or not p.is_file(): raise ValueError('test_symlink_or_missing: '+rel)
                result[rel] = digest(p)
    if not result: raise ValueError('empty_test_manifest')
    return result

def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)

def head(root):
    r = run(['git','rev-parse','HEAD'], root)
    if r.returncode: raise ValueError('missing_git_head')
    return r.stdout.strip()

def clean(root, change):
    r = run(['git','status','--porcelain=v1','--untracked-files=all','-z'], root)
    if r.returncode: raise ValueError('git_status_failed')
    base = change.rsplit('-', 1)[0] if change.endswith(('-visible', '-holdout')) else change
    prefixes = tuple(f'specs/changes/{base}-{label}/tdd/' for label in ('visible', 'holdout')) if base != change else (f'specs/changes/{change}/tdd/',)
    for e in r.stdout.split('\0'):
        if e and not (e.startswith('?? ') and e[3:].startswith(prefixes)): raise ValueError('dirty_worktree')

def tree_hashes(root):
    return {p.relative_to(root).as_posix():digest(p) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()}

def atom(ev, name, root, change, phase, command=None):
    a = ['python3',str(ev/'xflow/atoms'/name),'--project-root',str(root),'--change-id',change,'--phase',phase]
    a += ['--test-command',command] if command is not None else ['--tdd-mode','red-green']
    r = run(a)
    try: data = json.loads(r.stdout)
    except ValueError: raise ValueError('invalid_atom_output')
    if r.returncode or not data.get('ok'): raise ValueError('atom_failed:'+str(data.get('verdict',data.get('failure_kind','unknown'))))
    return data

def main():
    p=argparse.ArgumentParser(); p.add_argument('action',choices=['capture','verify']); p.add_argument('--project',required=True); p.add_argument('--evalid',required=True); p.add_argument('--evidence',required=True); p.add_argument('--change-id',required=True); p.add_argument('--command',required=True); p.add_argument('--phase',choices=['red','green','posthoc']); a=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',a.change_id): raise ValueError('invalid_change_id')
    root=Path(a.project).resolve(); ev=Path(a.evalid).resolve(); out=Path(a.evidence).resolve()
    if out==root or root in out.parents: raise ValueError('evidence_must_be_outside_project')
    version=head(ev)
    status=run(['git','status','--porcelain=v1','--untracked-files=all'],ev)
    if status.returncode or status.stdout.strip(): raise ValueError('dirty_evalid_checkout')
    project_head=head(root); clean(root,a.change_id); manifest=tests(root)
    meta_path=out/'record.json'; record=json.loads(meta_path.read_text()) if meta_path.exists() else None
    common={'change_id':a.change_id,'command':a.command,'evalid_commit':version}
    if record and any(record.get(k)!=v for k,v in common.items()): raise ValueError('identity_mismatch')
    if a.action=='capture':
        if not a.phase: raise ValueError('phase_required')
        if record and a.phase in record['phases']: raise ValueError('phase_already_exists')
        if a.phase=='red' and record: raise ValueError('red_must_be_first')
        if a.phase=='green' and (not record or 'red' not in record['phases']): raise ValueError('red_required_use_posthoc')
        if a.phase=='posthoc' and record: raise ValueError('posthoc_must_be_first')
        if record and manifest!=record['test_manifest']: raise ValueError('test_manifest_mismatch')
        if not record: record={**common,'test_manifest':manifest,'phases':{}}
        phase='green' if a.phase=='posthoc' else a.phase
        dest=out/a.phase
        if dest.exists(): raise ValueError('evidence_destination_exists')
        atom(ev,'i6a_tdd_run.py',root,a.change_id,phase,a.command)
        proofdir=root/'specs/changes'/a.change_id/'tdd'; proof=json.loads((proofdir/f'{phase}-0.json').read_text())
        if phase=='red' and proof.get('failure_kind')!='assertion': raise ValueError('red_requires_assertion_failure')
        if tests(root)!=manifest or head(root)!=project_head: raise ValueError('project_changed_during_test')
        clean(root,a.change_id)
        atom(ev,'i6b_tdd_proof_validate.py',root,a.change_id,phase)
        if tests(root)!=manifest or head(root)!=project_head: raise ValueError('project_changed_during_replay')
        clean(root,a.change_id)
        out.mkdir(parents=True,exist_ok=True); dest.mkdir()
        shutil.copy2(proofdir/f'{phase}-0.json',dest/f'{phase}-0.json')
        shutil.copytree(proofdir/'snapshots'/f'{phase}-0',dest/'snapshot')
        record['phases'][a.phase]={'commit':project_head,'proof_tree':tree_hashes(dest)}
        tmp=out/'record.json.tmp'; tmp.write_text(json.dumps(record,indent=2,sort_keys=True)+'\n'); tmp.replace(meta_path)
        return {'ok':True,'action':'capture','phase':a.phase,'commit':project_head,'classification':'tdd' if a.phase=='green' else ('posthoc' if a.phase=='posthoc' else 'red_only'),'evalid_commit':version,'test_count':len(manifest)}
    if not record: raise ValueError('record_missing')
    final='green' if 'green' in record['phases'] else 'posthoc' if 'posthoc' in record['phases'] else None
    if not final: raise ValueError('green_or_posthoc_missing')
    if project_head!=record['phases'][final]['commit']: raise ValueError('commit_mismatch')
    if manifest!=record['test_manifest']: raise ValueError('test_manifest_mismatch')
    proofdir=root/'specs/changes'/a.change_id/'tdd'
    for label,info in record['phases'].items():
        phase='green' if label=='posthoc' else label; source=out/label
        if tree_hashes(source)!=info['proof_tree']: raise ValueError('sealed_evidence_mismatch')
        if digest(source/f'{phase}-0.json')!=digest(proofdir/f'{phase}-0.json') or tree_hashes(source/'snapshot')!=tree_hashes(proofdir/'snapshots'/f'{phase}-0'): raise ValueError('working_evidence_mismatch')
        atom(ev,'i6b_tdd_proof_validate.py',root,a.change_id,phase)
    if tests(root)!=manifest or head(root)!=project_head: raise ValueError('project_changed_during_replay')
    clean(root,a.change_id)
    return {'ok':True,'action':'verify','classification':'tdd' if final=='green' else 'posthoc','commit':project_head,'evalid_commit':version,'test_count':len(manifest),'phases':list(record['phases'])}

if __name__=='__main__':
    try: print(json.dumps(main(),sort_keys=True))
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},sort_keys=True)); sys.exit(1)
