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
            binary.write_text('#!/usr/bin/python3\nimport json,subprocess,shutil\n'
                              'g=subprocess.run(["git","status","--porcelain"],capture_output=True,text=True)\n'
                              'n=subprocess.run(["node","--version"],capture_output=True,text=True)\n'
                              'print(json.dumps({"git":g.returncode,"node":n.returncode,"node_path":shutil.which("node"),"parent":open("../tracked").read()}))\n')
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
                self.assertEqual(json.loads(result.stdout), {'git': 0, 'node': 0, 'node_path': '/node/bin/node', 'parent': 'ok'})
            finally:
                sock.close()

    def test_sandbox_tmp_is_a_disk_directory_that_rpc_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            work = root / 'work'
            work.mkdir()
            base = root / 'sandbox-tmp'
            (base / 'openswarm-pi-tmp').mkdir(parents=True)
            (base / 'openswarm-pi-tmp' / 'stale').write_text('old turn')
            (base / 'keep').write_text('not owned by the wrapper')
            runtime = root / 'runtime'
            binary = runtime / 'node_modules' / '.bin' / 'pi'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/usr/bin/python3\nimport json,os\n'
                              'open("/tmp/written","w").write("from sandbox")\n'
                              'print(json.dumps(sorted(os.listdir("/tmp"))))\n')
            binary.chmod(0o755)
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / 'broker.sock')
                sock.bind(path)
                sock.listen()
                env = dict(os.environ, OPENSWARM_PI_RUNTIME=str(runtime), OPENSWARM_PI_BROKER_SOCKET=path,
                           OPENSWARM_PI_SANDBOX_TMP=str(base))
                base_args = ['--provider', 'minimax-cn', '--model', 'MiniMax-M3.1-Flash',
                             '--tools', 'read,bash,edit,write,grep,find,ls']
                one_shot = [str(WRAPPER), '-p', '--mode', 'json', *base_args, 'synthetic']
                result = subprocess.run(one_shot, cwd=work, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue({'stale', 'written'} <= set(json.loads(result.stdout)), result.stdout)
                rpc = [str(WRAPPER), '--mode', 'rpc', *base_args]
                result = subprocess.run(rpc, cwd=work, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                listing = set(json.loads(result.stdout))
                self.assertIn('written', listing)
                self.assertNotIn('stale', listing)
                self.assertEqual((base / 'openswarm-pi-tmp' / 'written').read_text(), 'from sandbox')
                self.assertEqual((base / 'keep').read_text(), 'not owned by the wrapper')
                result = subprocess.run(one_shot, cwd=work, capture_output=True, text=True, timeout=15,
                                        env=dict(env, OPENSWARM_PI_SANDBOX_TMP='relative/tmp'))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('absolute path', result.stderr)
            finally:
                sock.close()


class PiPreflightTests(unittest.TestCase):
    """The wrapper reports its spawn preconditions without starting pi (Refs #1747)."""

    def environment(self, root):
        """An environment whose sandbox dependencies exist, so one guard can be tested at a time."""
        stubs = root / 'bin'
        stubs.mkdir(exist_ok=True)
        for name in ('bwrap', 'socat'):
            stub = stubs / name
            stub.write_text('#!/bin/sh\nexit 0\n')
            stub.chmod(0o755)
        runtime = root / 'runtime'
        binary = runtime / 'node_modules' / '.bin' / 'pi'
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
                   OPENSWARM_PI_RUNTIME=str(runtime))
        env.pop('OPENSWARM_PI_BROKER_SOCKET', None)
        return env

    def preflight(self, env, cwd, model='MiniMax-M3.1-Flash'):
        command = [str(WRAPPER), '--preflight', '--mode', 'rpc', '--provider', 'minimax-cn',
                   '--model', model]
        result = subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30)
        return result, json.loads(result.stdout)

    def test_unset_broker_socket_is_reported_as_the_failing_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self.environment(root)
            result, report = self.preflight(env, root)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report['guard'], 'broker_socket')
            self.assertIn('OPENSWARM_PI_BROKER_SOCKET', report['error'])
            self.assertIsNone(report['broker']['path'])

    def test_a_model_the_broker_does_not_serve_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self.environment(root)
            result, report = self.preflight(env, root, model='another-model')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report['guard'], 'configuration')
            self.assertIn('another-model', report['error'])

    def test_a_live_broker_socket_and_runtime_pass_the_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self.environment(root)
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / 'broker.sock')
                sock.bind(path)
                sock.listen()
                env['OPENSWARM_PI_BROKER_SOCKET'] = path
                result, report = self.preflight(env, root)
                self.assertEqual(result.returncode, 0, report)
                self.assertIsNone(report['guard'])
                self.assertTrue(report['broker']['live'])
            finally:
                sock.close()

    def test_a_stale_broker_socket_is_reported_as_not_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            env = self.environment(root)
            path = root / 'broker.sock'
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            sock.close()  # the file outlives the broker that owned it
            env['OPENSWARM_PI_BROKER_SOCKET'] = str(path)
            result, report = self.preflight(env, root)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report['guard'], 'broker_socket')
            self.assertIn('not live', report['error'])


if __name__ == '__main__':
    unittest.main()
