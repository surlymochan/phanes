"""Check the isolated Pi wrapper against a synthetic credential and host file."""
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

WRAPPER = pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'pi-isolated'


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bwrap') and shutil.which('socat'),
                     'requires Linux, bubblewrap and socat')
class PiIsolationTests(unittest.TestCase):
    def test_host_secrets_and_network_are_not_visible(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            work = root / 'work'
            work.mkdir()
            secret = root / 'outside-canary'
            secret.write_text('synthetic credential')
            deps = root / 'base'
            (deps / 'node_modules').mkdir(parents=True)
            (deps / 'node_modules' / 'marker').write_text('node-dep')
            (deps / '.venv').mkdir()
            (deps / '.venv' / 'marker').write_text('python-dep')
            (work / 'node_modules').symlink_to(deps / 'node_modules')
            (work / '.venv').symlink_to(deps / '.venv')
            runtime = root / 'runtime'
            binary = runtime / 'node_modules' / '.bin' / 'pi'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/usr/bin/python3\nimport json,os,socket\n'
                              's=socket.socket();s.settimeout(1)\n'
                              'try:s.connect(("1.1.1.1",443));network=True\n'
                              'except OSError:network=False\n'
                              'print(json.dumps({"environment": "OS_CANARY" in os.environ,'
                              '"host_file":os.path.exists(' + repr(str(secret)) + '),'
                              '"network":network,"writable":os.access(os.getcwd(),os.W_OK),'
                              '"node_dep":open("node_modules/marker").read(),'
                              '"python_dep":open(".venv/marker").read()}))\n')
            binary.chmod(0o755)
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / 'broker.sock')
                sock.bind(path)
                sock.listen()
                env = dict(os.environ, OS_CANARY='synthetic', OPENSWARM_PI_RUNTIME=str(runtime),
                           OPENSWARM_PI_BROKER_SOCKET=path, OPENSWARM_PI_DEPS_ROOT=str(deps))
                command = [str(WRAPPER), '-p', '--mode', 'json', '--provider', 'minimax-cn', '--model',
                           'MiniMax-M3.1-Flash', '--tools', 'read,bash,edit,write,grep,find,ls', 'synthetic']
                result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'environment': False, 'host_file': False, 'network': False, 'writable': True,
                                  'node_dep': 'node-dep', 'python_dep': 'python-dep'})
                command[command.index('MiniMax-M3.1-Flash')] = 'wrong-model'
                result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
            finally:
                sock.close()

    def test_linked_git_worktree_and_node_remain_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init', '-q')
            (repo / 'tracked').write_text('ok')
            git('add', 'tracked')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial')
            work = root / 'worktree'
            git('worktree', 'add', '--detach', str(work), 'HEAD')
            (work / 'nested').mkdir()
            cwd = work / 'nested'
            runtime = root / 'runtime'
            binary = runtime / 'node_modules' / '.bin' / 'pi'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/usr/bin/python3\nimport json,os,pathlib,subprocess,shutil\n'
                              'g=subprocess.run(["git","status","--porcelain"],capture_output=True,text=True)\n'
                              'n=subprocess.run(["node","--version"],capture_output=True,text=True)\n'
                              'probe=pathlib.Path(".git-write-probe");probe.write_text("probe")\n'
                              'a=subprocess.run(["git","add","--",str(probe)],capture_output=True,text=True)\n'
                              'idx=subprocess.run(["git","rev-parse","--git-path","index"],capture_output=True,text=True)\n'
                              'subprocess.run(["git","reset","--quiet","--",str(probe)],capture_output=True)\n'
                              'ref="refs/swrm-test/pi-isolated-probe"\n'
                              'u=subprocess.run(["git","update-ref",ref,"HEAD"],capture_output=True,text=True)\n'
                              'if u.returncode == 0: subprocess.run(["git","update-ref","-d",ref],capture_output=True)\n'
                              'probe.unlink()\n'
                              'print(json.dumps({"git":g.returncode,"node":n.returncode,"node_path":shutil.which("node"),"parent":open("../tracked").read(),"add":a.returncode,"index":idx.stdout.strip(),"add_error":a.stderr.strip(),"update_ref":u.returncode,"ref_error":u.stderr.strip()}))\n')
            binary.chmod(0o755)
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / 'broker.sock')
                sock.bind(path)
                sock.listen()
                env = dict(os.environ, OPENSWARM_PI_RUNTIME=str(runtime), OPENSWARM_PI_BROKER_SOCKET=path)
                command = [str(WRAPPER), '-p', '--mode', 'json', '--provider', 'minimax-cn', '--model',
                           'MiniMax-M3.1-Flash', '--tools', 'read,bash,edit,write,grep,find,ls', 'synthetic']
                result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected_index = subprocess.check_output(
                    ['git', '-C', str(work), 'rev-parse', '--path-format=absolute', '--git-path', 'index'],
                    text=True).strip()
                payload = json.loads(result.stdout)
                self.assertEqual({k: payload[k] for k in ('git', 'node', 'node_path', 'parent', 'index')}, {
                    'git': 0, 'node': 0, 'node_path': '/node/bin/node', 'parent': 'ok',
                    'index': expected_index})
                # Preserve the existing sandbox policy: shared Git metadata is read-only.
                self.assertEqual(payload['add'], 128, payload)
                self.assertIn(expected_index + '.lock', payload['add_error'])
                self.assertIn('Read-only file system', payload['add_error'])
                self.assertEqual(payload['update_ref'], 128, payload)
                self.assertIn('refs/swrm-test', payload['ref_error'])
                # Git words the refusal differently across versions and across the two
                # ways a new ref can be written ("Read-only file system", "unable to
                # create lock file ...", "unable to create directory for ..."): the
                # shared Git metadata must stay unwritable in every wording.
                self.assertTrue(any(word in payload['ref_error'] for word in
                                    ('Read-only file system', 'read-only', 'unable to create', 'cannot lock')), payload)
                self.assertEqual(subprocess.run(['git', '-C', str(work), 'show-ref', '--verify',
                                                 'refs/swrm-test/pi-isolated-probe'],
                                                capture_output=True, text=True).stdout.strip(), '',
                                 'the shared Git metadata must not hold the new ref')
            finally:
                sock.close()


if __name__ == '__main__':
    unittest.main()
