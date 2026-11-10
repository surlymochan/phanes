"""Exercise publish configuration with recorders; no network or Git mutation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublishConfigTests(unittest.TestCase):
    def test_publish_targets_follow_selected_repository_or_explicit_override(self):
        for script in ('pub.sh', 'pub2.sh'):
            for target in (None, 'example/project'):
                with self.subTest(script=script, target=target), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp) / 'alias'
                    directory.symlink_to(Path(tmp), target_is_directory=True)
                    commands = directory / 'commands'
                    commands.mkdir()
                    log = directory / 'calls.jsonl'
                    recorder = f'''#!{sys.executable}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ['CALL_LOG'], 'a') as stream:
    stream.write(json.dumps([name, os.getcwd(), sys.argv[1:]]) + '\\n')
if name == 'ssh':
    print('PUBLISHED abcdef0' if 'bash -s' in sys.argv[-1] else 'abcdef0')
elif name == 'git' and sys.argv[1] == 'rev-parse':
    print('abcdef0')
elif name == 'gh':
    print('https://example.invalid/pr/1')
'''
                    for name in ('ssh', 'scp', 'git', 'gh'):
                        path = commands / name
                        path.write_text(recorder)
                        path.chmod(0o700)
                    env = {key: value for key, value in os.environ.items() if not key.startswith('PHANES_')}
                    env.update(PATH=str(commands) + os.pathsep + env['PATH'], CALL_LOG=str(log))
                    for name in ('SCRATCH_DIR', 'LOCAL_REPO', 'REMOTE_REPO', 'REMOTE_WORKTREE',
                                 'DEPS_SCRIPT', 'DEPS_SOURCE', 'PATCH_DIR'):
                        env['PHANES_' + name] = str(directory)
                    env['PHANES_PUBLISH_HOST'] = 'publish.example.invalid'
                    if target: env['PHANES_PR_REPO'] = target
                    args = ['branch', 'message', 'title', 'body', 'abcdef0']
                    if script == 'pub.sh':
                        args = ['ssh', 'source.example.invalid', 'repo', 'run', *args[:4]]
                    result = subprocess.run(['bash', str(ROOT / 'tools' / script), *args],
                                            env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    gh = next(row for row in calls if row[0] == 'gh')
                    self.assertEqual(Path(gh[1]).resolve(), directory.resolve())
                    if target:
                        self.assertEqual(gh[2][2:4], ['--repo', target])
                    else:
                        self.assertNotIn('--repo', gh[2])
                        self.assertNotIn('-R', gh[2])
                    self.assertTrue(any(row[0] == 'ssh' and 'publish.example.invalid' in row[2] for row in calls))


if __name__ == '__main__':
    unittest.main()
