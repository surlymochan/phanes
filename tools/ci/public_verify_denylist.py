"""Private denylist interface of the public-verify scanner (Refs #2034).

The denylist itself never lives in this repository: it is injected at runtime,
either through the ``PUBLIC_VERIFY_DENYLIST`` environment variable or through a
file passed with ``--denylist``.  The format is one rule per line::

    category<TAB>term
    category<TAB>re:regex

``category`` is one of ``host``, ``domain``, ``bus``, ``account`` and
``holdout``.  Plain terms are matched case-insensitively on word boundaries,
``re:`` rules are regular expressions and are matched case-insensitively as
well.  Blank lines and lines starting with ``#`` are ignored.

Every rule matches on word boundaries, regular expressions included: a match
that touches a neighbouring word character is not a finding.  The boundary is
only required on a side where the matched text itself ends with a word
character, so that a suffix such as ``.internal`` still matches inside a
longer name.

The scanner is fail-closed: a missing or empty denylist raises
``DenylistError`` (exit code 2) and only an explicit ``generic_only`` request
skips the private denylist.

A finding never carries the matched text.  It holds the category of the rule,
the position of the match and the first eight hex digits of the hash of the
matched text.  A configuration error never repeats the denylist content
either, it names the source and the line of the offending rule only.

This module is internal to the scanner, it is not a public API.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path


CATEGORIES = ("host", "domain", "bus", "account", "holdout")
DENYLIST_ENV = "PUBLIC_VERIFY_DENYLIST"
REGEX_PREFIX = "re:"
CONFIG_ERROR_EXIT_CODE = 2
HASH_LENGTH = 8

__all__ = [
    "CATEGORIES",
    "CONFIG_ERROR_EXIT_CODE",
    "DENYLIST_ENV",
    "DenylistError",
    "Hit",
    "Matcher",
    "Rule",
    "load",
    "parse",
]


class DenylistError(Exception):
    """Configuration error of the private denylist, the scanner fails closed.

    ``exit_code`` is the exit code the scanner uses for a configuration error.
    """

    exit_code = CONFIG_ERROR_EXIT_CODE


@dataclass(frozen=True)
class Rule:
    """A single denylist rule.

    The rule keeps no term of its own: the private text lives in the pattern
    only and never leaves the matcher.
    """

    category: str
    pattern: "re.Pattern"
    is_regex: bool


@dataclass(frozen=True)
class Hit:
    """A finding: category, position and hash prefix, never the matched text."""

    category: str
    start: int
    end: int
    line: int
    column: int
    hash8: str

    def as_dict(self):
        """The finding as data, without the matched text."""
        return {
            "category": self.category,
            "location": {"offset": self.start, "line": self.line, "column": self.column},
            "hash8": self.hash8,
        }

    def render(self):
        """The finding as one line of text, without the matched text."""
        return f"{self.category}\t{self.line}:{self.column}\t{self.hash8}"


class Matcher:
    """Matches text against the private denylist."""

    def __init__(self, rules=(), source="denylist"):
        self.rules = tuple(rules)
        self.source = source

    def __len__(self):
        return len(self.rules)

    def __bool__(self):
        return bool(self.rules)

    def scan(self, text):
        """Return the findings of ``text``, ordered by position."""
        hits = []
        seen = set()
        for rule in self.rules:
            for match in rule.pattern.finditer(text):
                matched = match.group(0)
                if not matched:
                    continue
                start, end = match.span()
                if not _on_word_boundary(text, start, end):
                    continue
                key = (rule.category, start, end)
                if key in seen:
                    continue
                seen.add(key)
                line, column = _position(text, start)
                hits.append(Hit(rule.category, start, end, line, column, _hash8(matched)))
        hits.sort(key=lambda hit: (hit.start, hit.category))
        return hits


def load(env=None, denylist_path=None, generic_only=False, denylist=None):
    """Build a matcher from the private denylist.

    env: mapping holding ``PUBLIC_VERIFY_DENYLIST``, defaults to ``os.environ``.
    denylist_path: path of a denylist file, it takes precedence over ``env``.
    generic_only: skip the private denylist entirely.
    denylist: alias of ``denylist_path``.

    Raises ``DenylistError`` when the denylist is missing, empty or malformed.
    """
    if generic_only:
        return Matcher((), source="generic-only")
    if denylist_path is None and denylist is not None:
        denylist_path = denylist
    if denylist_path is not None:
        content = _read_file(denylist_path)
        source = str(denylist_path)
    else:
        content, source = _from_env(env)
    rules = parse(content, source=source)
    if not rules:
        raise DenylistError(f"{source}: denylist is empty")
    return Matcher(rules, source=source)


def parse(content, source="denylist"):
    """Parse the denylist content into rules.

    Raises ``DenylistError`` for a malformed line.  The error never repeats
    the content of the denylist.
    """
    rules = []
    for number, raw in enumerate(str(content).splitlines(), 1):
        line = raw.rstrip("\r")
        if not line.strip() or line.strip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2:
            raise DenylistError(f"{source}:{number}: expected 'category<TAB>term'")
        category = fields[0].strip().lower()
        term = fields[1].strip()
        if category not in CATEGORIES:
            raise DenylistError(f"{source}:{number}: unknown category")
        if not term:
            raise DenylistError(f"{source}:{number}: empty term")
        if term.startswith(REGEX_PREFIX):
            expression = term[len(REGEX_PREFIX):]
            if not expression:
                raise DenylistError(f"{source}:{number}: empty regular expression")
            try:
                pattern = re.compile(expression, re.IGNORECASE)
            except re.error:
                # The message of re.error quotes the pattern, so it must not
                # be chained into the traceback of the configuration error.
                raise DenylistError(f"{source}:{number}: invalid regular expression") from None
            rules.append(Rule(category, pattern, True))
        else:
            rules.append(Rule(category, re.compile(re.escape(term), re.IGNORECASE), False))
    return rules


def _from_env(env):
    """Read the denylist content from the environment mapping."""
    if env is None:
        env = os.environ
    if not hasattr(env, "get"):
        raise DenylistError(f"env must be a mapping holding {DENYLIST_ENV}")
    value = env.get(DENYLIST_ENV)
    if value is None:
        raise DenylistError(f"denylist missing: set {DENYLIST_ENV} or pass --denylist FILE")
    return value, DENYLIST_ENV


def _read_file(path):
    """Read the denylist file."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeError:
        raise DenylistError(f"denylist file is not valid UTF-8: {path}")
    except OSError as exc:
        raise DenylistError(f"denylist file cannot be read: {path} ({exc.strerror or exc})")


def _on_word_boundary(text, start, end):
    """Return True when the match touches no neighbouring word character.

    A boundary is only required on a side where the matched text itself ends
    with a word character, so that a suffix such as ``.internal`` still
    matches inside a longer name.
    """
    if start > 0 and _is_word_char(text[start]) and _is_word_char(text[start - 1]):
        return False
    if end < len(text) and _is_word_char(text[end - 1]) and _is_word_char(text[end]):
        return False
    return True


def _is_word_char(char):
    return bool(re.match(r"\w", char))


def _position(text, offset):
    """Return the one-based line and column of ``offset`` in ``text``."""
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset) + 1
    return line, offset - line_start + 1


def _hash8(matched):
    """Return the first eight hex digits of the hash of the matched text."""
    return hashlib.sha256(matched.encode("utf-8")).hexdigest()[:HASH_LENGTH]
