"""The busy/idle summary of the fleet lanes (Refs #1503).

The report is read only: it looks at the files the fleet already writes and never
dispatches.  Its value is only as good as its agreement with the dispatcher, so the
tests pin the places where the two could disagree: the shape of the status file, the
hot file guard of the dispatcher (documentation files and the union never block, the
declared files are split on every separator, a cap below one is raised to one) and
the rules of the dispatcher that decide which card a lane may take.

Every test runs offline: the queue of the dispatcher is a fake.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "lanes_report.py"
SPEC = importlib.util.spec_from_file_location("dispatch_lanes_report", MODULE_PATH)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


def reasons(got):
    """The reason of every idle lane of a report, lane by lane."""
    return {row["lane"]: row["reason"] for row in got["idle"]}


def fake_gh(cards):
    """A ``gh`` that answers the list of the ready cards and the read of a dependency."""
    def run(args, **kwargs):
        if "list" in args:
            stdout = json.dumps(cards)
        elif "view" in args:
            number = int(args[args.index("view") + 1])
            card = next((card for card in cards if card["number"] == number), None)
            stdout = json.dumps(dict(card, state="OPEN") if card else {"state": "CLOSED"})
        else:
            return subprocess.CompletedProcess(args, 1, "", "unexpected read")
        return subprocess.CompletedProcess(args, 0, stdout, "")
    return run


class LanesReportTests(unittest.TestCase):
    def test_non_idle_fleet_states_are_not_reported_as_dispatchable(self):
        lanes = {"P1": {"backend": "pi"}}
        for status in ("crashed", "starting", "future-state", {"state": "starting"}):
            for queue in ([], [{"backend": "pi"}]):
                with self.subTest(status=status, queue=queue):
                    result = report.summarize(lanes, {"P1": status}, {}, {}, queue)
                    self.assertEqual(reasons(result)["P1"], "unknown")

    """The busy/idle summary and the reasons of the idle lanes."""

    def test_summarize_accepts_caller_can_take(self):
        # B2: callers can replace backend admission with their dispatch rules.
        lanes = {"P1": {"backend": "pi"}, "C1": {"backend": "codex"}}
        card = {"backend": "codex", "strict": True}
        got = report.summarize(lanes, {}, {}, {}, [card], can_take=lambda *a: False)
        self.assertEqual(reasons(got), {"P1": "no_card", "C1": "no_card"})

        rule = mock.Mock(side_effect=lambda backend, item: backend == "pi")
        got = report.summarize(lanes, {}, {}, {}, [card], can_take=rule)
        self.assertEqual(reasons(got), {"P1": "ready", "C1": "no_card"})
        self.assertEqual(rule.call_args_list, [mock.call("pi", card), mock.call("codex", card)])

    def test_caller_can_take_preserves_external_deps_and_hot_guards(self):
        lanes = {"P1": {"backend": "pi"}, "P2": {"backend": "pi"}}
        state = {"P1": {"issue": 12, "hot": ["a.py"]}}
        cases = [([{"backend": "external"}], "no_card"),
                 ([{"backend": "pi", "deps_ok": False}], "deps"),
                 ([{"backend": "pi", "hot": ["a.py"]}], "hot_blocked:a.py")]
        for queue, expected in cases:
            with self.subTest(queue=queue):
                got = report.summarize(lanes, {}, state, {}, queue,
                                       can_take=lambda backend, item: True)
                self.assertEqual(got["busy"], 1)
                self.assertEqual(reasons(got), {"P2": expected})

    def test_cli_rechecks_search_hits_before_reporting_ready(self):
        # B1 (#1503): search labels lag claims, completion and label changes.
        card = {"number": 1503, "body": "", "labels": [{"name": "lane-ready"}]}
        cases = [("OPEN", ["lane-ready", label])
                 for label in ("in-progress", "lane-pr", "lane-failed")]
        cases += [("CLOSED", ["lane-ready"]), ("OPEN", [])]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root)
            (state_dir / "lanes.json").write_text(json.dumps({"P1": {"backend": "pi"}}))
            for state, labels in cases:
                with self.subTest(state=state, labels=labels):
                    fresh = {"state": state, "labels": [{"name": name} for name in labels]}
                    reads = [subprocess.CompletedProcess([], 0, json.dumps([card]), ""),
                             subprocess.CompletedProcess([], 0, json.dumps(fresh), "")]
                    output = io.StringIO()
                    with mock.patch.object(report.subprocess, "run", side_effect=reads) as run, \
                            contextlib.redirect_stdout(output):
                        code = report.main(["--state-dir", root, "--lanes", "lanes.json", "--json"])
                    payload = json.loads(output.getvalue())
                    self.assertEqual(code, 0)
                    self.assertTrue(payload["queue_known"])
                    self.assertEqual(reasons(payload), {"P1": "no_card"})
                    self.assertEqual(len(run.call_args_list), 2)
                    args = run.call_args.args[0]
                    self.assertIn("view", args)
                    self.assertIn("1503", args)
                    self.assertEqual(args[args.index("--json") + 1], "state,labels")

    def test_queue_admission_uses_authoritative_labels_and_configuration(self):
        # B1: fresh labels govern the backend pin, strictness and exclusions too.
        card = {"number": 1503, "body": "",
                "labels": [{"name": "ready-now"}, {"name": "claimed"}, {"name": "backend:pi"}]}
        config = {"repo": "org/queue", "labels": {"ready": "ready-now", "in_progress": "claimed"},
                  "skip_labels": ["manual-only"]}
        cases = [(["ready-now", "backend:codex", "strict-backend"],
                  {"P1": "no_card", "C1": "ready"}),
                 (["ready-now", "backend:external"], {"P1": "no_card", "C1": "no_card"}),
                 (["ready-now", "manual-only"], {"P1": "no_card", "C1": "no_card"}),
                 (["ready-now", "claimed"], {"P1": "no_card", "C1": "no_card"})]
        for labels, expected in cases:
            with self.subTest(labels=labels):
                fresh = {"state": "OPEN", "labels": [{"name": name} for name in labels]}
                reads = [subprocess.CompletedProcess([], 0, json.dumps([card]), ""),
                         subprocess.CompletedProcess([], 0, json.dumps(fresh), "")]
                with mock.patch.object(report.subprocess, "run", side_effect=reads) as run:
                    queue = report._queue(config)
                lanes = {"P1": {"backend": "pi"}, "C1": {"backend": "codex"}}
                self.assertEqual(reasons(report.summarize(lanes, {}, {}, {}, queue, config)), expected)
                for call in run.call_args_list:
                    args = call.args[0]
                    self.assertEqual(args[args.index("-R") + 1], "org/queue")

    def test_unreadable_search_hits_make_the_cli_queue_unknown(self):
        # B1: a failed direct read proves neither readiness nor an empty queue.
        card = {"number": 1503, "body": "", "labels": [{"name": "lane-ready"}]}
        failures = [OSError("no gh"), subprocess.CompletedProcess([], 1, "", "unavailable")]
        failures += [subprocess.CompletedProcess([], 0, payload, "")
                     for payload in ("not json", "[]", '{}', '{"state":"OPEN"}',
                                     '{"state":"OPEN","labels":null}')]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            (Path(root) / "lanes.json").write_text(json.dumps({"P1": {"backend": "pi"}}))
            for failure in failures:
                with self.subTest(failure=failure):
                    reads = [subprocess.CompletedProcess([], 0, json.dumps([card]), ""), failure]
                    output = io.StringIO()
                    with mock.patch.object(report.subprocess, "run", side_effect=reads), \
                            contextlib.redirect_stdout(output):
                        code = report.main(["--state-dir", root, "--lanes", "lanes.json", "--json"])
                    payload = json.loads(output.getvalue())
                    self.assertEqual(code, 0)
                    self.assertFalse(payload["queue_known"])
                    self.assertEqual(reasons(payload), {"P1": "unknown"})

    def test_the_reason_does_not_depend_on_the_shape_of_the_status_file(self):
        """The same lane state gives the same reason in both shapes of the status file.

        The flat map is what the legacy autodispatcher writes, the map with a record
        per lane is what the dispatch engine writes.  A lane the fleet could not read
        carries the state "?" and its reason is "unknown"; a lane whose host read
        failed carries the error text and its reason is "host_fail".
        """
        lanes = {"P1": {"backend": "pi", "host": "h1"},
                 "P2": {"backend": "pi", "host": "h1"},
                 "C1": {"backend": "codex", "host": "h2"}}
        queue = [{"backend": "pi", "deps_ok": True}, {"backend": "codex", "deps_ok": True}]
        flat = {"P1": "busy #12", "P2": "?", "C1": "ssh status timeout",
                "_busy": 1, "_ready": 2, "_blocked": 0}
        schema_two = {"P1": {"state": "busy #12", "issue": 12, "backend": "pi", "host": "h1"},
                      "P2": {"state": "?", "issue": None, "backend": "pi", "host": "h1"},
                      "C1": {"state": "ssh status timeout", "issue": None,
                             "backend": "codex", "host": "h2"},
                      "schema": 2, "_summary": {"busy": 1, "idle": 2}, "_busy": 1, "_ready": 2}
        expected = {"P2": "unknown", "C1": "host_fail"}
        for name, status in (("flat", flat), ("schema 2", schema_two)):
            with self.subTest(shape=name):
                got = report.summarize(lanes, status, {}, {}, queue)
                self.assertEqual(got["busy"], 1, "the busy lane must be counted busy")
                self.assertNotIn("P1", reasons(got), "a busy lane is not an idle lane")
                self.assertEqual({lane: reason for lane, reason in reasons(got).items()
                                  if lane in expected}, expected)

    def test_a_documentation_file_never_blocks_a_lane(self):
        """The dispatcher never refuses a card over a documentation file.

        A card that touches ``README.md`` or ``docs/guide.md`` never waits for
        another lane: documentation is read-mostly and a conflict there costs
        nothing.
        """
        lanes = {"P1": {"backend": "pi"}, "P2": {"backend": "pi"}}
        for held in ("README.md", "docs/guide.md"):
            with self.subTest(hot_file=held):
                state = {"P1": {"issue": 12, "hot": [held]}}
                queue = [{"backend": "pi", "deps_ok": True, "hot": [held]}]
                got = report.summarize(lanes, {}, state, {}, queue)
                self.assertEqual(reasons(got).get("P2"), "ready")

    def test_the_declared_hot_files_are_split_on_every_separator(self):
        """The hot files of a card are the files the dispatcher counts.

        The dispatcher splits the ``热文件：`` line on whitespace, ``,``, ``，``,
        ``、``, ``;`` and ``；``.  A card that declares ``a.py、b.py`` holds two
        files; a report that reads one file with a Chinese comma in its name never
        sees the conflict of ``a.py``.
        """
        card = {"number": 1503, "title": "lane busy/idle summary",
                "body": "热文件：a.py、b.py\n", "labels": [{"name": "lane-ready"}]}
        with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])):
            queue = report._queue(report._merge(report.DEFAULTS, {}))
        self.assertEqual([item.get("hot") for item in queue], [["a.py", "b.py"]])

    def test_a_cap_below_one_is_raised_to_one(self):
        """A cap of zero must not block every card forever.

        The dispatcher raises a cap below one to one, so a file nobody works on is
        free for the next card.
        """
        lanes = {"P1": {"backend": "pi"}, "P2": {"backend": "pi"}}
        config = {"hot_files": {"line_re": r"^\s*(?:热文件|Hot-files)\s*[:：](.*)$",
                                "union": [], "limits": {"a.py": 0}, "default_limit": 1}}
        queue = [{"backend": "pi", "deps_ok": True, "hot": ["a.py"]}]
        got = report.summarize(lanes, {}, {}, {}, queue, config)
        self.assertEqual(reasons(got).get("P2"), "ready")

    def test_the_command_line_reports_the_conflict_of_the_declared_files(self):
        """The command line reads the queue of the fleet and names the conflict.

        The end to end path: the files of the fleet in a state directory, a fake
        ``gh`` that answers with one card whose ``热文件：`` line declares two files
        separated by a Chinese enumeration comma, and the JSON report of the
        command line.
        """
        card = {"number": 1503, "title": "lane busy/idle summary",
                "body": "热文件：a.py、b.py\n", "labels": [{"name": "lane-ready"}]}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root) / "state"
            state_dir.mkdir()
            (state_dir / "lanes.json").write_text(
                json.dumps({"P1": {"backend": "pi", "host": "h1"},
                            "P2": {"backend": "pi", "host": "h1"}}), encoding="utf-8")
            (state_dir / "lanes-status.json").write_text(
                json.dumps({"P1": "busy #12", "_busy": 1, "_ready": 1}), encoding="utf-8")
            (state_dir / "state.json").write_text(
                json.dumps({"P1": {"issue": 12, "hot": ["a.py"]}}), encoding="utf-8")

            output = io.StringIO()
            argv = ["--state-dir", str(state_dir), "--lanes", str(state_dir / "lanes.json"), "--json"]
            with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])), \
                    contextlib.redirect_stdout(output):
                code = report.main(argv)
            self.assertEqual(code, 0, "the report of the fleet must succeed")
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
            reason = reasons(payload).get("P2")
            self.assertTrue(reason and reason.startswith("hot_blocked:"),
                            "the lane waits for the file a.py: {}".format(reason))
            self.assertIn("a.py", reason)

    def test_the_string_lanes_keep_their_name_and_their_count(self):
        """A lane given as a bare name is a lane, not a lost row."""
        got = report.summarize(["P1", "P2"], {"P1": "busy"}, {}, {}, [])
        self.assertEqual(got["busy"], 1)
        self.assertEqual([row["lane"] for row in got["idle"]], ["P2"])
        got = report.summarize({"lanes": ["P1"]}, {}, {}, {}, [])
        self.assertEqual(got["idle"][0]["lane"], "P1")

    def test_the_failed_states_of_the_dispatcher_are_host_fail(self):
        """The states the dispatcher writes when a host fails are host failures."""
        for status, reason in (("missing in batch", "host_fail"), ("down", "host_fail"),
                               ("error", "host_fail"), ("ssh status timeout", "host_fail"),
                               ("host in backoff", "host_fail"), ("draining", "drain")):
            with self.subTest(status=status):
                got = report.summarize({"P": {"backend": "pi"}}, {"P": status}, {}, {}, [])
                self.assertEqual(got["idle"][0]["reason"], reason)

    def test_the_drain_comes_from_the_lane_configuration(self):
        """The dispatcher reads the drain from the lane configuration, so does the report."""
        lanes = {"C2": {"backend": "codex", "drain": True}, "P1": {"backend": "codex"}}
        got = report.summarize(lanes, {}, {}, {}, [])
        self.assertEqual(reasons(got)["C2"], "drain")
        self.assertEqual(reasons(got)["P1"], "no_card")

    def test_ssh_errors_beat_a_dispatchable_card(self):
        # B1: remote() puts raw SSH stderr in lanes-status.json on failure.
        lanes = {"P1": {"backend": "pi"}}
        queue = [{"backend": "pi", "deps_ok": True}]
        errors = ("Permission denied (publickey).", "ssh: Could not resolve hostname h1: "
                  "Name or service not known", "Connection closed by remote host",
                  "Connection reset by peer", "client_loop: send disconnect: Broken pipe",
                  "ssh: connect to host h1 port 22: No route to host")
        for error in errors:
            for entry in (error, {"state": error}):
                with self.subTest(status=entry):
                    got = report.summarize(lanes, {"P1": entry}, {}, {}, queue)
                    self.assertEqual(reasons(got), {"P1": "host_fail"})
        for status in ("idle", "?", "unknown"):
            with self.subTest(status=status):
                got = report.summarize(lanes, {"P1": status}, {}, {}, queue)
                self.assertEqual(reasons(got)["P1"], "ready" if status == "idle" else "unknown")

    def test_the_cards_of_an_external_backend_are_not_dispatchable(self):
        """A card of an external backend is not a card of the fleet.

        The card does not have to say so itself: a backend the fleet does not
        dispatch is an external backend.
        """
        lanes = {"P": {"backend": "pi"}}
        for card in ({"backend": "x"}, {"backend": "other", "external": True}):
            with self.subTest(card=card):
                got = report.summarize(lanes, {}, {}, {}, [card])
                self.assertEqual(got["idle"][0]["reason"], "no_card")

    def test_only_the_cards_with_their_dependencies_satisfied_block_a_lane(self):
        """A card that waits for a dependency never blocks a lane."""
        lanes = {"L": {"backend": "pi"}, "B": {"backend": "pi"}}
        state = {"B": {"issue": 2, "hot": ["x.py"]}}
        queue = [{"backend": "pi", "deps_ok": False, "hot": ["x.py"]},
                 {"backend": "pi", "deps_ok": True, "hot": []}]
        got = report.summarize(lanes, {}, state, {}, queue)
        self.assertEqual(got["idle"][0]["reason"], "ready")
        queue[0]["deps_ok"] = True
        got = report.summarize(lanes, {}, state, {}, queue)
        self.assertEqual(got["idle"][0]["reason"], "ready",
                         "a card without a hot file runs, so the lane is not blocked")

    def test_the_scheduler_shape_names_the_lanes_by_their_key(self):
        """The lane configuration of the dispatcher is a map: the key is the name."""
        lanes = {"P1": {"host": "h1", "backend": "pi"}, "P2": {"backend": "pi"},
                 "C1": {"backend": "codex"}}
        status = {"P1": "busy #12"}
        queue = [{"backend": "pi", "deps_ok": True}, {"backend": "codex", "deps_ok": True}]
        got = report.summarize(lanes, status, {}, {}, queue)
        self.assertEqual(got["busy"], 1)
        self.assertEqual(got["backends"]["pi"], {"busy": 1, "idle": 1})
        self.assertEqual(got["backends"]["codex"], {"busy": 0, "idle": 1})
        self.assertEqual(sorted(reasons(got)), ["C1", "P2"])
        self.assertEqual(reasons(got)["P2"], "ready")
        self.assertEqual(reasons(got)["C1"], "ready")

    def test_a_bare_busy_state_counts_as_busy(self):
        """A lane that runs a card the dispatcher does not know about is busy."""
        got = report.summarize({"P1": {"backend": "pi"}}, {"P1": "busy"}, {}, {}, [])
        self.assertEqual(got["busy"], 1)
        self.assertEqual(got["backends"]["pi"], {"busy": 1, "idle": 0})
        self.assertEqual(got["idle"], [])

    def test_the_pause_of_a_lane_beats_the_missing_card(self):
        """A paused lane is paused, whatever the queue says."""
        lanes = {"N": {"backend": "pi", "host": "h1"}}
        for pauses, reason in (({"lane:N": {}}, "paused:PAUSE-N"),
                               ({"HOST-h1": {}}, "paused:PAUSE-HOST-h1"),
                               ({"host:h1": {}}, "paused:PAUSE-HOST-h1"),
                               ({"backend:pi": {}}, "paused:PAUSE-BACKEND-pi"),
                               ({"all": "PAUSE"}, "paused:PAUSE")):
            with self.subTest(pauses=pauses):
                got = report.summarize(lanes, {}, {}, pauses, [])
                self.assertEqual(got["idle"][0]["reason"], reason)

    def test_the_queue_of_the_other_backends_is_not_a_missing_card(self):
        """The fallback chain of the backends lets a lane take the card of another backend."""
        lanes = {"P1": {"backend": "pi"}}
        got = report.summarize(lanes, {}, {}, {}, [{"backend": "codex", "deps_ok": True}])
        self.assertEqual(got["idle"][0]["reason"], "ready")
        got = report.summarize(lanes, {}, {}, {}, [{"backend": "codex", "deps_ok": True,
                                                    "strict": True}])
        self.assertEqual(got["idle"][0]["reason"], "no_card",
                         "a card pinned with strict-backend is never taken by another backend")

    def test_the_dependencies_of_every_card_block_the_lane(self):
        """The lane waits for the dependency of every card it could take."""
        lanes = {"P": {"backend": "pi"}}
        got = report.summarize(lanes, {}, {}, {}, [{"backend": "pi", "deps_ok": False}])
        self.assertEqual(got["idle"][0]["reason"], "deps")
        got = report.summarize(lanes, {}, {}, {}, [{"backend": "codex", "deps_ok": False}])
        self.assertEqual(got["idle"][0]["reason"], "deps",
                         "a card of another backend is takeable, so the lane waits for it")

    def test_every_idle_reason_of_a_mixed_fleet(self):
        """One fleet, one reason per idle lane: every reason of the enumeration."""
        lanes = {"P1": {"backend": "pi"}, "P2": {"backend": "pi"},
                 "P3": {"backend": "codex", "drain": True}, "P4": {"backend": "codex"},
                 "P5": {"backend": "pi"}, "P6": {"backend": "agy"},
                 "P7": {"backend": "pi"}, "P8": {"backend": "copilot"},
                 "B1": {"backend": "pi"}}
        status = {"P2": "ssh status timeout", "P7": "?"}
        state = {"B1": {"issue": 7, "hot": ["a.py"]}}
        pauses = {"lane:P1": {}}
        queue = [{"backend": "codex", "strict": True, "deps_ok": False, "hot": []},
                 {"backend": "copilot", "strict": True, "deps_ok": True, "hot": ["b.py"]},
                 {"backend": "pi", "strict": True, "deps_ok": True, "hot": ["a.py"]}]
        got = report.summarize(lanes, status, state, pauses, queue)
        self.assertEqual(got["busy"], 1)
        self.assertEqual(reasons(got), {"P1": "paused:PAUSE-P1", "P2": "host_fail",
                                        "P3": "drain", "P4": "deps",
                                        "P5": "hot_blocked:a.py", "P6": "no_card",
                                        "P7": "unknown", "P8": "ready"})

    def test_the_text_summary_of_the_report(self):
        """The text report is one line of the busy lanes and one line per idle lane."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root) / "state"
            state_dir.mkdir()
            (state_dir / "lanes.json").write_text(
                json.dumps({"P1": {"backend": "pi"}, "P2": {"backend": "codex"}}),
                encoding="utf-8")
            output = io.StringIO()
            argv = ["--state-dir", str(state_dir), "--lanes", str(state_dir / "lanes.json")]
            with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([])), \
                    contextlib.redirect_stdout(output):
                code = report.main(argv)
            self.assertEqual(code, 0)
            lines = output.getvalue().strip().splitlines()
            self.assertEqual(lines[0], "busy 0: pi 0/1, codex 0/1")
            self.assertEqual(sorted(lines[1:]), ["P1: no_card", "P2: no_card"])

    def test_the_queue_is_unknown_when_github_is_not_reachable(self):
        """Without a queue the report says so instead of claiming that no card exists."""
        with mock.patch.object(report.subprocess, "run", side_effect=OSError("no gh")):
            self.assertIsNone(report._queue(report.DEFAULTS))
            got = report.summarize({"P1": {"backend": "pi"}}, {}, {}, {}, None)
        self.assertFalse(got["queue_known"])
        self.assertEqual(got["idle"][0]["reason"], "unknown")

    def test_the_cards_of_the_finished_lanes_are_not_in_the_queue(self):
        """The queue of the dispatcher holds the open, ready and unclaimed cards only."""
        card = {"number": 1503, "title": "lane busy/idle summary", "body": "",
                "labels": [{"name": "lane-ready"}]}
        for label in ("in-progress", "lane-pr", "lane-failed"):
            with self.subTest(label=label):
                marked = dict(card, labels=[{"name": "lane-ready"}, {"name": label}])
                with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([marked])):
                    queue = report._queue(report.DEFAULTS)
                self.assertEqual(queue, [])

    def test_the_queue_is_read_from_the_repository_of_the_configuration(self):
        """The queue is read from the repository of the dispatcher, not from the cwd."""
        completed = subprocess.CompletedProcess([], 0, "[]", "")
        with mock.patch.object(report.subprocess, "run", return_value=completed) as run:
            self.assertEqual(report._queue(report._merge(report.DEFAULTS,
                                                         {"repo": "org/repo"})), [])
        self.assertEqual(run.call_args.args[0][2:4], ["-R", "org/repo"])

    def test_the_dependencies_of_the_cards_are_read_on_the_command_line(self):
        """The command line reads the dependencies of a card; an open one blocks the lane.

        The dispatcher reads the ``依赖：#N`` line of the card and checks the state of
        every dependency, so the report of the command line does the same.
        """
        card = {"number": 1503, "title": "lane busy/idle summary",
                "body": "依赖：#99\n", "labels": [{"name": "lane-ready"}]}

        def run(args, **kwargs):
            """Answer the list of the ready cards and the read of the dependency."""
            if "list" in args:
                stdout = json.dumps([card])
            elif "view" in args:
                stdout = json.dumps(dict(card, state="OPEN") if "1503" in args
                                    else {"state": "OPEN"})
            else:
                return subprocess.CompletedProcess(args, 1, "", "unexpected read")
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root) / "state"
            state_dir.mkdir()
            (state_dir / "lanes.json").write_text(
                json.dumps({"P1": {"backend": "pi"}}), encoding="utf-8")
            output = io.StringIO()
            argv = ["--state-dir", str(state_dir), "--lanes", str(state_dir / "lanes.json"),
                    "--json"]
            with mock.patch.object(report.subprocess, "run", side_effect=run), \
                    contextlib.redirect_stdout(output):
                report.main(argv)
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
            self.assertEqual(reasons(payload)["P1"], "deps")

    def test_the_pause_store_of_the_fleet_is_read(self):
        """A lane paused in the pause store is paused, an expired record is not."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root) / "state"
            (state_dir / "pauses").mkdir(parents=True)
            (state_dir / "lanes.json").write_text(
                json.dumps({"P1": {"backend": "pi"}}), encoding="utf-8")
            (state_dir / "pauses" / "lane-p1.json").write_text(
                json.dumps({"scope": "lane:P1", "reason": "maintenance",
                            "expires_at": time.time() + 60}), encoding="utf-8")
            output = io.StringIO()
            argv = ["--state-dir", str(state_dir), "--lanes", str(state_dir / "lanes.json"),
                    "--json"]
            with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([])), \
                    contextlib.redirect_stdout(output):
                report.main(argv)
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
            self.assertEqual(reasons(payload)["P1"], "paused:PAUSE-P1")
            (state_dir / "pauses" / "lane-p1.json").write_text(
                json.dumps({"scope": "lane:P1", "expires_at": time.time() - 60}),
                encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([])), \
                    contextlib.redirect_stdout(output):
                report.main(argv)
            payload = json.loads(output.getvalue().strip().splitlines()[-1])
            self.assertEqual(reasons(payload)["P1"], "no_card")

    def test_home_pause_store_and_marker_survive_expired_records(self):
        # B1/B2: current home store is independent of the legacy state directory.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as root:
            state_dir = Path(root) / "state"
            home = Path(root) / "home"
            state_dir.mkdir()
            (home / "pauses").mkdir(parents=True)
            record = home / "pauses" / "lane-p1.json"
            record.write_text(json.dumps({"scope": "lane:P1", "expires_at": time.time() + 60}))
            lanes = {"P1": {"backend": "pi"}}
            with mock.patch.dict(os.environ, {"OPENSWARM_HOME": str(home)}):
                got = report.summarize(lanes, {}, {}, report._pauses(str(state_dir)), [])
                self.assertEqual(reasons(got)["P1"], "paused:PAUSE-P1")
                (state_dir / "PAUSE-P1").touch()
                record.write_text(json.dumps({"scope": "lane:P1", "expires_at": time.time() - 60}))
                got = report.summarize(lanes, {}, {}, report._pauses(str(state_dir)), [])
                self.assertEqual(reasons(got)["P1"], "paused:PAUSE-P1")

    def test_default_queue_and_dependency_reads_use_dispatcher_repository(self):
        # B3: neither gh read may infer a repository from the working directory.
        card = {"number": 1, "body": "依赖：#2", "labels": [{"name": "lane-ready"}]}
        with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])) as run:
            report._queue({})
        self.assertEqual(len(run.call_args_list), 3)
        for call in run.call_args_list:
            args = call.args[0]
            self.assertIn("-R", args)
            self.assertEqual(args[args.index("-R") + 1], "surlymochan/phanes")

    def test_verify_cards_only_go_to_review_lanes(self):
        # B4: queue parsing and public summary both apply the lane admission gate.
        card = {"number": 1, "body": "## Verify Intent\n目标 PR：#1522\n证据要求：\n- [ ] 测试结果\n",
                "labels": [{"name": "lane-ready"}, {"name": "verify-intent"}]}
        with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])):
            queue = report._queue({})
        self.assertTrue(queue[0]["verify"])
        lanes = {name: {"backend": "pi"} for name in ("P1", "CX2", "CX4-alt", "cx5_test", "CX3")}
        got = reasons(report.summarize(lanes, {}, {}, {}, queue))
        self.assertEqual(got, {"P1": "no_card", "CX2": "ready", "CX4-alt": "ready",
                               "cx5_test": "ready", "CX3": "no_card"})

    def test_invalid_verify_cards_never_make_a_lane_ready(self):
        # B2: a verify label requires the heading, target and evidence checklist.
        lanes = {"P1": {"backend": "pi"}, "CX2": {"backend": "codex"}}
        bodies = ("目标 PR：#1522\n证据要求：\n- [ ] 测试结果\n",
                  "## Verify Intent\n",
                  "## Verify Intent\n证据要求：\n- [ ] 测试结果\n",
                  "## Verify Intent\n目标 PR：\n证据要求：\n- [ ] 测试结果\n",
                  "## Verify Intent\n目标 PR：#1522\n",
                  "## Verify Intent\n目标 PR：#1522\n证据要求：\n- 测试结果\n",
                  "## Verify Intent\n目标 PR：#1522\n证据要求：\n- [ ] \n")
        for body in bodies:
            with self.subTest(body=body):
                card = {"number": 1, "body": body,
                        "labels": [{"name": "lane-ready"}, {"name": "verify-intent"}]}
                with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])):
                    queue = report._queue({})
                self.assertEqual(queue, [])
                self.assertEqual(reasons(report.summarize(lanes, {}, {}, {}, queue)),
                                 {"P1": "no_card", "CX2": "no_card"})

    def test_verify_heading_without_label_remains_an_ordinary_card(self):
        # Only the label asks for the verify contract, as in the dispatcher.
        card = {"number": 1, "body": "## Verify Intent\n",
                "labels": [{"name": "lane-ready"}]}
        with mock.patch.object(report.subprocess, "run", side_effect=fake_gh([card])):
            queue = report._queue({})
        self.assertFalse(queue[0]["verify"])
        self.assertEqual(reasons(report.summarize({"P1": {"backend": "pi"}}, {}, {}, {}, queue)),
                         {"P1": "ready"})


if __name__ == "__main__":
    unittest.main()
