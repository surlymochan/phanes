"""Verify credential rotation is not silently lost after a failed Keychain update."""
import contextlib
import io
import json
import pathlib
import runpy
import tempfile
import unittest
from unittest import mock


MODULE = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'bin/agy-via-local-proxy'),
                        run_name='agy_runtime_test')
GLOBALS = MODULE['main'].__globals__


class AgyKeychainRuntimeTests(unittest.TestCase):
    def test_keychain_command_is_terminated_by_newline(self):
        result = mock.Mock(returncode=0)
        with mock.patch.object(GLOBALS['subprocess'], 'run', return_value=result) as run:
            MODULE['keychain_write'](b'credential')
        command = run.call_args.kwargs['input']
        self.assertTrue(command.endswith('\n'))
        self.assertFalse(command.endswith('\\n'))

    def test_failed_rotated_token_save_is_retried_on_next_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'token'
            original = json.dumps({'token': {'refresh_token': 'old'}}).encode()
            updated = json.dumps({'token': {'refresh_token': 'new'}}).encode()
            path.write_bytes(updated)
            child = mock.Mock()
            child.wait.return_value = 0
            persist = mock.Mock(side_effect=[RuntimeError('keychain locked'), None])
            with mock.patch.dict(GLOBALS, {'token_path': path, 'keychain_read': mock.Mock(return_value=original),
                                           'ensure_home': mock.Mock(), 'keychain_write': persist}), \
                 mock.patch.object(GLOBALS['subprocess'], 'Popen', return_value=child), \
                 mock.patch.object(GLOBALS['signal'], 'signal'), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(MODULE['main'](), 1)
                self.assertEqual(MODULE['main'](), 0)
            self.assertEqual(persist.call_count, 2)
            self.assertEqual(persist.call_args.args[0], updated)


    def test_file_mode_marks_child_headless_so_agy_skips_os_keyring(self):
        with mock.patch.dict(GLOBALS['os'].environ, {}, clear=True):
            self.assertEqual(MODULE['keyring_mode'](), 'file')
        env = MODULE['child_env']('file', base={'PATH': '/usr/bin'})
        self.assertEqual(env['HOME'], str(GLOBALS['home']))
        self.assertIn('SSH_CONNECTION', env)
        self.assertIn('SSH_CLIENT', env)
        self.assertEqual(env['PATH'], '/usr/bin')

    def test_file_mode_keeps_a_real_ssh_session_value(self):
        env = MODULE['child_env']('file', base={'SSH_CONNECTION': '10.0.0.1 1 10.0.0.2 22'})
        self.assertEqual(env['SSH_CONNECTION'], '10.0.0.1 1 10.0.0.2 22')

    def test_os_mode_leaves_keyring_detection_to_agy(self):
        env = MODULE['child_env']('os', base={})
        self.assertNotIn('SSH_CONNECTION', env)
        self.assertNotIn('SSH_CLIENT', env)

    def test_unknown_keyring_mode_is_rejected(self):
        with mock.patch.dict(GLOBALS['os'].environ, {'OPENSWARM_AGY_KEYRING': 'keychain'}):
            with self.assertRaises(ValueError):
                MODULE['keyring_mode']()

    def test_low_space_moves_only_old_conversations_to_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            home = root / 'home'
            conversations = home / '.gemini/antigravity-cli/conversations'
            conversations.mkdir(parents=True)
            old, new = conversations / 'old.db', conversations / 'new.db'
            old.write_bytes(b'x')
            new.write_bytes(b'y')
            GLOBALS['os'].utime(old, (1000, 1000))
            GLOBALS['os'].utime(new, (90000, 90000))
            env = {'OPENSWARM_HOME': str(root / 'osw'), 'OPENSWARM_AGY_MIN_FREE_MB': '1e12',
                   'OPENSWARM_AGY_ARCHIVE_AFTER_H': '1'}
            with mock.patch.dict(GLOBALS['os'].environ, env):
                moved = MODULE['archive_old_conversations'](root, home, now=90000 + 60)
            self.assertEqual(moved, 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue((root / 'osw/agy-conversation-archive/old.db').exists())

    def test_enough_space_moves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch.dict(GLOBALS['os'].environ, {'OPENSWARM_AGY_MIN_FREE_MB': '0'}):
                self.assertEqual(MODULE['archive_old_conversations'](root, root / 'home'), 0)


if __name__ == '__main__':
    unittest.main()
