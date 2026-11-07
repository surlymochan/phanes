"""Acceptance mapping: the card's acceptance items and the PR's evidence table (Refs #1730).

A card says what has to hold; a pull request has to say what proves it.  The two are
connected by the *acceptance mapping*: a numbered table in the PR body where every
acceptance item of the card gets exactly one line, and every line carries the evidence
that proves the item (a test name plus its result, or the deciding command output).

Three rules hold everywhere in this module:

* **The card is the authority.**  The numbering of the mapping never replaces the
  numbering of the card: a mapping that renumbers or rewords the items leaves the card
  item uncovered instead of redefining it.
* **Evidence is what counts.**  A mapping line without evidence covers nothing; the
  line only claims something.
* **Old PRs keep working.**  A PR without a mapping section is not a broken PR, it is
  an old one: the caller takes the compatibility path and only hints at the gap.

The module is pure - it parses and compares texts, it never talks to GitHub and never
writes anything.  The dispatch engine owns the wiring (see ``lib/dispatch/engine.py``).
"""
import re

# The PR body section that carries the mapping.
SECTION_TITLE = "验收映射"
SECTION_TITLE_EN = "Acceptance mapping"
# The marker the dispatcher writes into every PR body it creates.  A PR body without
# the marker was written before the mapping existed and takes the compatibility path.
MARKER = "<!-- lane-acceptance-map -->"

# A card states its acceptance either as a prefixed line or as a section with bullets.
ACCEPTANCE_PREFIXES = ("验收：", "验收:", "acceptance:", "Acceptance:")
ACCEPTANCE_HEADINGS = ("验收", "验收标准", "验收条件", "acceptance", "acceptance criteria")
# A mapping line separates the acceptance item from its evidence with one of these.
EVIDENCE_MARKERS = ("证据：", "证据:", "evidence:", "Evidence:")

# One mapping line: an optional bullet, the number, the item and its evidence.
ENTRY_RE = re.compile(r"^\s*(?:[-*+]\s*)?(\d+)\s*[.、)．]\s*(.+?)\s*$")
# A markdown heading, used to find sections in a card or a PR body.
HEADING_RE = re.compile(r"^\s*(#{1,6})\s*(.+?)\s*$")

# Two texts count as the same thing when they share this share of their bigrams.
SIMILAR_THRESHOLD = 0.45


def normalize(text):
    """Return the text reduced to what it says: no markdown noise, no case, single spaces."""
    text = str(text or "").strip().lower()
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _bigrams(text):
    text = re.sub(r"\s+", "", text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def similar(left, right, threshold=SIMILAR_THRESHOLD):
    """Return True when two texts say the same thing.

    The comparison is deliberately loose: the mapping line may quote the card item,
    shorten it or wrap it in a sentence.  Only texts that share almost nothing are
    treated as different, because a mapping that talks about another item must not
    pass as a proof of this one.
    """
    first, second = normalize(left), normalize(right)
    if not first or not second:
        return False
    if first == second or first in second or second in first:
        return True
    left_grams, right_grams = _bigrams(first), _bigrams(second)
    if not left_grams or not right_grams:
        return False
    shared = len(left_grams & right_grams)
    return 2.0 * shared / float(len(left_grams) + len(right_grams)) >= threshold


def card_items(text):
    """Return the acceptance items of a card, numbered from 1 in document order.

    An item is a line that starts with an acceptance prefix (``验收：``) or a bullet
    inside an acceptance section (``## 验收``).  A card that has an acceptance
    section takes its items from that section: the trailing ``验收：`` line of such a
    card is the verify command, not an item of its own.  The card is the authority for
    both the wording and the numbering of the items.
    """
    prefixed, listed = [], []
    in_section = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = HEADING_RE.match(line)
        if heading:
            in_section = heading.group(2).strip().lower() in ACCEPTANCE_HEADINGS
            continue
        matched = False
        for prefix in ACCEPTANCE_PREFIXES:
            if line.lower().startswith(prefix.lower()):
                value = line[len(prefix):].strip()
                if value:
                    prefixed.append(value)
                else:
                    in_section = True
                matched = True
                break
        if matched or not in_section:
            continue
        bullet = re.sub(r"^\s*(?:[-*+]|\d+[.、)．])\s*", "", line).strip()
        if bullet:
            listed.append(bullet)
    items = listed or prefixed
    return [{"number": index, "text": text} for index, text in enumerate(items, start=1)]


def parse_entries(text):
    """Return the mapping entries of a mapping text.

    Every entry is one line ``<number>. <item> 证据：<evidence>``.  The evidence is
    what makes an entry usable: an entry without evidence covers nothing, it only
    claims something.
    """
    entries = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        match = ENTRY_RE.match(line)
        if not match:
            continue
        rest = match.group(2).strip()
        item, evidence = rest, ""
        for marker in EVIDENCE_MARKERS:
            index = rest.lower().find(marker.lower())
            if index >= 0:
                item = rest[:index].strip()
                evidence = rest[index + len(marker):].strip()
                break
        entries.append({"number": int(match.group(1)), "text": item, "evidence": evidence})
    return entries


def section(body, title=SECTION_TITLE):
    """Return the text of the ``## <title>`` section of a markdown body.

    ``None`` means the body has no such section at all - that is the marker of an old
    PR, not of an empty mapping.
    """
    lines = str(body or "").splitlines()
    start = None
    level = 0
    for index, line in enumerate(lines):
        heading = HEADING_RE.match(line.strip())
        if not heading:
            continue
        if start is None:
            names = (str(title or SECTION_TITLE).strip().lower(), SECTION_TITLE_EN.lower())
            if heading.group(2).strip().lower() in names:
                start, level = index + 1, len(heading.group(1))
        elif len(heading.group(1)) <= level:
            return "\n".join(lines[start:index]).strip("\n")
    if start is None:
        return None
    return "\n".join(lines[start:]).strip("\n")


def has_marker(body):
    """Return True when the body carries the marker of a dispatcher written PR."""
    return MARKER in str(body or "")


def render(entries):
    """Render mapping entries as the lines of the PR body section."""
    lines = []
    for entry in entries or ():
        evidence = (entry.get("evidence") or "").strip()
        lines.append("{number}. {text} 证据：{evidence}".format(number=entry.get("number"),
                                                              text=(entry.get("text") or "").strip(),
                                                              evidence=evidence).rstrip())
    return "\n".join(lines)


def coverage(items, entries):
    """Return which acceptance items of the card the mapping covers.

    The card is the authority: an entry covers the card item with the same number
    only when it carries evidence and its text still talks about that item.  A
    mapping that renumbers the items or points them at something else therefore
    leaves the card item uncovered instead of replacing it.

    The result carries the covered numbers, the uncovered ones and the ones whose
    text does not match the card (the mismatch is the reason they are uncovered).
    """
    by_number = {}
    for entry in entries or ():
        by_number.setdefault(entry.get("number"), entry)
    covered, missing, mismatched = [], [], []
    for item in items or ():
        number = item.get("number")
        entry = by_number.get(number)
        if not entry or not (entry.get("evidence") or "").strip():
            missing.append(number)
            continue
        text = (entry.get("text") or "").strip()
        if text and not similar(text, item.get("text")):
            mismatched.append(number)
            missing.append(number)
            continue
        covered.append(number)
    return {"covered": covered, "missing": missing, "mismatched": mismatched}


def prompt_block(items, entries, missing=(), legacy=False):
    """Return the review prompt block that carries the mapping table.

    The block lists the acceptance items of the card with their numbers, the mapping
    the PR claims, and the items the mapping does not cover.  The reviewer is told to
    verify every item, to cite the item number in every finding and to mark an
    uncovered item as BLOCKER.
    """
    if legacy:
        return ("### 验收映射\n\n"
                "该 PR 没有「验收映射」段（老 PR，兼容路径）：无法逐条核验。"
                "请在结论里说明这一限制，不要臆造证据。")
    lines = ["### 验收映射（逐条核验）", "", "卡面验收项（编号以卡面为准）：", ""]
    if items:
        for item in items:
            lines.append("{number}. {text}".format(number=item.get("number"), text=item.get("text")))
    else:
        lines.append("（卡面没有验收项）")
    lines += ["", "PR 的验收映射：", ""]
    if entries:
        lines.append(render(entries))
    else:
        lines.append("（PR 没有给出映射）")
    if missing:
        lines += ["", "未覆盖的验收项：" + "、".join("A{n}".format(n=n) for n in missing) +
                  " —— 必须标 BLOCKER，结论只能是 CHANGES。"]
    lines += ["", "核验要求：",
              "- 逐条核验上面的验收项：证据里的测试名必须真实存在，结果必须与证据一致；抽查任意一条证据。",
              "- 每条发现都要引用验收编号：`[A1] file:line 说明`（A1 = 第 1 条验收项）。",
              "- 未覆盖的验收项必须标 BLOCKER，并把结论写成 CHANGES。",
              "- 证据与实际测试不符（伪造）一律按 BLOCKER 处理。"]
    return "\n".join(lines)


__all__ = [
    "ACCEPTANCE_HEADINGS",
    "ACCEPTANCE_PREFIXES",
    "EVIDENCE_MARKERS",
    "MARKER",
    "SECTION_TITLE",
    "SECTION_TITLE_EN",
    "card_items",
    "coverage",
    "has_marker",
    "normalize",
    "parse_entries",
    "prompt_block",
    "render",
    "section",
    "similar",
]
