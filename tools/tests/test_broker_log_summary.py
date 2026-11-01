import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "broker-log-summary.py"
spec = importlib.util.spec_from_file_location("broker_log_summary", SCRIPT)
bls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bls)


def line(ts, status, lane="m4-1", err=""):
    return json.dumps({"ts": ts, "id": "r", "lane": lane, "status": status,
                       "err": err, "ms": 10, "in_tok": 1, "out_tok": 1})


def run_report(lines, tz="+08:00", by="minute", window=10.0, since=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broker0-requests.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        records, bad = bls.load_records([str(path)])
    if since:
        since_dt = bls.parse_since(since, bls.parse_tz(tz), records)
        records = [r for r in records if r["ts"] >= since_dt]
    return bls.build_report(records, bad, bls.parse_tz(tz), by, window)


# Today's fragment: healthy 200 until 05:40, then every request is 429.
TODAY_FRAGMENT = (
    [line(f"2026-11-02T05:{m:02d}:00+08:00", 200) for m in (20, 30)]
    + [line(f"2026-11-02T05:{m:02d}:30+08:00", 429) for m in range(40, 60)]
    + [line(f"2026-11-02T06:{m:02d}:30+08:00", 429) for m in range(0, 6)]
)


class BrokerLogSummaryTests(unittest.TestCase):
    def test_today_fragment_is_quota_or_rate_limit(self):
        report = run_report(TODAY_FRAGMENT)
        self.assertEqual(report["verdict"], "quota_or_rate_limit")
        overall = report["overall"]
        self.assertEqual(overall["first_non200"], "2026-11-02T05:40:30+08:00")
        self.assertEqual(overall["longest_429_run"]["count"], 26)
        self.assertEqual(overall["longest_429_run"]["start"], "2026-11-02T05:40:30+08:00")
        self.assertEqual(overall["longest_429_run"]["end"], "2026-11-02T06:05:30+08:00")
        self.assertEqual(overall["counts"]["200"], 2)
        self.assertEqual(report["window"]["non_200"], report["window"]["total"])

    def test_mixed_sample_is_ok(self):
        lines = [line(f"2026-11-02T05:{m:02d}:00+08:00", 429 if m % 3 == 0 else 200)
                 for m in range(0, 30)]
        report = run_report(lines)
        self.assertEqual(report["verdict"], "ok")
        self.assertEqual(report["overall"]["longest_429_run"]["count"], 1)

    def test_upstream_5xx_and_timeouts(self):
        lines = [line(f"2026-11-02T05:{m:02d}:00+08:00", 503, lane="m4-2") for m in range(5)]
        lines += [line(f"2026-11-02T05:{m:02d}:30+08:00", 0, err="request timeout")
                  for m in range(5, 10)]
        report = run_report(lines)
        self.assertEqual(report["verdict"], "upstream_unavailable")
        self.assertEqual(report["overall"]["counts"]["upstream"], 10)

    def test_bad_lines_are_skipped_and_counted(self):
        lines = [
            line("2026-11-02T05:40:00+08:00", 429),
            "not json",
            "[]",
            json.dumps({"ts": "2026-11-02T05:41:00+08:00", "status": "429"}),
            json.dumps({"ts": "bogus", "status": 429}),
            "",
            line("2026-11-02T05:42:00+08:00", 200),
        ]
        report = run_report(lines)
        self.assertEqual(report["bad_lines"], 4)
        self.assertEqual(report["records"], 2)
        self.assertEqual(report["overall"]["total"], 2)

    def test_timezone_conversion(self):
        lines = [line("2026-11-02T05:40:00+00:00", 429),
                 line("2026-11-02T05:41:00", 200)]
        report = run_report(lines, tz="+08:00")
        keys = [g["key"] for g in report["groups"]]
        self.assertEqual(keys, ["2026-11-02 13:40", "2026-11-02 13:41"])
        self.assertEqual(report["overall"]["first_non200"], "2026-11-02T13:40:00+08:00")
        self.assertEqual(report["tz"], "+08:00")
        utc = run_report(lines, tz="Z")
        self.assertEqual([g["key"] for g in utc["groups"]],
                         ["2026-11-02 05:40", "2026-11-02 05:41"])

    def test_since_hhmm_uses_output_timezone(self):
        report = run_report(TODAY_FRAGMENT, since="05:45")
        self.assertEqual(report["overall"]["total"], 15 + 6)
        self.assertEqual(report["overall"]["counts"]["200"], 0)

    def test_lane_grouping(self):
        lines = [line("2026-11-02T05:40:00+08:00", 429, lane="m4-1"),
                 line("2026-11-02T05:40:10+08:00", 200, lane="m4-2")]
        report = run_report(lines, by="lane")
        self.assertEqual([g["key"] for g in report["groups"]], ["m4-1", "m4-2"])

    def test_cli_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker1-requests.jsonl"
            path.write_text("\n".join(TODAY_FRAGMENT) + "\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = bls.main([str(path), "--json", "--tz", "+08:00", "--window", "5"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["verdict"], "quota_or_rate_limit")
        self.assertEqual(data["window"]["minutes"], 5)

    def test_cli_text_prints_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broker2-requests.jsonl"
            path.write_text("\n".join(TODAY_FRAGMENT) + "\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bls.main([str(path), "--tz", "+08:00"])
        self.assertIn("VERDICT quota_or_rate_limit", out.getvalue().splitlines())


if __name__ == "__main__":
    unittest.main()
