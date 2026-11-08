"""Keep opaque compiler/test-runner outputs out of test source trees (Refs #1753)."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
GENERATED_TEST_BINARY = re.compile(r"\.dde[0-9a-f]+-00000000\.so$")


class GeneratedTestBinaryTests(unittest.TestCase):
    def test_test_trees_do_not_contain_opaque_shared_object_outputs(self):
        artifacts = sorted(
            str(path.relative_to(ROOT))
            for path in ROOT.rglob(".dde*-00000000.so")
            if "tests" in path.relative_to(ROOT).parts
            and GENERATED_TEST_BINARY.fullmatch(path.name)
            and ".git" not in path.relative_to(ROOT).parts
        )

        self.assertEqual(artifacts, [], f"unexpected generated test binaries: {artifacts}")


if __name__ == "__main__":
    unittest.main()
