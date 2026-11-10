"""Acceptance gate for the planner batch 20261103-0313 (Refs #1781).

The planner batch is the queue source the autodispatcher imports: `replenish()`
reads `docs/backlog/next/*.md` on origin/main and files one issue per card
(`tools/lane-autodispatch/autodispatch.py`).  A batch that is missing, thin, or
that names files the repository does not contain cannot refill the lane queue,
so the rules the planner issue announces are checked here as executable
assertions:

  * exactly the 20 announced files exist, including shell count validation;
  * every card keeps the announced section order (title / labels / body /
    验收) and passes the dispatcher's own card predicate
    (`tools/check-ad-ready-target.py`);
  * the acceptance line runs real behaviour instead of a grep pipeline;
  * the paths a card names live in the repository (a card that points at a
    moved tree cannot be dispatched);
  * the backend mix keeps every lane fed and no card repeats another one.

The original 17 assertions fail on the baseline: the batch was never written.

The issue's original pipeline ends with ``grep -qx 20``, which rejects BSD wc's
space-padded count on macOS. The portable replacement is::

    ls docs/backlog/next/20261103-0313-*.md | wc -l | grep -Eq '^[[:space:]]*20[[:space:]]*$'

Both pipelines are exercised below, including a padded-wc reproduction. Updating
the external issue/host acceptance command remains necessary; card contents
cannot change the output formatting of the host's wc.
"""

import glob
import importlib.util
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NEXT = ROOT / "docs" / "backlog" / "next"
TAG = "20261103-0313"
COUNT = 20
CARD_NAMES = [f"{TAG}-{index:02d}.md" for index in range(1, COUNT + 1)]
COUNT_PIPELINE = f"ls docs/backlog/next/{TAG}-*.md | wc -l"
ISSUE_COUNT_COMMAND = COUNT_PIPELINE + " | grep -qx 20"
PORTABLE_COUNT_COMMAND = COUNT_PIPELINE + " | grep -Eq '^[[:space:]]*20[[:space:]]*$'"
AD_PATH = ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"
READY_TARGET_PATH = ROOT / "tools" / "check-ad-ready-target.py"

# The planner issue bans these topics outright; a card that hits one of them is
# a thin card the dispatcher refuses to import.
FORBIDDEN_TOPICS = (
    "文档一句", "常用命令", "README", "playbook", "checklist", "补充一行",
    "文档-only", "docs-only", "only-docs", "只改文档", "失败提示单测",
    "提示文案单测", "纯单元边界灌水", "空·缺字段灌水", "路径存在性检查类文档 chore",
)

# The announced backend mix is pi / agy / codex / none = 2 : 1 : 2 : 1 over 20
# cards, and at least three cards must carry no backend at all so a starved
# lane can still pick them up.
BACKEND_RANGE = {"pi": (5, 9), "agy": (2, 5), "codex": (5, 9), None: (3, 5)}

CODE_SPAN = re.compile(r"`([^`\n]+)`")
COMMAND_PATH = re.compile(r"[\w][\w./-]*\.(?:py|sh|ts|tsx|js|md|json|toml|yaml|yml)")
CJK = re.compile(r"[一-鿿]")
LABELS_LINE = re.compile(r"^labels:\s*(.+)$")
LINE_ANCHOR = re.compile(r":\d+(?:[-:,]\d+)*$")
SIZE_CLAIM = re.compile(r"(\d{2,3})\s*[+*~]?\s*(?:行|LOC)", re.IGNORECASE)
MINUTES_CLAIM = re.compile(r"\d+\s*(?:[-–—]\s*\d+\s*)?分钟")
DEPENDENCY = re.compile(r"(?:依赖|阻塞于|等待)\s*#\d+")
SUCCESS_CLAIM = re.compile(r"成功|通过|正常")
FAILURE_CLAIM = re.compile(r"失败|异常|拒绝|报错|不可用|非零|缺失|超时")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AD = load_module("autodispatch_batch_0313", AD_PATH)
READY_TARGET = load_module("ready_target_batch_0313", READY_TARGET_PATH)


def batch_files():
    """The files the announced acceptance command would list."""
    if not NEXT.is_dir():
        return []
    return sorted(path for path in NEXT.glob(f"{TAG}-*.md") if path.is_file())


def load_cards():
    """The announced cards, keyed by file name."""
    return {path.name: path.read_text(encoding="utf-8") for path in batch_files()}


def batch_gap(cards):
    """A compact description of the missing part of the batch."""
    missing = [name for name in CARD_NAMES if name not in cards]
    if not missing:
        return ""
    return ("the planner batch %s is incomplete: %d of %d announced cards are missing "
            "(%s ... %s)" % (TAG, len(missing), COUNT, missing[0], missing[-1]))


def code_spans(text):
    return [span.strip() for span in CODE_SPAN.findall(text)]


def clean_path(token):
    """Strip the punctuation and the `file.py:12-24` anchors of a code span."""
    token = token.strip().strip(",;:：，。")
    return LINE_ANCHOR.sub("", token)


def is_path_like(token):
    """True for a repository path, not for a command or a prose fragment."""
    if not token or " " in token or token.startswith(("~", "$", "http", "-")):
        return False
    return "/" in token or "." in token


def resolves(token):
    """True when the token names an existing file or a glob with matches."""
    if any(char in token for char in "*?["):
        return bool(glob.glob(str(ROOT / token)))
    return (ROOT / token).exists()


def parent_exists(token):
    """True when the directory that would hold the token exists."""
    parent = Path(token).parent
    if str(parent) in ("", "."):
        return True
    return (ROOT / parent).is_dir()


def command_paths(text):
    """The file paths a text names, also inside a longer command span."""
    found = []
    for match in COMMAND_PATH.finditer(text):
        token = clean_path(match.group(0))
        if token and is_path_like(token):
            found.append(token)
    return found


def card_paths(text):
    """The repository paths a card names, without the acceptance line."""
    lines = [line for line in text.splitlines() if not line.strip().startswith("验收：")]
    body = "\n".join(lines)
    paths = []
    for span in code_spans(body):
        token = clean_path(span)
        if is_path_like(token):
            paths.append(token)
    paths.extend(command_paths(body))
    return paths


def is_behaviour_command(command):
    """True when the acceptance command runs real behaviour.

    A grep pipeline (`rg ... | head`) is not a behaviour: the acceptance must
    run pytest, a python entry point, or an existing repository script.
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    head = tokens[0].lower()
    if head in ("rg", "grep", "ag", "ack", "cut", "head", "tail"):
        return False
    lowered = command.lower()
    if ("rg " in lowered or "grep " in lowered) and not any(
            name in lowered for name in ("python", "pytest", "unittest")):
        return False
    if any(name in lowered for name in ("python", "pytest", "unittest")):
        return True
    for token in tokens:
        candidate = token.strip("'\"")
        if candidate.startswith(("./", "tools/", "bin/", "swrm/")) and (ROOT / candidate).exists():
            return True
    return False


def acceptance_command(text):
    """The backticked command of the 验收 line, if the card has one."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("验收："):
            spans = code_spans(stripped)
            return spans[0] if spans else ""
    return ""


def card_labels(text):
    """The labels the dispatcher would file the card with."""
    lines = text.splitlines()
    if len(lines) < 2:
        return []
    match = LABELS_LINE.match(lines[1].strip())
    if not match:
        return []
    return [label.strip() for label in match.group(1).split(",") if label.strip()]


def card_title(text):
    lines = text.splitlines()
    return lines[0].strip().lstrip("#").strip() if lines else ""


def card_body(text):
    """The body lines of a card, without the labels line and the 验收 line."""
    lines = text.splitlines()[2:]
    return [line for line in lines if line.strip() and not line.strip().startswith("验收：")]


def existing_titles():
    """The titles of the cards the repository already holds."""
    titles = set()
    if not NEXT.is_dir():
        return titles
    for path in NEXT.glob("*.md"):
        if path.name in CARD_NAMES:
            continue
        title = " ".join(card_title(path.read_text(encoding="utf-8")).split()).lower()
        if title:
            titles.add(title)
    return titles


class BatchCase(unittest.TestCase):
    """Shared batch loading: every rule needs the announced cards."""

    def setUp(self):
        self.cards = load_cards()

    def require_batch(self):
        gap = batch_gap(self.cards)
        self.assertEqual(gap, "", gap)


class BatchPresenceTests(BatchCase):
    """The announced acceptance command: exactly twenty files."""

    def test_batch_has_exactly_the_twenty_announced_files(self):
        files = batch_files()
        names = [path.name for path in files]
        missing = [name for name in CARD_NAMES if name not in names]
        self.assertEqual(missing, [], "the planner batch is incomplete: %d of %d announced cards "
                                       "are missing (%s ... %s)"
                         % (len(missing), COUNT, missing[0] if missing else "-",
                            missing[-1] if missing else "-"))
        self.assertEqual(len(files), COUNT,
                         "the batch must list exactly %d cards, found %d: %s"
                         % (COUNT, len(files), names))

    def test_batch_files_are_not_empty(self):
        self.require_batch()
        for path in batch_files():
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.strip(), "%s is empty" % path.name)


class BatchCountCommandTests(BatchCase):
    """Run shell acceptance, including the macOS wc formatting difference."""

    def run_command(self, command, cwd=ROOT):
        return subprocess.run(
            ["sh", "-c", command], cwd=cwd, capture_output=True, text=True, timeout=10,
        )

    def test_original_issue_pipeline_matches_native_wc_format(self):
        self.require_batch()
        count = self.run_command(COUNT_PIPELINE)
        self.assertEqual(count.returncode, 0, count.stderr)
        self.assertEqual(int(count.stdout.strip()), COUNT)
        result = self.run_command(ISSUE_COUNT_COMMAND)
        # Execute the exact issue command, without silently substituting a
        # different grep pattern or wrapping the native wc on the real batch.
        expected = 0 if count.stdout.rstrip("\n") == "20" else 1
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_portable_pipeline_accepts_the_real_batch(self):
        self.require_batch()
        result = self.run_command(PORTABLE_COUNT_COMMAND)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_padded_wc_accepts_twenty_and_rejects_incomplete_or_extra_cards(self):
        # Count actual files with native wc, then format its result as BSD wc
        # does. No macOS executable or change to the user's PATH is needed.
        padded_wc = 'wc() { wc_count=$(command wc "$@"); printf "%8d\\n" "$wc_count"; }; '
        with tempfile.TemporaryDirectory(prefix=".batch-0313-", dir=ROOT) as temporary:
            root = Path(temporary)
            directory = root / "docs" / "backlog" / "next"
            directory.mkdir(parents=True)
            for name in CARD_NAMES[:-1]:
                (directory / name).write_text("# 计数夹具\n", encoding="utf-8")
            for count in (19, 20, 21):
                if count >= 20:
                    (directory / f"{TAG}-{count:02d}.md").write_text("# 计数夹具\n", encoding="utf-8")
                with self.subTest(count=count):
                    formatted = self.run_command(padded_wc + COUNT_PIPELINE, cwd=root)
                    self.assertEqual(formatted.returncode, 0, formatted.stderr)
                    self.assertEqual(formatted.stdout, f"{count:8d}\n")
                    original = self.run_command(padded_wc + ISSUE_COUNT_COMMAND, cwd=root)
                    self.assertEqual(original.returncode, 1, original.stderr)
                    portable = self.run_command(padded_wc + PORTABLE_COUNT_COMMAND, cwd=root)
                    self.assertEqual(portable.returncode, 0 if count == COUNT else 1, portable.stderr)


class CardShapeTests(BatchCase):
    """The strict section order the planner issue announces."""

    def test_card_has_title_labels_body_and_acceptance(self):
        self.require_batch()
        for name, text in self.cards.items():
            lines = text.splitlines()
            self.assertTrue(lines[0].startswith("# "), "%s: the card must start with a title" % name)
            self.assertTrue(CJK.search(lines[0]), "%s: the title must be a Chinese title" % name)
            self.assertTrue(LABELS_LINE.match(lines[1].strip()),
                            "%s: the second line must be the labels line" % name)
            self.assertTrue(card_body(text), "%s: the card needs a body" % name)
            self.assertTrue(acceptance_command(text),
                            "%s: the card needs a 验收 line with a command" % name)

    def test_card_labels_name_a_priority_and_at_most_one_backend(self):
        self.require_batch()
        for name, text in self.cards.items():
            labels = card_labels(text)
            self.assertTrue(labels, "%s: the labels line is empty" % name)
            self.assertIn(labels[0], ("p1", "p2"), "%s: the first label must be p1 or p2" % name)
            backends = [label.split(":", 1)[1] for label in labels if label.startswith("backend:")]
            self.assertLessEqual(len(backends), 1, "%s: at most one backend label" % name)
            for backend in backends:
                self.assertIn(backend, ("pi", "codex", "agy"),
                              "%s: unknown backend %r" % (name, backend))

    def test_card_passes_the_dispatcher_card_predicate(self):
        self.require_batch()
        for name, text in self.cards.items():
            self.assertFalse(READY_TARGET.is_thin_card(text),
                             "%s: the dispatcher would reject this thin card" % name)

    def test_card_body_states_the_scope_the_success_and_the_failure(self):
        self.require_batch()
        for name, text in self.cards.items():
            body = "\n".join(card_body(text))
            self.assertGreaterEqual(len(body.strip()), 60,
                                    "%s: the body must describe the change" % name)
            self.assertTrue(SUCCESS_CLAIM.search(body), "%s: the body must state the success path" % name)
            self.assertTrue(FAILURE_CLAIM.search(body),
                            "%s: the body must state the failure path" % name)

    def test_card_body_states_the_expected_size_and_the_time_budget(self):
        self.require_batch()
        for name, text in self.cards.items():
            body = "\n".join(card_body(text))
            sizes = [int(value) for value in SIZE_CLAIM.findall(body)]
            self.assertTrue(any(size >= 30 for size in sizes),
                            "%s: the body must state an expected diff of at least 30 lines" % name)
            self.assertTrue(MINUTES_CLAIM.search(body),
                            "%s: the body must state the time budget" % name)

    def test_card_does_not_depend_on_another_unfinished_task(self):
        self.require_batch()
        for name, text in self.cards.items():
            self.assertIsNone(DEPENDENCY.search(text),
                              "%s: a card must not depend on another unfinished task" % name)

    def test_card_avoids_the_banned_thin_topics(self):
        self.require_batch()
        for name, text in self.cards.items():
            haystack = "\n".join([card_title(text)] + card_body(text))
            for topic in FORBIDDEN_TOPICS:
                self.assertNotIn(topic, haystack,
                                 "%s: the card hits the banned topic %r" % (name, topic))


class CardGroundingTests(BatchCase):
    """A card must point at files the repository really has."""

    def test_card_names_at_least_one_existing_repository_path(self):
        self.require_batch()
        for name, text in self.cards.items():
            paths = card_paths(text)
            self.assertTrue(paths, "%s: the body must name the files to change" % name)
            self.assertTrue(any(resolves(path) for path in paths),
                            "%s: no named path exists in the repository: %s" % (name, paths))

    def test_card_paths_are_not_stale_trees(self):
        self.require_batch()
        for name, text in self.cards.items():
            for path in card_paths(text):
                self.assertTrue(parent_exists(path),
                                "%s: %s does not live in the repository" % (name, path))

    def test_acceptance_command_runs_real_behaviour(self):
        self.require_batch()
        for name, text in self.cards.items():
            command = acceptance_command(text)
            self.assertTrue(is_behaviour_command(command),
                            "%s: %r is not a behaviour command" % (name, command))

    def test_acceptance_command_paths_are_not_stale_trees(self):
        self.require_batch()
        for name, text in self.cards.items():
            command = acceptance_command(text)
            for candidate in command_paths(command):
                self.assertTrue(parent_exists(candidate),
                                "%s: the acceptance path %s does not live in the repository"
                                % (name, candidate))


class CardDispatchContractTests(BatchCase):
    """The dispatcher must be able to file every card of the batch."""

    def test_card_matches_the_replenish_import_contract(self):
        self.require_batch()
        for name, text in self.cards.items():
            self.assertTrue(text.startswith("#"), "%s: replenish() imports a markdown card" % name)
            self.assertTrue(text.splitlines()[1].startswith("labels:"),
                            "%s: replenish() needs the labels on the second line" % name)

    def test_dispatcher_files_one_issue_per_card(self):
        self.require_batch()
        created = []

        def fake_gh(args, timeout=60):
            created.append(list(args))
            return 0, "https://github.com/surlymochan/phanes/issues/1234", ""

        original, original_lint = AD.gh, AD.preflight_card
        AD.gh = fake_gh
        AD.preflight_card = lambda text: None  # the filing is under test, not the card lint
        try:
            for name in CARD_NAMES:
                AD.file_issue(self.cards[name])
        finally:
            AD.gh, AD.preflight_card = original, original_lint

        self.assertEqual(len(created), COUNT,
                         "the dispatcher filed %d issues for %d cards" % (len(created), COUNT))
        for name, args in zip(CARD_NAMES, created):
            self.assertEqual(args[:2], ["issue", "create"], "%s: unexpected gh call" % name)
            title = args[args.index("-t") + 1]
            self.assertEqual(title, card_title(self.cards[name]),
                             "%s: the filed title must be the card title" % name)
            body = args[args.index("-b") + 1]
            self.assertTrue(body.strip(), "%s: the filed issue needs a body" % name)
            labels = [args[index + 1] for index, arg in enumerate(args) if arg == "-l"]
            self.assertIn("lane-ready", labels, "%s: the filed issue must be lane-ready" % name)
            for label in card_labels(self.cards[name]):
                self.assertIn(label, labels, "%s: the label %r was dropped" % (name, label))


class BatchMixTests(BatchCase):
    """The announced mix and the no-duplicate rule of the batch."""

    def test_backend_mix_keeps_every_lane_fed(self):
        self.require_batch()
        counts = {"pi": 0, "agy": 0, "codex": 0, None: 0}
        for text in self.cards.values():
            backends = [label.split(":", 1)[1] for label in card_labels(text)
                        if label.startswith("backend:")]
            counts[backends[0] if backends else None] += 1
        for backend, (low, high) in BACKEND_RANGE.items():
            self.assertGreaterEqual(counts[backend], low,
                                    "too few %s cards: %s" % (backend, counts))
            self.assertLessEqual(counts[backend], high,
                                 "too many %s cards: %s" % (backend, counts))
        self.assertGreaterEqual(counts[None], 3,
                                "at least three cards must carry no backend: %s" % counts)

    def test_titles_are_unique_and_do_not_repeat_an_existing_card(self):
        self.require_batch()
        titles = []
        for name, text in self.cards.items():
            titles.append((" ".join(card_title(text).split()).lower(), name))
        self.assertEqual(len({title for title, _ in titles}), COUNT,
                         "the batch repeats a title: %s" % titles)
        known = existing_titles()
        for title, name in titles:
            self.assertNotIn(title, known,
                             "%s repeats the title of an existing backlog card" % name)


if __name__ == "__main__":
    unittest.main()
