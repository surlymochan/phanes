"""Offline CLI fixtures for architecture review metrics and its time-window contract."""

import copy
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "architecture_review_metrics.py"
START = "2026-10-26T00:00:00Z"
END = "2026-11-02T00:00:00Z"


def snapshot(prs=(), issues=()):
    return {"repository": "owner/repo", "window": {"start": START, "end": END},
            "captured_at": "2026-11-02T01:00:00Z", "pull_requests": list(prs),
            "issues": list(issues), "completeness": {"complete": True}}


def pull_request(number=1, created="2026-10-27T00:00:00Z", merged=None, comments=(), reviews=()):
    return {"number": number, "url": f"https://github.com/owner/repo/pull/{number}",
            "created_at": created, "updated_at": max(created, merged or "", "2026-11-01T00:00:00Z"), "merged_at": merged,
            "head_ref": f"fleet/test-issue{number}-stamp", "comments": list(comments),
            "reviews": list(reviews)}


def lane_comment(identifier=10, at="2026-10-28T00:00:00Z", verdict="CHANGES", blockers="1",
                 head="a" * 40, run="run-one", number=1):
    body = (f"**Lane review** by test, run `{run}`, head `{head[:10]}`\n\n"
            f"VERDICT: {verdict}\nBLOCKERS: {blockers}\nSUMMARY: fixture\n\n"
            f"<!-- lane-review pr={number} sha={head} verdict={verdict} -->")
    return {"id": identifier, "url": f"https://github.com/owner/repo/pull/{number}#issuecomment-{identifier}",
            "created_at": at, "body": body}


def formal_review(identifier=20, at="2026-10-28T00:00:00Z", state="APPROVED", body="BLOCKERS: 0"):
    return {"id": identifier, "url": f"https://github.com/owner/repo/pull/1#pullrequestreview-{identifier}",
            "submitted_at": at, "state": state, "body": body}


def issue(number=1, labels=(), created="2026-10-01T00:00:00Z", state="open", closed=None):
    return {"number": number, "url": f"https://github.com/owner/repo/issues/{number}",
            "created_at": created, "closed_at": closed, "state": state, "labels": list(labels)}


class ArchitectureReviewMetricsTests(unittest.TestCase):
    def test_compressed_snapshot_replays_and_truncation_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json.gz"
            payload = gzip.compress(json.dumps(snapshot()).encode("utf-8"), mtime=0)
            path.write_bytes(payload)
            command = [sys.executable, str(SCRIPT), "--snapshot-in", str(path)]
            valid = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["reviews"]["total"], 0)
            path.write_bytes(payload[:8])
            broken = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(broken.returncode, 2)
            self.assertNotIn("Traceback", broken.stderr)

    def cli(self, data, *options):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "snapshot.json"
            source.write_text(json.dumps(data), encoding="utf-8")
            before = source.read_bytes()
            result = subprocess.run([sys.executable, str(SCRIPT), "--snapshot-in", str(source), *options],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(source.read_bytes(), before, "analysis must not mutate the snapshot")
            return result

    def report(self, data, *options):
        result = self.cli(data, *options)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_review_and_merge_windows_include_start_and_exclude_end(self):
        comments = [lane_comment(1, "2026-10-25T23:59:59Z", "APPROVE", "0", run="before"),
                    lane_comment(2, START, "APPROVE", "0", run="start"),
                    lane_comment(3, "2026-11-01T23:59:59Z", run="last"),
                    lane_comment(4, END, run="end")]
        data = snapshot([
            pull_request(1, created="2026-10-01T00:00:00Z", comments=comments),
            pull_request(2, created="2026-10-25T23:00:00Z", merged=START),
            pull_request(3, created="2026-10-01T00:00:00Z", merged=END),
        ])
        report = self.report(data)
        self.assertEqual(report["reviews"]["total"], 2)
        self.assertEqual(report["reviews"]["verdicts"], {"APPROVE": 1, "CHANGES": 1})
        self.assertEqual(report["merges"]["count"], 1)
        self.assertEqual(report["merges"]["rows"][0]["number"], 2)
        self.assertEqual(report["merges"]["rows"][0]["wait_seconds"], 3600)
        self.assertEqual(report["review_rounds"]["pull_requests"], 2)

    def test_duplicate_lane_publications_dedup_but_independent_runs_and_heads_remain(self):
        first = lane_comment(1, run="same")
        duplicate = lane_comment(2, "2026-10-28T00:00:10Z", run="same")
        new_run = lane_comment(3, run="different")
        new_head = lane_comment(4, head="b" * 40, run="same")
        report = self.report(snapshot([pull_request(comments=[duplicate, first, new_run, new_head])]))
        self.assertEqual(report["reviews"]["total"], 3)
        self.assertEqual(report["reviews"]["verdicts"]["CHANGES"], 3)
        self.assertEqual(report["review_rounds"]["histogram"].get("3"), 1)
        urls = {url for event in report["reviews"]["events"]
                for url in event["evidence_urls"] + event.get("duplicate_urls", [])}
        self.assertEqual(urls, {item["url"] for item in (first, duplicate, new_run, new_head)})

    def test_formal_reviews_use_submission_timestamp_and_decision_states(self):
        reviews = [formal_review(1, START, "APPROVED"),
                   formal_review(2, "2026-10-30T00:00:00Z", "CHANGES_REQUESTED", "BLOCKERS: 2"),
                   formal_review(3, END, "APPROVED"),
                   formal_review(4, state="COMMENTED"), formal_review(5, state="DISMISSED"),
                   formal_review(6, state="PENDING")]
        report = self.report(snapshot([pull_request(created="2026-10-25T00:00:00Z", reviews=reviews)]))
        self.assertEqual(report["reviews"]["total"], 2)
        self.assertEqual(report["reviews"]["verdicts"], {"APPROVE": 1, "CHANGES": 1})
        self.assertEqual({event["at"] for event in report["reviews"]["events"]},
                         {START, "2026-10-30T00:00:00Z"})

    def test_blocker_buckets_preserve_malformed_or_missing_values_as_unknown(self):
        values = ["0", "1", "2", "3", "22", "-1", "1.5", "many", "", "0\nBLOCKERS: 2"]
        comments = [lane_comment(i + 1, blockers=value, run=f"run-{i}") for i, value in enumerate(values)]
        missing = lane_comment(99, run="missing")
        missing["body"] = missing["body"].replace("BLOCKERS: 1\n", "")
        comments.append(missing)
        report = self.report(snapshot([pull_request(comments=comments)]))
        self.assertEqual(report["reviews"]["total"], 11)
        self.assertEqual(report["reviews"]["blockers"], {"0": 1, "1": 1, "2": 1, "3+": 2, "unknown": 6})

    def test_round_histogram_includes_zero_review_prs_and_excludes_untouched_old_prs(self):
        report = self.report(snapshot([
            pull_request(1),
            pull_request(2, created="2026-10-01T00:00:00Z",
                         comments=[lane_comment(number=2)]),
            pull_request(3, created="2026-10-01T00:00:00Z", merged="2026-10-28T00:00:00Z"),
            pull_request(4, created="2026-10-01T00:00:00Z"),
        ]))
        rounds = report["review_rounds"]
        self.assertEqual(rounds["pull_requests"], 3)
        self.assertEqual(rounds["histogram"].get("0"), 2)
        self.assertEqual(rounds["histogram"].get("1"), 1)
        self.assertEqual({row["number"] for row in rounds["rows"]}, {1, 2, 3})

    def test_merge_wait_distribution_uses_creation_time_and_documented_boundaries(self):
        merged = datetime(2026, 10, 31, tzinfo=timezone.utc)
        durations = [3599, 3600, 21600, 86400, 259200]
        prs = [pull_request(i + 1, created=(merged - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
                            merged=merged.isoformat().replace("+00:00", "Z"))
               for i, seconds in enumerate(durations)]
        report = self.report(snapshot(prs))
        waits = report["merges"]
        self.assertEqual(waits["count"], 5)
        self.assertEqual(waits["wait_seconds"], {"min": 3599, "median": 21600, "p90": 259200, "max": 259200})
        self.assertEqual(waits["buckets"], {"<1h": 1, "1h-6h": 1, "6h-24h": 1, "1d-3d": 1,
                                           ">=3d": 1, "unknown": 0})

    def test_negative_merge_chronology_retains_event_without_fabricating_wait(self):
        created = "2026-11-01T09:57:48Z"
        merged = "2026-11-01T09:08:43Z"
        bad = pull_request(1081, created=created, merged=merged)
        known = pull_request(1082, created="2026-10-30T00:00:00Z", merged="2026-10-30T02:00:00Z")
        waits = self.report(snapshot([bad, known]))["merges"]
        self.assertEqual(waits["count"], 2)
        self.assertEqual(waits["known_wait_count"], 1)
        self.assertEqual(waits["unknown_wait_count"], 1)
        self.assertEqual(waits["buckets"]["unknown"], 1)
        self.assertEqual(waits["buckets"]["1h-6h"], 1)
        self.assertEqual(waits["wait_seconds"], {"min": 7200, "median": 7200, "p90": 7200, "max": 7200})
        row = next(row for row in waits["rows"] if row["number"] == 1081)
        self.assertIsNone(row["wait_seconds"])
        self.assertEqual(row["wait_status"], "invalid_order")
        self.assertEqual(row["url"], bad["url"])
        self.assertEqual(row["created_at"], created)
        self.assertEqual(row["merged_at"], merged)

    def test_open_label_stock_is_live_and_includes_issues_created_before_window(self):
        report = self.report(snapshot(issues=[
            issue(1, ["lane-failed", "lane-ready", "needs-human", "backend:codex", "bug"]),
            issue(2, ["lane-failed"], state="closed", closed="2026-10-29T00:00:00Z"),
            issue(3, ["lane-pr", "follow-up"], created="2026-10-30T00:00:00Z"),
        ]))
        labels = report["issue_labels"]
        self.assertEqual(labels["counts"], {"lane-ready": 1, "lane-failed": 1, "lane-pr": 1,
                                            "needs-human": 1, "follow-up": 1})
        self.assertEqual([item["number"] for item in labels["failed_issues"]], [1])
        self.assertEqual(labels["failed_label_distribution"]["backend:codex"], 1)
        self.assertEqual(labels["failed_label_distribution"]["bug"], 1)
        self.assertNotIn(2, [item["number"] for item in labels["issues_by_label"]["lane-failed"]])

    def test_snapshot_window_and_captured_time_override_live_defaults_and_rerun_deterministically(self):
        data = snapshot([pull_request(comments=[lane_comment()])])
        before = copy.deepcopy(data)
        first = self.cli(data, "--days", "1", "--end", "2027-01-01T00:00:00Z")
        second = self.cli(data)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["window"], data["window"])
        self.assertEqual(report["captured_at"], data["captured_at"])
        self.assertEqual(report["repository"], "owner/repo")
        self.assertEqual(data, before)

    def test_all_event_merge_and_issue_rows_retain_source_links(self):
        item = pull_request(1, merged="2026-10-29T00:00:00Z", comments=[lane_comment()])
        report = self.report(snapshot([item], [issue(9, ["lane-failed"])]))
        event = report["reviews"]["events"][0]
        self.assertEqual(event["pr_url"], item["url"])
        self.assertIn(item["comments"][0]["url"], event["evidence_urls"])
        self.assertEqual(report["merges"]["rows"][0]["url"], item["url"])
        self.assertEqual(report["issue_labels"]["failed_issues"][0]["url"], issue(9)["url"])

    def test_invalid_or_incomplete_snapshots_fail_instead_of_reporting_zero_metrics(self):
        invalid = [None, [], {}, {**snapshot(), "completeness": {"complete": False}},
                   {**snapshot(), "window": {"start": END, "end": START}},
                   {**snapshot(), "captured_at": "not-a-time"}]
        bad_review = snapshot([pull_request(comments=[lane_comment(at="invalid")])])
        invalid.append(bad_review)
        missing_review_time = snapshot([pull_request(comments=[lane_comment()])])
        del missing_review_time["pull_requests"][0]["comments"][0]["created_at"]
        invalid.append(missing_review_time)
        missing = snapshot()
        del missing["issues"]
        invalid.append(missing)
        for data in invalid:
            with self.subTest(data=data):
                result = self.cli(data)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.strip())

    def test_malformed_json_exits_nonzero_with_a_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "truncated.json"
            source.write_text('{"pull_requests": [', encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), "--snapshot-in", str(source)],
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.strip())

    def test_valid_empty_snapshot_is_distinct_from_incomplete_input(self):
        report = self.report(snapshot())
        self.assertEqual(report["reviews"]["total"], 0)
        self.assertEqual(report["merges"]["count"], 0)
        self.assertEqual(report["review_rounds"]["pull_requests"], 0)
        self.assertEqual(report["merges"]["wait_seconds"], {"min": None, "median": None, "p90": None, "max": None})


if __name__ == "__main__":
    unittest.main()
