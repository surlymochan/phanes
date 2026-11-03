"""The raw evidence pack of a lane review (Refs #1788, Epic #1785 P1-2).

A lane that reviews a PR used to collect the evidence *and* to declare the verdict in
one free-text file.  Doing both in one place is one of the root causes of judgement
drift, so the review lane now delivers **raw evidence** in a fixed, machine readable
format and the judgement belongs to the tester (the lane verdict keeps running in
parallel during the shadow period and dies when the tester takes over).

The pack is a markdown file with five fixed sections::

    # Evidence pack — PR #123

    ## 1. CI results (CI 结论逐项)
    CI: OpenSwarm tests | pass | https://github.com/acme/project/runs/1

    ## 2. Test log summary and links (测试日志摘要与链接)
    TEST: python -m unittest discover -s tests | 42 tests, all passed | runs/42.log
    LOG: Ran 42 tests in 1.234s
    LOG: OK

    ## 3. Diff statistics (diff 统计)
    DIFFSTAT: files=3 | insertions=120 | deletions=12 | effective_lines=95
    RULE: effective_lines = added + deleted lines of code and test files, docs excluded

    ## 4. Acceptance checks (验收项逐条原始核查)
    CHECK: acceptance item 1 | pass | `python -m unittest` → Ran 42 tests ... OK

    ## 5. Preliminary opinion (初步意见 · 参考，不算判定)
    OPINION: looks fine to me

The four evidence sections are required; the fifth (the preliminary opinion of the
lane) is optional.  Two rules keep the pack honest:

* the pack carries **no verdict**: a ``VERDICT:`` line anywhere in it is refused, so
  an opinion can never masquerade as a conclusion;
* the opinion section is marked ``参考，不算判定`` (reference only, not a judgement);
  an unmarked opinion section is refused as well.

``parse_evidence(text)`` returns the structured fields of a pack, ``build_evidence(...)``
assembles one, ``missing_sections(text)``/``is_complete(text)`` answer the format
question the dispatcher asks in ``finish_review``: an incomplete pack is *recorded*
(missing sections and errors), never silently accepted.

The module is loaded by path: ``openswarm/lib/review/`` is a namespace package without
an ``__init__.py``, so it must not be imported as a package.  Standard library only,
Python 3.9+.
"""

import re

# ---------------------------------------------------------------- the format

# The five sections of a pack.  `aliases` are the headings a lane may use (the
# canonical title first, the Chinese wording of the card as an alias); a heading
# matches a section when it *starts with* one of its aliases.
SECTIONS = (
    {"key": "ci", "number": 1, "title": "CI results (CI 结论逐项)",
     "aliases": ("ci results", "ci 结论", "ci结论"), "required": True},
    {"key": "testlog", "number": 2, "title": "Test log summary and links (测试日志摘要与链接)",
     "aliases": ("test log", "测试日志"), "required": True},
    {"key": "diffstat", "number": 3, "title": "Diff statistics (diff 统计)",
     "aliases": ("diff stat", "diffstat", "diff 统计"), "required": True},
    {"key": "checks", "number": 4, "title": "Acceptance checks (验收项逐条原始核查)",
     "aliases": ("acceptance check", "验收项"), "required": True},
    {"key": "opinion", "number": 5, "title": "Preliminary opinion (初步意见 · 参考，不算判定)",
     "aliases": ("preliminary opinion", "初步意见"), "required": False},
)

SECTION_BY_KEY = {section["key"]: section for section in SECTIONS}
REQUIRED_KEYS = tuple(s["key"] for s in SECTIONS if s["required"])
OPTIONAL_KEYS = tuple(s["key"] for s in SECTIONS if not s["required"])

# The marker the preliminary opinion has to carry: the opinion is a reference, never
# a judgement.
OPINION_MARKERS = ("参考", "不算判定")
OPINION_MARKERS_EN = ("reference only", "not a verdict")

# The verdict line of the old review format.  It must not appear in a pack.
VERDICT_LINE_RE = re.compile(r"^\s*(?:VERDICT|判定|结论)\s*[:：]", re.I)

# The effective line rule of the diff statistics: the same rule the thin-PR gate uses
# (code and test files count, documentation does not).
EFFECTIVE_RULE = ("effective_lines = added + deleted lines of code and test files; "
                  "documentation (.md/.txt/.rst/.adoc, docs/) is excluded "
                  "(the rule of the thin-PR gate)")

# The number of the effective line metric of the fleet.
DEFAULT_DOC_EXT = (".md", ".txt", ".rst", ".adoc")
DEFAULT_CODE_EXT = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".go", ".rs", ".sh")
DEFAULT_CODE_DIRS = ("tests/", "tools/", "bin/")

# The result of one acceptance check: pass, fail or not checked at all.
CHECK_RESULTS = ("pass", "fail", "unchecked")
_RESULT_ALIASES = {
    "pass": "pass", "passed": "pass", "通过": "pass", "符合": "pass",
    "fail": "fail", "failed": "fail", "不通过": "fail", "未通过": "fail", "不符合": "fail",
    "未查": "unchecked", "未核查": "unchecked", "未检查": "unchecked", "unchecked": "unchecked",
    "not checked": "unchecked", "not-checked": "unchecked", "skipped": "unchecked",
}

# The conclusion of one CI check (the raw one, not the lane's opinion about it).
CI_STATUSES = ("pass", "fail", "pending", "skipped", "neutral", "cancelled", "unknown")
_CI_ALIASES = {
    "success": "pass", "passed": "pass", "green": "pass", "ok": "pass",
    "failure": "fail", "failed": "fail", "red": "fail",
    "queued": "pending", "in_progress": "pending", "waiting": "pending",
    "cancelled": "cancelled", "timed_out": "fail", "action_required": "fail",
}

_HEADING_RE = re.compile(r"^\s{0,3}#{2,4}\s+(\S.*?)\s*$")
_NUMBER_PREFIX_RE = re.compile(r"^\(?\d+\s*[.)、:]\s*")
_FIELD_SPLIT_RE = re.compile(r"\s*\|\s*")
_DASH_SPLIT_RE = re.compile(r"\s+[—–]\s+")
_INT_RE = re.compile(r"^-?\d+$")


def heading_text(line):
    """Return the heading of a markdown line, or None when the line is not a heading."""
    match = _HEADING_RE.match(str(line or ""))
    return match.group(1) if match else None


def match_section(heading):
    """Return the key of the section a heading belongs to, or None."""
    text = _NUMBER_PREFIX_RE.sub("", str(heading or "")).strip().lower()
    for section in SECTIONS:
        for alias in section["aliases"]:
            if text.startswith(alias):
                return section["key"]
    return None


def field_value(line, marker):
    """Return the value of a ``MARKER: value`` line, or None when the line is no such field."""
    text = str(line or "").strip()
    if not text.lower().startswith(marker.lower() + ":"):
        return None
    return text[len(marker) + 1:].strip()


def split_fields(value, maxsplit=None):
    """Split a field value into its parts (``|`` separated, a dash is the fallback).

    ``maxsplit`` bounds the number of splits: with ``maxsplit=2`` the third part keeps
    the rest of the text verbatim.  The raw evidence of a lane may contain a ``|``
    itself (a pipe in a command, a table in a log line) and the parser must not cut it
    into pieces.
    """
    text = str(value or "").strip()
    if not text:
        return []
    if maxsplit is None:
        parts = [part.strip() for part in _FIELD_SPLIT_RE.split(text)]
        if len(parts) == 1:
            parts = [part.strip() for part in _DASH_SPLIT_RE.split(text)]
    else:
        parts = [part.strip() for part in _FIELD_SPLIT_RE.split(text, maxsplit)]
        if len(parts) == 1:
            parts = [part.strip() for part in _DASH_SPLIT_RE.split(text, maxsplit)]
    return [part for part in parts if part != ""] or [text]


def normalize_status(value):
    """Return the CI conclusion of one check (lowercase, known aliases folded)."""
    text = str(value or "").strip().lower()
    return _CI_ALIASES.get(text, text)


def normalize_result(value):
    """Return the result of one acceptance check: ``pass``, ``fail`` or ``unchecked``."""
    text = str(value or "").strip().lower()
    return _RESULT_ALIASES.get(text, text)


def to_int(value):
    """Return the integer of a field, or None when it is not a number."""
    text = str(value or "").strip()
    return int(text) if _INT_RE.match(text) else None


def opinion_marked(heading):
    """Return True when the opinion heading carries the 「参考，不算判定」 marker."""
    text = str(heading or "").lower()
    if any(marker in text for marker in OPINION_MARKERS):
        return True
    return any(marker in text for marker in OPINION_MARKERS_EN)


# ---------------------------------------------------------------- the diff statistics
def compute_diffstat(numstat, doc_ext=DEFAULT_DOC_EXT, code_ext=DEFAULT_CODE_EXT,
                     code_dirs=DEFAULT_CODE_DIRS):
    """Return the diff statistics of a ``git diff --numstat`` text.

    The result holds the number of files, the added and the deleted lines and the
    *effective* lines: the added and deleted lines of code and test files, the same
    rule the thin-PR gate uses (documentation does not count).
    """
    files = insertions = deletions = effective = docs = other = 0
    for line in str(numstat or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        try:
            a = 0 if added == "-" else int(added)
            d = 0 if deleted == "-" else int(deleted)
        except ValueError:
            continue
        files += 1
        insertions += a
        deletions += d
        low = path.lower()
        if low.endswith(tuple(doc_ext)) or "/docs/" in low or low.startswith("docs/"):
            docs += a + d
        elif low.endswith(tuple(code_ext)) or any(("/" + cd) in low or low.startswith(cd)
                                                  for cd in code_dirs):
            effective += a + d
        else:
            other += a + d
    return {"files": files, "insertions": insertions, "deletions": deletions,
            "effective_lines": effective, "docs_lines": docs, "other_lines": other}


# ---------------------------------------------------------------- the pack
def build_evidence(pr, sha=None, ci=None, test_logs=None, log_lines=None, diffstat=None,
                   checks=None, opinion=None):
    """Assemble one evidence pack from structured fields.

    A section whose data is ``None`` is left out of the pack (the pack is then
    *missing* that section); a section with an empty list is written without any item
    (the pack is then *present but empty*).  The parser tells the two defects apart.
    """
    out = ["# Evidence pack — PR %s" % pr]
    if sha:
        out += ["", "Head sha: `%s`" % sha]
    if ci is not None:
        out += ["", "## %d. %s" % (SECTION_BY_KEY["ci"]["number"], SECTION_BY_KEY["ci"]["title"])]
        out += ["CI: " + _ci_field(item) for item in ci]
    if test_logs is not None:
        out += ["", "## %d. %s" % (SECTION_BY_KEY["testlog"]["number"],
                                   SECTION_BY_KEY["testlog"]["title"])]
        out += ["TEST: " + _test_field(item) for item in test_logs]
        out += ["LOG: " + str(line).strip() for line in (log_lines or [])]
    if diffstat is not None:
        out += ["", "## %d. %s" % (SECTION_BY_KEY["diffstat"]["number"],
                                   SECTION_BY_KEY["diffstat"]["title"])]
        out += ["DIFFSTAT: " + _diffstat_field(diffstat)]
        out += ["RULE: " + EFFECTIVE_RULE]
    if checks is not None:
        out += ["", "## %d. %s" % (SECTION_BY_KEY["checks"]["number"],
                                   SECTION_BY_KEY["checks"]["title"])]
        out += ["CHECK: " + _check_field(item) for item in checks]
    if opinion is not None:
        out += ["", "## %d. %s" % (SECTION_BY_KEY["opinion"]["number"],
                                   SECTION_BY_KEY["opinion"]["title"])]
        out += ["OPINION: " + str(opinion).strip()]
    return "\n".join(out) + "\n"


def _ci_field(item):
    """Return the field text of one CI item."""
    if isinstance(item, dict):
        parts = [str(item.get("name") or "").strip(), str(item.get("status") or "").strip(),
                 str(item.get("detail") or item.get("url") or "").strip()]
        return " | ".join(part for part in parts if part)
    return str(item)


def _test_field(item):
    """Return the field text of one test log item."""
    if isinstance(item, dict):
        parts = [str(item.get("command") or "").strip(), str(item.get("summary") or "").strip(),
                 str(item.get("link") or item.get("url") or "").strip()]
        return " | ".join(part for part in parts if part)
    return str(item)


def _check_field(item):
    """Return the field text of one acceptance check."""
    if isinstance(item, dict):
        parts = [str(item.get("item") or item.get("name") or "").strip(),
                 str(item.get("result") or "").strip(), str(item.get("evidence") or "").strip()]
        return " | ".join(part for part in parts if part)
    return str(item)


def _diffstat_field(diffstat):
    """Return the field text of the diff statistics."""
    if not isinstance(diffstat, dict):
        return str(diffstat)
    parts = []
    for key in ("files", "insertions", "deletions", "effective_lines"):
        if key in diffstat:
            parts.append("%s=%s" % (key, diffstat[key]))
    return " | ".join(parts)


def parse_evidence(text):
    """Return the structured fields of one evidence pack.

    The result holds the sections that are present, the required sections that are
    missing, the format errors and the parsed fields (``ci``, ``test_logs``,
    ``log_lines``, ``diffstat``, ``checks``, ``opinion``).  ``complete`` is True when
    no required section is missing and no error was found.
    """
    lines = str(text or "").splitlines()
    errors = []
    bodies = {}
    headings = {}
    order = []
    current = None
    for line in lines:
        heading = heading_text(line)
        if heading is not None:
            key = match_section(heading)
            if key is None:
                current = None          # an unknown section: its lines belong to nobody
                continue
            if key in bodies:
                errors.append("section-duplicated")
            current = key
            headings[key] = heading
            if key not in bodies:
                bodies[key] = []
                order.append(key)
            continue
        if current is not None:
            bodies[current].append(line)

    missing = [s["key"] for s in SECTIONS if s["required"] and s["key"] not in bodies]

    ci = []
    for line in bodies.get("ci", []):
        value = field_value(line, "CI")
        if value is None:
            continue
        parts = split_fields(value, maxsplit=2)
        ci.append({"name": parts[0] if parts else "",
                   "status": normalize_status(parts[1] if len(parts) > 1 else ""),
                   "detail": parts[2] if len(parts) > 2 else ""})
    if "ci" in bodies and not ci:
        errors.append("ci-empty")

    test_logs = []
    log_lines = []
    for line in bodies.get("testlog", []):
        value = field_value(line, "TEST")
        if value is not None:
            parts = split_fields(value, maxsplit=2)
            test_logs.append({"command": parts[0] if parts else "",
                               "summary": parts[1] if len(parts) > 1 else "",
                               "link": parts[2] if len(parts) > 2 else ""})
            continue
        value = field_value(line, "LOG")
        if value is not None:
            log_lines.append(value)
    if "testlog" in bodies:
        if not test_logs:
            errors.append("testlog-empty")
        else:
            if any(not log["link"].strip() for log in test_logs):
                errors.append("testlog-no-link")
            if not log_lines:
                errors.append("testlog-no-raw")

    diffstat = {}
    rule = ""
    for line in bodies.get("diffstat", []):
        value = field_value(line, "DIFFSTAT")
        if value is not None:
            for part in split_fields(value):
                key, _, val = part.partition("=")
                if key.strip():
                    diffstat[key.strip().lower()] = val.strip()
            continue
        value = field_value(line, "RULE")
        if value is not None:
            rule = rule or value
            continue
        if "effective" in line.lower() and not rule:
            rule = line.strip()
    if "diffstat" in bodies:
        if not diffstat:
            errors.append("diffstat-missing")
        else:
            absent = [key for key in ("files", "insertions", "deletions", "effective_lines")
                      if key not in diffstat]
            if absent:
                errors.append("diffstat-incomplete")
            elif not rule:
                errors.append("diffstat-no-rule")
    for key in ("files", "insertions", "deletions", "effective_lines"):
        if key in diffstat:
            diffstat[key] = to_int(diffstat[key])

    checks = []
    for line in bodies.get("checks", []):
        value = field_value(line, "CHECK")
        if value is None:
            continue
        parts = split_fields(value, maxsplit=2)
        checks.append({"item": parts[0] if parts else "",
                       "result": normalize_result(parts[1] if len(parts) > 1 else ""),
                       "evidence": parts[2] if len(parts) > 2 else ""})
    if "checks" in bodies:
        if not checks:
            errors.append("checks-empty")
        else:
            if any(not check["evidence"].strip() for check in checks):
                errors.append("check-no-evidence")
            if any(check["result"] not in CHECK_RESULTS for check in checks):
                errors.append("check-bad-result")

    opinion = {"present": "opinion" in bodies, "marked": False, "text": ""}
    if opinion["present"]:
        opinion["marked"] = opinion_marked(headings.get("opinion", ""))
        if not opinion["marked"]:
            errors.append("opinion-not-marked")
        texts = []
        for line in bodies["opinion"]:
            value = field_value(line, "OPINION")
            texts.append(value if value is not None else line.strip())
        opinion["text"] = "\n".join(text for text in texts if text)

    for line in lines:
        if VERDICT_LINE_RE.match(line):
            errors.append("verdict-in-pack")
            break

    return {"sections": order, "missing": missing, "errors": errors,
            "complete": not missing and not errors,
            "ci": ci, "test_logs": test_logs, "log_lines": log_lines,
            "diffstat": diffstat, "diffstat_rule": rule, "checks": checks, "opinion": opinion}


def missing_sections(text):
    """Return the required sections a pack does not carry."""
    return parse_evidence(text)["missing"]


def is_complete(text):
    """Return True when a pack carries every required section and no format error."""
    return parse_evidence(text)["complete"]


def render_spec(pr=None, path=None):
    """Return the format specification the review task hands to the lane."""
    head = "PR #%s" % pr if pr is not None else "PR #<n>"
    target = path or "<the evidence file>"
    return "\n".join([
        "Write the file `%s` with EXACTLY these five sections, in this order." % target,
        "The headings and the `KEY:` markers are fixed: the parser reads them and nothing else.",
        "",
        "# Evidence pack — %s" % head,
        "",
        "## %d. %s" % (SECTION_BY_KEY["ci"]["number"], SECTION_BY_KEY["ci"]["title"]),
        "CI: <check name> | <pass|fail|pending|skipped|unknown> | <run URL or note>",
        "  one CI: line per check of the PR — the raw conclusion, not your judgement",
        "",
        "## %d. %s" % (SECTION_BY_KEY["testlog"]["number"], SECTION_BY_KEY["testlog"]["title"]),
        "TEST: <command> | <summary: counts and outcome> | <log URL or path>",
        "LOG: <one raw line of the log, copied verbatim>",
        "  the summary AND the raw lines: a conclusion without the raw log is not accepted",
        "",
        "## %d. %s" % (SECTION_BY_KEY["diffstat"]["number"], SECTION_BY_KEY["diffstat"]["title"]),
        "DIFFSTAT: files=<n> | insertions=<n> | deletions=<n> | effective_lines=<n>",
        "RULE: %s" % EFFECTIVE_RULE,
        "",
        "## %d. %s" % (SECTION_BY_KEY["checks"]["number"], SECTION_BY_KEY["checks"]["title"]),
        "CHECK: <acceptance item> | <pass|fail|未查> | <raw evidence: the command and its output>",
        "  one CHECK: line per acceptance item of the issue; 未查 when you could not check it",
        "",
        "## %d. %s" % (SECTION_BY_KEY["opinion"]["number"], SECTION_BY_KEY["opinion"]["title"]),
        "OPINION: <optional, your own opinion>",
        "",
        "Rules:",
        "- the pack is raw evidence: no `VERDICT:` line anywhere, the judgement belongs to the tester.",
        "- section 5 is optional; when present it must carry the 「参考，不算判定」 marker.",
        "- write nothing else: the parser reads these sections and nothing else.",
    ])


__all__ = [
    "CHECK_RESULTS",
    "CI_STATUSES",
    "DEFAULT_CODE_DIRS",
    "DEFAULT_CODE_EXT",
    "DEFAULT_DOC_EXT",
    "EFFECTIVE_RULE",
    "OPINION_MARKERS",
    "OPTIONAL_KEYS",
    "REQUIRED_KEYS",
    "SECTION_BY_KEY",
    "SECTIONS",
    "build_evidence",
    "compute_diffstat",
    "field_value",
    "is_complete",
    "match_section",
    "missing_sections",
    "normalize_result",
    "normalize_status",
    "opinion_marked",
    "parse_evidence",
    "render_spec",
    "split_fields",
]
