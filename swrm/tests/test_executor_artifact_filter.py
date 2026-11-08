import pathlib
import runpy
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SWARM = runpy.run_path(str(ROOT / "bin" / "swrm"))


class ExecutorArtifactFilterTests(unittest.TestCase):
    def test_leaked_issue_card_is_excluded_from_executor_commits(self):
        leaked_card = "openswarm/tests/openswarm-card-412ygy9g.md"

        self.assertTrue(
            SWARM["executor_artifact"](leaked_card, SWARM["NEVER_COMMIT"]),
            "executor artifact filter must exclude the leaked issue-card sample",
        )


if __name__ == "__main__":
    unittest.main()
