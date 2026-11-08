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

# A stand-in for bubblewrap: it runs the wrapped command as it is, with the pi binary replaced by
# a fake one, so the wrapper can be checked without user namespaces (Refs #1847).
FAKE_BWRAP = '''#!/usr/bin/env python3
import os, shutil, subprocess, sys
args = sys.argv[1:]
index = args.index("-c")
script = args[index + 1]
root = os.environ["FAKE_SANDBOX_ROOT"]
script = (script.replace("/runtime/node_modules/.bin/pi", os.environ["FAKE_PI_BINARY"])
                .replace("/config/models.json", os.path.join(root, "models.json"))
                .replace("/run/broker.sock", os.environ["FAKE_BROKER_SOCKET"])
                .replace("/tmp/agent", os.path.join(root, "agent"))
                .replace("/tmp/pi-sessions", os.path.join(root, "pi-sessions"))
                .replace("/home/pi", os.path.join(root, "home")))
# The broker relay is not part of this check: the fake sandbox shares the host network.
script = "\\n".join(line for line in script.splitlines() if not line.strip().startswith("socat "))
sys.exit(subprocess.call([shutil.which("sh") or "/bin/sh", "-c", script, *args[index + 2:]]))
'''

# The fake pi reports the command line it was started with.
FAKE_PI = '''#!/usr/bin/env python3
import json, sys
print(json.dumps(sys.argv))
'''


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


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('socat'),
                     'requires Linux and socat')
class PiThinkingFlagTests(unittest.TestCase):
    """The isolated launcher hands the thinking level to pi (Refs #1847)."""

    def wrapper(self, *extra):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binary = root / 'bwrap'
            binary.write_text(FAKE_BWRAP)
            binary.chmod(0o755)
            pi = root / 'pi'
            pi.write_text(FAKE_PI)
            pi.chmod(0o755)
            runtime = root / 'runtime'
            (runtime / 'node_modules' / '.bin').mkdir(parents=True)
            (runtime / 'node_modules' / '.bin' / 'pi').write_text('#!/bin/sh\\nexit 0\\n')
            work = root / 'work'
            work.mkdir()
            (root / 'models.json').write_text('{}')
            sock = socket.socket(socket.AF_UNIX)
            try:
                path = str(root / 'broker.sock')
                sock.bind(path)
                sock.listen()
                env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                           FAKE_PI_BINARY=str(pi), FAKE_SANDBOX_ROOT=str(root),
                           FAKE_BROKER_SOCKET=path, OPENSWARM_PI_RUNTIME=str(runtime),
                           OPENSWARM_PI_BROKER_SOCKET=path)
                command = [str(WRAPPER), '--mode', 'rpc', '--provider', 'minimax-cn', '--model',
                           'MiniMax-M3.1-Flash', '--tools', 'read,bash,edit,write,grep,find,ls', *extra]
                result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=30)
                return result
            finally:
                sock.close()

    def test_the_thinking_level_reaches_pi(self):
        result = self.wrapper('--thinking', 'high')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)
        self.assertIn('--thinking', argv)
        self.assertEqual(argv[argv.index('--thinking') + 1], 'high')

    def test_without_a_level_pi_is_started_without_one(self):
        result = self.wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--thinking', json.loads(result.stdout))

    def test_an_unknown_level_is_refused(self):
        result = self.wrapper('--thinking', 'deeper')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--thinking', result.stderr)


if __name__ == '__main__':
    unittest.main()
