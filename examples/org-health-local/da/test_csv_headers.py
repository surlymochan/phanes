"""Check CSV headers through the diagnosis command and real input files."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import unittest

if __package__:
    from . import test_build_diagnosis as fixtures
else:
    import test_build_diagnosis as fixtures


class CsvHeaderCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = fixtures.temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.brief = fixtures.write_brief(self.root, fixtures.BRIEF)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.out = self.root / "output" / "diagnosis.json"
        self.restore_raw()

    def restore_raw(self) -> None:
        for name, text in fixtures.MINIMAL_RAW_FILES.items():
            (self.raw / name).write_text(text, encoding="utf-8")

    def cells(self, filename: str) -> list[list[str]]:
        return list(csv.reader(io.StringIO(fixtures.MINIMAL_RAW_FILES[filename], newline="")))

    def write_cells(self, filename: str, rows: list[list[str]], bom: bool = False) -> None:
        buffer = io.StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerows(rows)
        (self.raw / filename).write_text(buffer.getvalue(), encoding="utf-8-sig" if bom else "utf-8")

    def old_output(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text('{"old_snapshot": true}\n', encoding="utf-8")

    def assert_rejected(self, filename: str, columns=()) -> str:
        result = fixtures.run_cli(self.brief, self.raw, self.out)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(filename, result.stderr)
        for column in columns:
            self.assertIn(column, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.out.exists(), "A failed run must not leave a diagnosis file")
        self.assertEqual(list(self.out.parent.glob(f".{self.out.name}.*")), [])
        return result.stderr

    def test_missing_columns_report_every_missing_name_in_all_four_files(self) -> None:
        for filename in fixtures.MINIMAL_RAW_FILES:
            for header_only in (False, True):
                with self.subTest(filename=filename, header_only=header_only):
                    self.restore_raw()
                    rows = self.cells(filename)
                    missing = [rows[0][0], rows[0][-1]]
                    keep = [index for index, name in enumerate(rows[0]) if name not in missing]
                    shortened = [[row[index] for index in keep] for row in rows]
                    if not keep:
                        shortened = [["unrelated"]] + [["unused"] for _ in rows[1:]]
                    self.write_cells(filename, shortened[:1] if header_only else shortened)
                    if header_only:
                        self.old_output()
                    self.assert_rejected(filename, missing)

    def test_blank_optional_headers_are_rejected_without_creating_output(self) -> None:
        for filename in fixtures.MINIMAL_RAW_FILES:
            for blank in ("", "   "):
                with self.subTest(filename=filename, blank=blank):
                    self.restore_raw()
                    rows = self.cells(filename)
                    self.write_cells(filename, [rows[0] + [blank]] + [row + [""] for row in rows[1:]])
                    self.assert_rejected(filename)
                    self.assertFalse(self.out.parent.exists())

    def test_padded_optional_headers_fail_and_remove_stale_output(self) -> None:
        for filename in fixtures.MINIMAL_RAW_FILES:
            with self.subTest(filename=filename):
                self.restore_raw()
                rows = self.cells(filename)
                self.write_cells(filename, [rows[0] + [" extra "]] + [row + ["value"] for row in rows[1:]])
                self.old_output()
                self.assert_rejected(filename)

    def test_header_only_files_still_reject_dirty_headers(self) -> None:
        for filename in fixtures.MINIMAL_RAW_FILES:
            with self.subTest(filename=filename):
                self.restore_raw()
                self.write_cells(filename, [self.cells(filename)[0] + [""]])
                self.old_output()
                self.assert_rejected(filename)

    def test_duplicate_optional_headers_are_rejected_in_every_input(self) -> None:
        for filename in fixtures.MINIMAL_RAW_FILES:
            with self.subTest(filename=filename):
                self.restore_raw()
                rows = self.cells(filename)
                self.write_cells(filename, [rows[0] + ["extra", "extra"]] + [row + ["a", "b"] for row in rows[1:]])
                self.assert_rejected(filename, ["extra"])

    def test_inner_bom_and_control_characters_are_not_valid_header_names(self) -> None:
        for name in ("extra\ufefffield", "extra\tfield", "extra\nfield", "extra\x00field"):
            with self.subTest(name=name):
                self.restore_raw()
                rows = self.cells("forecast.csv")
                self.write_cells("forecast.csv", [rows[0] + [name]] + [row + ["value"] for row in rows[1:]])
                self.assert_rejected("forecast.csv")

    def test_invalid_utf8_header_is_a_data_error_and_removes_stale_output(self) -> None:
        for stale in (False, True):
            with self.subTest(stale=stale):
                self.restore_raw()
                (self.raw / "heads.csv").write_bytes(b"\xffbroken-header\nvalue\n")
                if stale:
                    self.old_output()
                self.assert_rejected("heads.csv")

    def test_later_missing_header_is_reported_before_earlier_bad_data(self) -> None:
        rows = self.cells("cost.csv")
        rows[1][1] = "-1"
        self.write_cells("cost.csv", rows)
        self.write_cells("forecast.csv", [["unrelated"]])
        self.old_output()
        columns = self.cells("forecast.csv")[0]
        self.assert_rejected("forecast.csv", columns)

    def test_reordered_columns_with_leading_bom_preserve_valid_diagnosis(self) -> None:
        result = fixtures.run_cli(self.brief, self.raw, self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = json.loads(self.out.read_text(encoding="utf-8"))
        for filename in fixtures.MINIMAL_RAW_FILES:
            rows = self.cells(filename)
            self.write_cells(filename, [list(reversed(row)) for row in rows], bom=True)
        result = fixtures.run_cli(self.brief, self.raw, self.out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8")), expected)


if __name__ == "__main__":
    unittest.main()
