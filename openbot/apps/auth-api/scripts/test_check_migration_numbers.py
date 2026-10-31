import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from check_migration_numbers import check_migrations


class CheckMigrationNumbersTests(unittest.TestCase):
    def check_names(self, names):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name in names:
                (directory / name).touch()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = check_migrations(directory)
            return result, output.getvalue()

    def test_allows_known_duplicate_0034(self):
        result, output = self.check_names(
            ["0034_server_claims.sql", "0034_account_attribution.sql"]
        )
        self.assertTrue(result)
        self.assertIn("known applied migrations preserved", output)

    def test_rejects_unapproved_duplicate(self):
        result, output = self.check_names(["0001_first.sql", "0001_second.sql"])
        self.assertFalse(result)
        self.assertIn("FAIL duplicate 0001", output)

    def test_rejects_invalid_prefix(self):
        result, output = self.check_names(["1_bad.sql"])
        self.assertFalse(result)
        self.assertIn("expected a four-digit prefix", output)

    def test_allows_gaps(self):
        result, _ = self.check_names(["0001_first.sql", "0003_third.sql"])
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
