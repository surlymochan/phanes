#!/usr/bin/env python3
"""Reproduction of the hot-file conflict preflight of the mint path (Refs #1721).

The dispatcher bounds how many cards may work on the same *hot file* at the same time.
A card declares the files it is about to change with a ``热文件：`` line, the shared list
(HOT_SHARED) gives every file its limit, and a card whose hot file already sits at its
limit cannot be handed to a lane.

The gap of this issue: only the dispatch side judged the hot files.  The mint side (the
planner backlog import, ``replenish()`` in ``tools/lane-autodispatch/autodispatch.py``)
filed its cards without any preflight, so two cards that change the same file entered
the queue one after the other.  The second one could never be dispatched, ``_ready``
counted a card that can never run (the supervisor reads ``_ready`` as the water level),
and the blocked cards piled up without the supervisor being able to see why.

The tests below drive the real ``tick()`` and the real ``replenish()`` of the dispatcher
against a fake fleet (no network, no ssh, no GitHub) and they state the contract of the
issue:

  * the mint path runs the preflight before it files a card: a card whose hot file is at
    its limit is not filed, it keeps its place in the backlog (the next tick retries it)
    and the decision is recorded (留痕) in the AD state directory;
  * a card whose hot file is free is filed as before;
  * a card that is blocked by a hot file is not counted in ``_ready`` and it is reported
    in ``lanes-status.json`` with the blocking file and the time it has been waiting;
  * the caps live in one place: the mint side and the dispatch side read the same list;
  * the replay of the planner batch ``20261101-0335`` agrees: the preflight and the
    dispatcher block exactly the same cards.  The sample
    (``tools/tests/fixtures/hotfiles_ad_sample_1721.json``) is built from the real
    planner cards of this repository; the test recomputes the card hashes, the hot files
    and the in-flight counts of the sample, so the sample cannot drift away from the
    repository it claims to come from.

On the baseline the hot-file tests fail: the dispatcher knows nothing about hot files,
the mint path files every card, and ``lanes-status.json`` carries a single blocked total
without a reason, a file or a waiting time.

Run it offline:

    python3 -m unittest discover -s tools/tests -p 'test_hot_preflight_1721.py' -v
"""
from __future__ import annotations
import sys
from pathlib import Path as _FixturePath
sys.path.append(str(_FixturePath(__file__).resolve().parents[1] / "lane-autodispatch"))
from pr_guard_fixtures import no_open_prs

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import time as time_module
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AD_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
SAMPLE_PATH = ROOT / "tools" / "tests" / "fixtures" / "hotfiles_ad_sample_1721.json"
SCRATCH = ROOT / ".swrm-tmp"

LANE = "GH2"
OTHER_LANE = "GH3"
HOST = "lane-host"
OTHER_HOST = "lane-host-2"

# The hot file of the sample batch: the cards 17 and 18 of the planner batch 20261101-0335
# both change it, so it is the file the shared list has to bound to one card at a time.
HOT_FILE = "swrm/bin/swrm"
OTHER_HOT_FILE = "swrm/tests/test_lessons.py"

# The issue numbers of the fake fleet: they are far away from the small counters of the
# lanes-status row, so a blocked-card detail cannot be confused with a count.
FIRST_CARD = 101
SECOND_CARD = 102


def load_module(name, path=AD_PATH):
    """Load a fresh copy of the dispatcher (every scenario patches its own copy)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def card_text(title, hot_files=(), body="The card changes the files it declares."):
    """The text of a planner card: the title, the labels, the body and the hot files.

    The ``热文件：`` line is the planner's contract: it names the files the card is about
    to change, so the dispatcher and the mint side know which file the card competes for.
    """
    lines = ["# %s" % title, "labels: p2,backend:codex", body]
    if hot_files:
        lines.append("")
        lines.append("热文件：%s" % ", ".join(hot_files))
    return "\n".join(lines) + "\n"


def leaves(node, path=()):
    """Every leaf of a JSON document: (path, value)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, path + (str(index),))
    else:
        yield path, node


def nodes(node, path=()):
    """Every dict of a JSON document: (path, dict)."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from nodes(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from nodes(value, path + (str(index),))


def values_of(node):
    """Every leaf value of a JSON document."""
    return [value for _, value in leaves(node)]


class Clock:
    """The clock of a scenario: it only moves when the test moves it."""

    def __init__(self, start=None):
        self.now = float(start if start is not None else time_module.time())

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now

    def localtime(self, seconds=None):
        return time_module.localtime(self.now if seconds is None else seconds)

    def strftime(self, fmt, stamp=None):
        return time_module.strftime(fmt, self.localtime() if stamp is None else stamp)

    def __getattr__(self, name):
        return getattr(time_module, name)


class Fleet:
    """A fake GitHub and a fake lane: the dispatcher sees cards, never the network."""

    def __init__(self):
        self.issues = {}    # issue number -> the issue row
        self.created = []   # the cards the mint path filed
        self.next_number = 900

    def add_issue(self, number, title, body, labels=("lane-ready",)):
        self.issues[number] = {"number": number, "title": title, "body": body,
                               "labels": list(labels), "state": "OPEN"}
        return self.issues[number]

    def labels_of(self, number):
        return list(self.issues[number]["labels"])

    @no_open_prs

    def gh(self, args, timeout=60):
        if args[:2] == ["issue", "list"]:
            rows = [dict(issue) for issue in sorted(self.issues.values(), key=lambda i: i["number"])]
            label = None
            if "--label" in args:
                label = args[args.index("--label") + 1]
            if label:
                rows = [row for row in rows if label in row["labels"]]
            if label == "planner":
                # the fleet has an open planner issue, so a refill never files a planner
                # card of its own (that card would pollute the mint assertions)
                return 0, json.dumps([{"number": 1, "title": "the open planner issue"}]), ""
            return 0, json.dumps(rows), ""
        if args[:2] == ["issue", "view"]:
            issue = self.issues.get(int(args[2]))
            if issue is None:
                return 1, "", "gh: no such issue"
            return 0, json.dumps({"number": issue["number"], "state": issue["state"],
                                  "labels": [{"name": name} for name in issue["labels"]]}), ""
        if args[:2] == ["issue", "edit"]:
            issue = self.issues.get(int(args[2]))
            if issue is not None:
                for flag in ("--add-label", "--remove-label"):
                    if flag not in args:
                        continue
                    label = args[args.index(flag) + 1]
                    if flag == "--add-label" and label not in issue["labels"]:
                        issue["labels"].append(label)
                    elif flag == "--remove-label" and label in issue["labels"]:
                        issue["labels"].remove(label)
            return 0, "", ""
        if args[:2] == ["issue", "create"]:
            title = args[args.index("-t") + 1]
            body = args[args.index("-b") + 1]
            labels = [args[index + 1] for index, arg in enumerate(args) if arg == "-l"]
            self.created.append({"title": title, "body": body, "labels": labels})
            number = self.next_number
            self.next_number += 1
            self.add_issue(number, title, body, labels)
            return 0, "https://github.com/surlymochan/phanes/issues/%d" % number, ""
        return 0, "", ""

    def sh(self, cmd, timeout=120, inp=None):
        command = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
        if "ls-remote" in command:
            return 0, "%s\trefs/heads/main\n" % ("a" * 40), ""
        match = re.search(r"#(\d+)", command)
        if match:  # the `lane assign` call: the fleet answers with the id of the run
            return 0, json.dumps({"id": "run-%s" % match.group(1)}), ""
        return 0, "", ""


class Scenario:
    """One dispatcher against a fake fleet, in its own sandbox."""

    def __init__(self, root, lanes, clock=None):
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.brain = self.root / "brain-log.md"
        self.brain.write_text("")
        self.module = load_module("autodispatch_hot_%s" % self.root.name)
        self.real_replenish = self.module.replenish
        self.clock = clock or Clock()
        self.fleet = Fleet()
        self.module.D = str(self.state_dir)
        self.module.BRAIN_LOG = str(self.brain)
        self.module.gh = self.fleet.gh
        self.module.sh = self.fleet.sh
        self.module.remote = lambda lane, cfg, run="": {"lane": "idle"}
        self.module.time = self.clock
        self.module.BACKOFF.clear()
        # the refill is not part of a dispatch scenario: the tests that need it call
        # `Scenario.replenish()` on purpose
        self.module.replenish = lambda k, state: []
        self.module.preflight_card = lambda text: None  # these scenarios judge the hot files, not the card lint
        self.module.card_quality = lambda text: None  # nor the dispatch card quality (#1853)
        self.lanes = {name: {"host": host, "backend": "pi", "repo": "/tmp/fleet",
                              "fleet": "/tmp/swrm-fleet"} for name, host in lanes}
        self.state = {}

    # --- the cards of the scenario -------------------------------------------------------
    def add_card(self, number, title, hot_files=(), body="The card changes the files it declares."):
        self.fleet.add_issue(number, title, card_text(title, hot_files, body))
        return number

    def backlog_card(self, name, text):
        """Put a card into the local backlog of the dispatcher (the mint path reads it)."""
        directory = self.state_dir / "backlog"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    # --- the dispatcher ------------------------------------------------------------------
    def tick(self):
        self.module.tick(self.lanes, self.state, 0)
        return self.state

    def replenish(self, k=1):
        """The real mint path: the planner backlog is imported into the queue."""
        self.module.replenish = self.real_replenish
        try:
            return self.real_replenish(k, self.state)
        finally:
            self.module.replenish = lambda k, state: []

    def status(self):
        """The lanes-status row the supervisor reads."""
        path = self.state_dir / "lanes-status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def filed(self):
        """The titles of the cards the mint path filed."""
        return [card["title"] for card in self.fleet.created]

    def trace_texts(self):
        """The machine-readable records of the AD state directory (the mint ledger)."""
        texts = []
        for path in sorted(self.state_dir.rglob("*.json")):
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
        return texts


class HotFileCase(unittest.TestCase):
    """The sandbox every scenario of this file runs in."""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="ad-hot-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.clock = Clock()
        # the pause store of the operator must not leak into the test
        old_home = os.environ.get("OPENSWARM_HOME")
        os.environ["OPENSWARM_HOME"] = str(self.root)
        self.addCleanup(self._restore_home, old_home)

    def _restore_home(self, old_home):
        if old_home is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = old_home

    def scenario(self, lanes=((LANE, HOST),)):
        """A fresh dispatcher scenario with the given lanes."""
        index = len(list(self.root.iterdir()))
        return Scenario(self.root / ("scenario-%d" % index), lanes, self.clock)


class MintPreflightTests(HotFileCase):
    """The mint path: the preflight runs before a card is filed."""

    def test_the_mint_path_refuses_a_card_whose_hot_file_is_at_its_cap(self):
        """A card that competes for a hot file of a running card is not filed."""
        scenario = self.scenario()
        # the lane runs a card that changes the hot file
        scenario.add_card(FIRST_CARD, "the running card", (HOT_FILE,))
        scenario.tick()
        running = [entry.get("issue") for entry in scenario.state.values()
                   if isinstance(entry, dict)]
        self.assertIn(FIRST_CARD, running,
                      "the first card must run, otherwise the scenario proves nothing")
        # the planner writes a second card for the same file into the backlog
        title = "the second card of the same hot file"
        path = scenario.backlog_card("second.md", card_text(title, (HOT_FILE,)))
        scenario.replenish(1)

        self.assertNotIn(title, scenario.filed(),
                         "the mint path filed a card whose hot file (%s) is already at its cap"
                         % HOT_FILE)
        self.assertTrue(path.exists(),
                        "the refused card must keep its place in the backlog for the next tick")

    def test_the_mint_path_files_a_card_whose_hot_file_is_free(self):
        """A card that competes for no hot file is filed as before."""
        scenario = self.scenario()
        scenario.add_card(FIRST_CARD, "the running card", (OTHER_HOT_FILE,))
        scenario.tick()
        title = "a card of its own hot file"
        scenario.backlog_card("free.md", card_text(title, (HOT_FILE,)))
        scenario.replenish(1)

        self.assertIn(title, scenario.filed(),
                      "the mint path refused a card whose hot file (%s) is free" % HOT_FILE)

    def test_the_mint_path_records_the_preflight_result(self):
        """The refusal leaves a trace: the card and the blocking file are recorded."""
        scenario = self.scenario()
        scenario.add_card(FIRST_CARD, "the running card", (HOT_FILE,))
        scenario.tick()
        title = "the second card of the same hot file"
        scenario.backlog_card("second.md", card_text(title, (HOT_FILE,)))
        scenario.replenish(1)

        traces = [text for text in scenario.trace_texts()
                  if HOT_FILE in text and (title in text or "second.md" in text)]
        self.assertTrue(traces,
                        "the refusal left no trace in the AD state directory: no record "
                        "mentions both the card (%s) and the blocking file (%s)"
                        % (title, HOT_FILE))

    def test_the_mint_path_records_the_result_of_an_allowed_card(self):
        """The preflight result is recorded for a card that is filed as well."""
        scenario = self.scenario()
        title = "a card of its own hot file"
        scenario.backlog_card("free.md", card_text(title, (HOT_FILE,)))
        scenario.replenish(1)

        traces = [text for text in scenario.trace_texts()
                  if title in text or "free.md" in text]
        self.assertTrue(traces,
                        "the preflight result of the card (%s) was not recorded" % title)


class BlockedCardTests(HotFileCase):
    """The dispatch side: a hot-file blocked card is blocked, counted and explained."""

    def two_cards_one_lane(self):
        """One lane, two cards that change the same hot file."""
        scenario = self.scenario()
        scenario.add_card(FIRST_CARD, "the first card", (HOT_FILE,))
        scenario.add_card(SECOND_CARD, "the card of the same hot file", (HOT_FILE,))
        return scenario

    def test_a_card_blocked_by_a_hot_file_is_not_counted_as_ready(self):
        """The card that can never be dispatched must not inflate ``_ready``."""
        scenario = self.two_cards_one_lane()
        scenario.tick()
        status = scenario.status()

        self.assertEqual(status.get("_ready"), 0,
                         "_ready is %r: the card whose hot file (%s) is at its cap is still "
                         "counted as dispatchable, the supply the supervisor reads is overstated"
                         % (status.get("_ready"), HOT_FILE))
        self.assertEqual(status.get("_blocked"), 1,
                         "_blocked is %r: the card whose hot file (%s) is at its cap is not "
                         "counted as blocked" % (status.get("_blocked"), HOT_FILE))

    def test_a_free_lane_does_not_take_a_card_whose_hot_file_is_at_its_cap(self):
        """The dispatcher judges the hot files: the second card waits for the first one."""
        scenario = self.scenario(((LANE, HOST), (OTHER_LANE, OTHER_HOST)))
        scenario.add_card(FIRST_CARD, "the first card", (HOT_FILE,))
        scenario.add_card(SECOND_CARD, "the card of the same hot file", (HOT_FILE,))
        scenario.tick()

        self.assertNotIn("in-progress", scenario.fleet.labels_of(SECOND_CARD),
                         "the card of the hot file (%s) was dispatched while the first card "
                         "still holds the file" % HOT_FILE)

    def test_a_lane_whose_only_card_is_blocked_gets_a_new_one(self):
        """The supply water level counts a blocked card as no work: the lane gets a card that does not conflict."""
        scenario = self.two_cards_one_lane()
        scenario.module.LOW = 0  # only the starvation count can trigger the refill
        scenario.module.replenish = scenario.real_replenish
        title = "a fresh card for the starved lane"
        scenario.backlog_card("fresh.md", card_text(title, (OTHER_HOT_FILE,)))
        scenario.tick()

        self.assertIn(title, scenario.filed(),
                      "the lane whose only card is blocked by the hot file (%s) got no new "
                      "card although a card without a conflict was in the backlog" % HOT_FILE)

    def test_a_starved_lane_still_refuses_a_conflicting_card(self):
        """Starvation never lets a card through whose hot file is at its cap."""
        scenario = self.two_cards_one_lane()
        scenario.module.LOW = 0
        scenario.module.replenish = scenario.real_replenish
        conflicting = "a conflicting card for the starved lane"
        free = "a card without a conflict for the starved lane"
        scenario.backlog_card("a-conflict.md", card_text(conflicting, (HOT_FILE,)))
        scenario.backlog_card("b-free.md", card_text(free, (OTHER_HOT_FILE,)))
        scenario.tick()

        self.assertNotIn(conflicting, scenario.filed(),
                         "a card of the hot file (%s) at its cap was created for a starved lane" % HOT_FILE)
        self.assertIn(free, scenario.filed(),
                      "the starved lane got no card although a card without a conflict was in the backlog")

    def test_the_blocked_card_carries_the_blocking_file_and_the_waiting_time(self):
        """The blocked card is explained: the file it waits for and how long it waits."""
        scenario = self.scenario(((LANE, HOST), (OTHER_LANE, OTHER_HOST)))
        scenario.add_card(FIRST_CARD, "the first card", (HOT_FILE,))
        scenario.add_card(SECOND_CARD, "the card of the same hot file", (HOT_FILE,))
        scenario.tick()
        status = scenario.status()

        detail = self.detail_of(status, SECOND_CARD)
        self.assertIsNotNone(detail,
                             "the lanes-status row carries no detail of the blocked card "
                             "#%d: %r" % (SECOND_CARD, status))
        self.assertIn(HOT_FILE, [str(value) for value in values_of(detail)],
                      "the blocked card #%d does not name the file it waits for (%s): %r"
                      % (SECOND_CARD, HOT_FILE, detail))
        wait = self.wait_of(detail)
        self.assertIsNotNone(wait,
                             "the blocked card #%d carries no waiting time: %r"
                             % (SECOND_CARD, detail))
        self.assertGreaterEqual(wait, 0)

    def test_the_waiting_time_grows_while_the_card_waits(self):
        """The waiting time is real: it grows while the card waits."""
        scenario = self.scenario(((LANE, HOST), (OTHER_LANE, OTHER_HOST)))
        scenario.add_card(FIRST_CARD, "the first card", (HOT_FILE,))
        scenario.add_card(SECOND_CARD, "the card of the same hot file", (HOT_FILE,))
        scenario.tick()
        first = self.wait_of(self.detail_of(scenario.status(), SECOND_CARD))
        self.clock.advance(600)
        scenario.tick()
        second = self.wait_of(self.detail_of(scenario.status(), SECOND_CARD))

        self.assertIsNotNone(first, "the blocked card #%d carries no waiting time"
                             % SECOND_CARD)
        self.assertIsNotNone(second, "the blocked card #%d carries no waiting time"
                             % SECOND_CARD)
        self.assertGreater(second, first,
                           "the waiting time of the blocked card #%d did not grow (%r -> %r): "
                           "the waiting time is not measured" % (SECOND_CARD, first, second))

    # --- the helpers of the assertions ---------------------------------------------------
    def detail_of(self, status, number):
        """The blocked-card detail of one issue (None when the status carries none)."""
        for _, node in nodes(status):
            values = [str(value) for value in values_of(node)]
            if str(number) in values:
                return node
        return None

    def wait_of(self, detail):
        """The waiting time of a blocked-card detail (None when it carries none)."""
        if detail is None:
            return None
        for path, value in leaves(detail):
            if not path:
                continue
            if "wait" not in path[-1].lower():
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            return float(value)
        return None


class SharedHotFileListTests(HotFileCase):
    """The caps of the hot files live in one place: both sides read the same list."""

    def declarations(self):
        """Every place of the repository that declares the cap of a hot file.

        A declaration is a mapping of a repository path to a number: in a JSON file a
        section whose name mentions "hot", in a Python file a dict literal of the same
        shape.  The test files and the sample of this issue are not declarations.
        """
        found = []
        for path in self.repository_files():
            if path.suffix == ".json":
                found.extend(self.json_declarations(path))
            elif path.suffix == ".py":
                found.extend(self.python_declarations(path))
        return found

    def repository_files(self):
        """The files of the repository (the scratch, the vendor trees and the tests are skipped)."""
        skip = {".git", ".swrm-tmp", ".tmp", "node_modules", "volto", "phanes", "examples"}
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if relative.parts[0] in skip:
                continue
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            if "fixtures" in relative.parts:
                continue
            yield path

    def json_declarations(self, path):
        """The hot-file caps declared in a JSON file."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        found = []

        def walk(node, path_):
            if isinstance(node, dict):
                for key, value in node.items():
                    if self.is_cap_map(value) and self.is_hot_context(path_ + [str(key)], path):
                        found.append((path, " > ".join(path_ + [str(key)]), value))
                    walk(value, path_ + [str(key)])
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, path_ + [str(index)])

        walk(data, [])
        return found

    def python_declarations(self, path):
        """The hot-file caps declared in a Python file."""
        if not ("hot" in path.name.lower() or path.resolve() == AD_PATH.resolve()):
            return []  # only the hot-file module and the dispatcher can carry the list
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            return []
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            caps = {}
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if not isinstance(value, ast.Constant) or isinstance(value.value, bool):
                    continue
                if not isinstance(value.value, int):
                    continue
                caps[key.value] = value.value
            if self.is_cap_map(caps):
                found.append((path, "line %d" % node.lineno, caps))
        return found

    @staticmethod
    def is_hot_context(key_path, path):
        """True when a declaration is a hot-file list: by its section or by its file name."""
        if any("hot" in part.lower() for part in key_path):
            return True
        name = path.name.lower()
        return "hot" in name or "policy" in name

    @staticmethod
    def is_cap_map(value):
        """True for a mapping of repository paths to their limit."""
        if not isinstance(value, dict) or not value:
            return False
        for key, cap in value.items():
            if not isinstance(key, str) or not isinstance(cap, int) or isinstance(cap, bool):
                return False
            if "/" not in key or "." not in key:
                return False
        return True

    def test_a_shared_hot_file_list_exists(self):
        """The caps of the hot files live in one list that both sides read."""
        declarations = self.declarations()
        where = ["%s (%s)" % (path.relative_to(ROOT), where) for path, where, _ in declarations]
        self.assertTrue(declarations,
                        "no shared hot-file list exists: the caps of the hot files would have "
                        "to be written once per side")

    def test_the_hot_file_caps_are_not_written_twice(self):
        """One list: the mint side and the dispatch side must not keep a list each."""
        declarations = self.declarations()
        self.assertTrue(declarations,
                        "no shared hot-file list exists: the caps would have to be written "
                        "once per side")
        places = {}
        for path, where, caps in declarations:
            for key in caps:
                places.setdefault(key, []).append(path)
        twice = {key: paths for key, paths in places.items() if len(paths) > 1}
        self.assertFalse(twice,
                         "the cap of %s is written in more than one place (%s): the mint side "
                         "and the dispatch side would read different lists"
                         % (", ".join(sorted(twice)),
                            "; ".join("%s in %s" % (key, ", ".join(str(p.relative_to(ROOT)) for p in paths))
                                      for key, paths in sorted(twice.items()))))

    def test_the_dispatcher_reads_the_shared_list(self):
        """The dispatcher reads the shared list instead of carrying a list of its own."""
        declarations = self.declarations()
        self.assertTrue(declarations,
                        "no shared hot-file list exists: the caps would have to be written "
                        "once per side")
        path = declarations[0][0]
        if path.resolve() == AD_PATH.resolve():
            return  # the dispatcher itself carries the one list
        sources = [AD_PATH.read_text(encoding="utf-8")]
        for sibling in sorted(AD_PATH.parent.glob("*.py")):
            sources.append(sibling.read_text(encoding="utf-8"))
        self.assertTrue(any(path.name in source for source in sources),
                        "the dispatcher does not read the shared hot-file list (%s)"
                        % path.relative_to(ROOT))


class SampleReplayTests(HotFileCase):
    """The replay of the planner batch: the preflight and the dispatcher must agree."""

    def setUp(self):
        super().setUp()
        self.sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    # --- the sample ----------------------------------------------------------------------
    def card_text_of(self, card):
        """The card of the sample: the real planner card plus its hot-file declaration."""
        text = (ROOT / card).read_text(encoding="utf-8")
        hot_files = self.sample["cards"][card]["hot_files"]
        return text.rstrip("\n") + "\n\n热文件：%s\n" % ", ".join(hot_files)

    def derived_hot_files(self, card):
        """The hot files of a card, derived from the paths its body names."""
        text = (ROOT / card).read_text(encoding="utf-8")
        token_re = re.compile(r"`([^`\n]+)`")
        anchor_re = re.compile(r":\d+(?:[-:,]\d+)*$")
        files, seen = [], set()
        for token in token_re.findall(text):
            token = token.strip()
            if not token or re.search(r"\s", token):
                continue
            token = anchor_re.sub("", token)
            if "/" not in token or token in seen:
                continue
            if not (ROOT / token).is_file():
                continue
            seen.add(token)
            files.append(token)
        return files

    def test_the_sample_matches_the_repository(self):
        """The sample is the repository: the cards, their hashes and their hot files."""
        for card, record in self.sample["cards"].items():
            path = ROOT / card
            self.assertTrue(path.is_file(), "the sampled card %s is not in the repository" % card)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, record["sha256"],
                             "the sampled card %s changed: the sample of the replay is stale"
                             % card)
            self.assertEqual(self.derived_hot_files(card), record["hot_files"],
                             "the hot files of the sampled card %s are not the files its body "
                             "names any more" % card)

    def test_the_in_flight_state_of_the_sample_is_derived(self):
        """The in-flight counts of the sample are the counts of the dispatched cards."""
        inflight = {}
        for index, case in enumerate(self.sample["cases"]):
            self.assertEqual(inflight, case["inflight"],
                             "the in-flight state of the case %d (%s) does not match the cards "
                             "that were dispatched before it" % (index, case["card"]))
            if case["blocked"]:
                continue  # a blocked card is never dispatched, it holds no hot file
            for path in self.sample["cards"][case["card"]]["hot_files"]:
                inflight[path] = inflight.get(path, 0) + 1

    # --- the replay ----------------------------------------------------------------------
    def test_the_replay_of_the_planner_batch_agrees_with_the_dispatcher(self):
        """The preflight and the dispatcher block the same cards."""
        for index, case in enumerate(self.sample["cases"]):
            self.replay_case(index, case)

    def running_cards(self, index):
        """The cards that are already running when the case is minted (the earlier ones)."""
        return [case["card"] for case in self.sample["cases"][:index] if not case["blocked"]]

    def title_of(self, card):
        """The title of a sampled card."""
        return (ROOT / card).read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()

    def replay_case(self, index, case):
        """One case of the replay: the mint side and the dispatch side must agree."""
        card = case["card"]
        blocked = case["blocked"]
        running = self.running_cards(index)
        # --- the dispatch side: the card waits in the queue of a free lane ---
        dispatch = self.scenario(((LANE, HOST), (OTHER_LANE, OTHER_HOST)))
        for number, running_card in enumerate(running):
            dispatch.add_card(FIRST_CARD + number, self.title_of(running_card),
                              self.sample["cards"][running_card]["hot_files"])
        dispatch.tick()
        dispatch.add_card(SECOND_CARD, self.title_of(card), self.sample["cards"][card]["hot_files"])
        dispatch.tick()
        taken = "in-progress" in dispatch.fleet.labels_of(SECOND_CARD)
        if blocked:
            self.assertFalse(taken,
                             "case %d: the dispatcher took %s although its hot file (%s) is at "
                             "its cap" % (index, card, ", ".join(self.sample["cards"][card]["hot_files"])))
        else:
            self.assertTrue(taken,
                            "case %d: the dispatcher did not take %s although no hot file of "
                            "the card is at its cap" % (index, card))
        # --- the mint side: the card is minted while the earlier cards are running ---
        mint = self.scenario()
        for number, running_card in enumerate(running):
            mint.add_card(FIRST_CARD + number, self.title_of(running_card),
                          self.sample["cards"][running_card]["hot_files"])
        mint.tick()
        mint.backlog_card(card.split("/")[-1], self.card_text_of(card))
        mint.replenish(1)
        filed = self.title_of(card) in mint.filed()
        if blocked:
            self.assertFalse(filed,
                             "case %d: the mint path filed %s although its hot file (%s) is at "
                             "its cap" % (index, card, ", ".join(self.sample["cards"][card]["hot_files"])))
        else:
            self.assertTrue(filed,
                            "case %d: the mint path refused %s although no hot file of the card "
                            "is at its cap" % (index, card))


if __name__ == "__main__":
    unittest.main()
