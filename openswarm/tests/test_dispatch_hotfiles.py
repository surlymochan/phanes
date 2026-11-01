"""Tests for the hot-file dispatch guard (Refs #1398).

The module lives in a namespace package, so it is loaded by file path instead of by import.
"""
import importlib.util
import pathlib
import unittest

HOTFILES = pathlib.Path(__file__).parents[1] / "lib" / "dispatch" / "hotfiles.py"
_spec = importlib.util.spec_from_file_location("dispatch_hotfiles", HOTFILES)
hotfiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hotfiles)


class ParseHotFilesTests(unittest.TestCase):
    def test_parses_the_chinese_comma(self):
        body = "# 标题\n\n热文件：openswarm/bin/openswarm，openswarm/lib/dispatch/hotfiles.py\n"

        self.assertEqual(
            hotfiles.parse_hot_files(body),
            ["openswarm/bin/openswarm", "openswarm/lib/dispatch/hotfiles.py"],
        )

    def test_parses_the_english_comma_and_spaces(self):
        body = "热文件: a.py, b.py c.py\n"

        self.assertEqual(hotfiles.parse_hot_files(body), ["a.py", "b.py", "c.py"])

    def test_strips_the_markdown_bullet_and_the_backticks(self):
        body = "- 热文件：`a.py`、b.py\n"

        self.assertEqual(hotfiles.parse_hot_files(body), ["a.py", "b.py"])

    def test_merges_several_lines_without_repeating_a_path(self):
        body = "热文件：a.py\n热文件：b.py, a.py\n"

        self.assertEqual(hotfiles.parse_hot_files(body), ["a.py", "b.py"])

    def test_a_card_without_hot_files_declares_none(self):
        self.assertEqual(hotfiles.parse_hot_files("# 标题\n\n验收：pytest\n"), [])
        self.assertEqual(hotfiles.parse_hot_files(""), [])
        self.assertEqual(hotfiles.parse_hot_files(None), [])


class HotLockTests(unittest.TestCase):
    def test_a_file_below_its_cap_does_not_block(self):
        lock = hotfiles.HotLock({"openswarm/bin/openswarm": 2})

        self.assertFalse(lock.blocked(["openswarm/bin/openswarm"], {"openswarm/bin/openswarm": 1}))

    def test_a_file_at_its_cap_blocks(self):
        lock = hotfiles.HotLock({"openswarm/bin/openswarm": 2})

        self.assertTrue(lock.blocked(["openswarm/bin/openswarm"], {"openswarm/bin/openswarm": 2}))

    def test_files_without_an_entry_fall_back_to_the_default_cap(self):
        lock = hotfiles.HotLock({}, default_cap=2)

        self.assertFalse(lock.blocked(["a.py"], {"a.py": 1}))
        self.assertTrue(lock.blocked(["a.py"], {"a.py": 2}))

    def test_the_union_of_the_hot_files_blocks_when_any_one_is_at_its_cap(self):
        lock = hotfiles.HotLock({"a.py": 1, "b.py": 1})

        self.assertFalse(lock.blocked(["a.py", "b.py"], {"a.py": 0, "b.py": 0}))
        self.assertTrue(lock.blocked(["a.py", "b.py"], {"a.py": 1, "b.py": 0}))
        self.assertTrue(lock.blocked(["a.py", "b.py"], {"a.py": 0, "b.py": 1}))

    def test_documentation_files_never_block(self):
        lock = hotfiles.HotLock({"README.md": 1, "openswarm/docs/BUS.md": 1, "docs/guide.py": 1})
        inflight = {"README.md": 7, "openswarm/docs/BUS.md": 7, "docs/guide.py": 7}

        self.assertFalse(lock.blocked(["README.md", "openswarm/docs/BUS.md", "docs/guide.py"], inflight))

    def test_a_documentation_file_does_not_block_the_code_file_next_to_it(self):
        lock = hotfiles.HotLock({"a.py": 1, "README.md": 1})

        self.assertFalse(lock.blocked(["README.md", "a.py"], {"README.md": 3, "a.py": 0}))
        self.assertTrue(lock.blocked(["README.md", "a.py"], {"README.md": 3, "a.py": 1}))

    def test_acquire_counts_the_hot_files_and_release_frees_the_card(self):
        lock = hotfiles.HotLock({"a.py": 1, "b.py": 1})

        self.assertTrue(lock.acquire(["a.py", "b.py"]))
        self.assertEqual(lock.inflight(), {"a.py": 1, "b.py": 1})
        self.assertTrue(lock.blocked(["a.py"]))
        self.assertTrue(lock.blocked(["b.py"]))

        lock.release(["a.py", "b.py"])

        self.assertEqual(lock.inflight(), {})
        self.assertFalse(lock.blocked(["a.py"]))
        self.assertTrue(lock.acquire(["a.py"]))

    def test_acquire_refuses_a_card_whose_hot_file_is_already_busy(self):
        lock = hotfiles.HotLock({"a.py": 1})

        self.assertTrue(lock.acquire(["a.py"]))
        self.assertFalse(lock.acquire(["a.py"]))
        self.assertEqual(lock.inflight(), {"a.py": 1})

    def test_release_never_pushes_the_count_below_zero(self):
        lock = hotfiles.HotLock({"a.py": 1})

        lock.release(["a.py"])

        self.assertEqual(lock.inflight(), {})

    def test_the_card_hot_files_drive_the_lock(self):
        card = "# 标题\n\n热文件：openswarm/bin/openswarm, openswarm/policy.json\n"
        files = hotfiles.parse_hot_files(card)
        lock = hotfiles.HotLock({"openswarm/bin/openswarm": 1})

        self.assertTrue(lock.acquire(files))
        self.assertTrue(lock.blocked(files))
        # A card that touches neither hot file is not held back by them.
        self.assertFalse(lock.blocked(["openswarm/lib/dispatch/hotfiles.py"]))


if __name__ == "__main__":
    unittest.main()
