"""Session context compaction for long runs (Refs #1716).

The fleet now budgets 90-120 minutes per run, so one conversation stays alive for the whole
budget. Every tool call grows that conversation, and a session that keeps every message and
every tool output eventually reaches the model window - or makes every request so large that
the answer arrives too late to be useful.

This module is the executor side of that problem. The executor keeps a normalized record of
the session it drives (:class:`SessionContext`) and, once that record crosses the configured
threshold, the early part of the session is replaced by a summary (:func:`compact_context`).
The summary keeps what a later turn still needs:

* the decisions the session took,
* the files the session changed,
* the todos that are still open,

and drops the repeated tool output that carries no new information. The numbers the run log
needs - the token count before and after the compaction and the number of summary lines -
travel with the result (:meth:`CompactionResult.event`).

The compaction is applied to the *running* session: the caller hands :func:`compact_context` a
``backend`` that compacts the live conversation (the pi lane does it through the pi RPC) and
that reports the real token counts before and after. Without such a backend the executor
compacts its own record of the session, which is what the backends without a compaction
command get.

The summary is extracted from the messages themselves, so a compaction never fails because a
provider was unavailable. A caller may still plug in a model backed summarizer
(:func:`prompt_summarizer`); if that summarizer raises, the compaction fails *softly*: the
original context is kept, the failure is reported, and the session keeps running.

The summary never drops a decision or a changed file silently: the facts of the earlier
summary are merged with the new ones item by item, and the items that do not fit into the
summary are counted (:attr:`CompactionResult.omitted`).

The module is loaded by path (openswarm/lib/ has no __init__.py), standard library only,
Python 3.9+.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence

# Rough token estimate: a CJK character costs about one token, everything else about four
# characters per token. The count only has to be good enough to decide when to compact.
CHARS_PER_TOKEN = 4
_CJK_RE = re.compile("[\u2e80-\u9fff\uac00-\ud7af\uff00-\uffef]")

# Defaults: compact a session once its context passes 120k tokens and keep the last 24k
# tokens (12 messages) verbatim.
DEFAULT_MAX_CONTEXT_TOKENS = 120_000
DEFAULT_KEEP_RECENT_TOKENS = 24_000
DEFAULT_KEEP_RECENT_MESSAGES = 12
# A second compaction only runs once the session grew by this share of the threshold again,
# so a context the backend did not shrink is not compacted over and over.
DEFAULT_MIN_GROWTH_SHARE = 0.1
# The summary keeps every distinct decision, changed file and todo it knows about, up to
# these caps. What does not fit is counted, never dropped without a trace.
DEFAULT_MAX_FACT_ITEMS = 120
DEFAULT_MAX_SUMMARY_LINES = 600
# The executor keeps at most this many messages, so a chatty session cannot exhaust memory.
MAX_TRACKED_MESSAGES = 4000

# The sections of a summary, in the order they are written.
KEY_SECTIONS = ("decisions", "files_changed", "todos")
SUMMARY_SECTIONS = KEY_SECTIONS + ("notes",)
SECTION_TITLES = {
    "decisions": "decisions:",
    "files_changed": "changed files:",
    "todos": "open todos:",
    "notes": "notes:",
}
SUMMARY_HEADER = "[summary of the compacted part of this session]"

# Markers of a line that states a decision worth keeping in the summary.
DECISION_MARKERS = (
    "decision", "decided", "we chose", "chose ", "instead of", "approach:", "plan:",
    "决定", "决策", "方案", "采用", "改用", "不采用", "选择",
)
TODO_MARKERS = ("todo", "to-do", "next step", "待办", "后续", "下一步")
# A checkbox line ("- [ ] fix the parser") is an open todo.
TODO_LINE_RE = re.compile(r"^\s*(?:[-*+]\s*)?\[( |x|X)\]\s*(.+?)\s*$")
# Paths mentioned by a tool call; the executor also reports the edited files directly.
FILE_PATH_RE = re.compile(r"(?<![\w./-])([\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]{1,10})")


def estimate_tokens(text: Any) -> int:
    """Rough token count of a text: CJK characters cost one token, the rest four characters."""
    if not text:
        return 0
    text = str(text)
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + (other + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dedupe(items: Sequence[str], limit: Optional[int] = None) -> List[str]:
    seen, out = set(), []
    for item in items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if limit is not None and len(out) >= limit:
            break
    return out


class CompactionPolicy:
    """When to compact a session context and how much of it to keep.

    ``max_context_tokens`` is the trigger: a session whose context is larger than that is
    compacted. ``keep_recent_tokens`` and ``keep_recent_messages`` are the retention window:
    the most recent messages are kept verbatim. ``summary_model`` names the model that writes
    the summary of the live session; without it the session model writes it.
    ``max_fact_items`` and ``max_summary_lines`` bound the summary: the facts that do not
    fit are counted, they are never dropped silently.
    """

    def __init__(self, enabled: bool = True,
                 max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
                 keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
                 keep_recent_messages: int = DEFAULT_KEEP_RECENT_MESSAGES,
                 summary_model: Optional[str] = None,
                 min_growth_share: float = DEFAULT_MIN_GROWTH_SHARE,
                 max_fact_items: int = DEFAULT_MAX_FACT_ITEMS,
                 max_summary_lines: int = DEFAULT_MAX_SUMMARY_LINES):
        self.enabled = bool(enabled)
        self.max_context_tokens = int(max_context_tokens)
        self.keep_recent_tokens = int(keep_recent_tokens)
        self.keep_recent_messages = int(keep_recent_messages)
        self.summary_model = summary_model or None
        self.min_growth_share = float(min_growth_share)
        self.max_fact_items = int(max_fact_items)
        self.max_summary_lines = int(max_summary_lines)

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "CompactionPolicy":
        """Build a policy from ``policy.session_compaction``; unusable values fall back."""
        config = config if isinstance(config, dict) else {}
        policy = cls()
        if "enabled" in config:
            policy.enabled = bool(config.get("enabled"))
        policy.max_context_tokens = _positive_int(
            config.get("max_context_tokens"), policy.max_context_tokens)
        policy.keep_recent_tokens = _positive_int(
            config.get("keep_recent_tokens"), policy.keep_recent_tokens)
        policy.keep_recent_messages = _positive_int(
            config.get("keep_recent_messages"), policy.keep_recent_messages)
        model = config.get("summary_model")
        policy.summary_model = str(model) if isinstance(model, str) and model.strip() else None
        share = config.get("min_growth_share")
        if isinstance(share, (int, float)) and not isinstance(share, bool) and share >= 0:
            policy.min_growth_share = float(share)
        policy.max_fact_items = _positive_int(
            config.get("max_fact_items"), policy.max_fact_items)
        policy.max_summary_lines = _positive_int(
            config.get("max_summary_lines"), policy.max_summary_lines)
        return policy

    @property
    def min_growth_tokens(self) -> int:
        """How much the session has to grow again before a second compaction runs."""
        return max(1, int(self.max_context_tokens * self.min_growth_share))

    @property
    def summary_line_budget(self) -> int:
        """The lines the summary may use.

        The summary has to fit into the threshold it helps to stay under, so its budget is
        a share of the threshold; the configured cap is the upper bound.
        """
        share = max(8, self.max_context_tokens // 100)
        return max(8, min(self.max_summary_lines, share))

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "max_context_tokens": self.max_context_tokens,
                "keep_recent_tokens": self.keep_recent_tokens,
                "keep_recent_messages": self.keep_recent_messages,
                "summary_model": self.summary_model,
                "min_growth_share": self.min_growth_share,
                "max_fact_items": self.max_fact_items,
                "max_summary_lines": self.max_summary_lines}


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value <= 0:
        return default
    return int(value)


class ContextMessage:
    """One normalized message of the session the executor drives."""

    __slots__ = ("role", "kind", "text", "files", "tool")

    def __init__(self, role: str = "assistant", text: str = "", kind: str = "text",
                 files: Optional[Sequence[str]] = None, tool: Optional[str] = None):
        self.role = role or "assistant"
        self.kind = kind or "text"
        self.text = text or ""
        self.files = list(files or [])
        self.tool = tool

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def is_summary(self) -> bool:
        return self.kind == "summary" or self.role == "summary"

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "kind": self.kind, "text": self.text,
                "files": list(self.files), "tool": self.tool}

    @classmethod
    def from_any(cls, value: Any) -> "ContextMessage":
        if isinstance(value, ContextMessage):
            return value
        if hasattr(value, "text") and hasattr(value, "role"):
            # A message of another module instance (the module is loaded by path).
            return cls(role=getattr(value, "role", "assistant"), text=getattr(value, "text", ""),
                       kind=getattr(value, "kind", "text"), files=getattr(value, "files", None),
                       tool=getattr(value, "tool", None))
        if isinstance(value, dict):
            return cls(role=value.get("role") or "assistant", text=value.get("text") or "",
                       kind=value.get("kind") or "text", files=value.get("files"),
                       tool=value.get("tool"))
        return cls(text=str(value))


class SessionContext:
    """The executor's record of the running session.

    ``messages`` holds the normalized messages the executor saw, ``observed_tokens`` the
    context size the backend reported for its last request (when it reports one) and
    ``summary`` the summary of the last successful compaction.
    """

    def __init__(self, messages: Optional[Sequence[Any]] = None, observed_tokens: int = 0):
        self.messages: List[ContextMessage] = [ContextMessage.from_any(m) for m in (messages or [])]
        self.observed_tokens = int(observed_tokens or 0)
        self.summary: Optional[str] = None
        self.compaction_count = 0
        self.last_compaction_tokens = 0
        self.last_compaction_at = 0.0
        # True while the backend still has to report the size of the compacted session:
        # pi reports it with the next assistant turn, not right after the compaction.
        self.awaiting_context_report = False

    def add(self, message: Any) -> ContextMessage:
        message = ContextMessage.from_any(message)
        self.messages.append(message)
        if len(self.messages) > MAX_TRACKED_MESSAGES:
            # Drop the oldest messages, but keep the summary of the last compaction.
            summaries = [m for m in self.messages if m.is_summary]
            tail = [m for m in self.messages[-MAX_TRACKED_MESSAGES:] if not m.is_summary]
            self.messages = summaries + tail
        return message

    def extend(self, messages: Sequence[Any]) -> None:
        for message in messages or []:
            self.add(message)

    def note_tokens(self, tokens: Any) -> int:
        """Record the context size the backend reported for its last request."""
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)):
            return self.observed_tokens
        self.observed_tokens = max(self.observed_tokens, int(tokens))
        return self.observed_tokens

    def set_tokens(self, tokens: Any) -> int:
        """Replace the observed context size (the backend reported a new one)."""
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)):
            return self.observed_tokens
        self.observed_tokens = max(0, int(tokens))
        return self.observed_tokens

    @property
    def estimated_tokens(self) -> int:
        return sum(message.tokens for message in self.messages)

    @property
    def tokens(self) -> int:
        """The size of the session context: the backend's number when it reported one."""
        return max(self.observed_tokens, self.estimated_tokens)

    def replace(self, messages: Sequence[Any], summary: Optional[str] = None) -> None:
        """Install the compacted messages; the backend's last report is stale afterwards."""
        self.messages = [ContextMessage.from_any(m) for m in (messages or [])]
        self.observed_tokens = 0
        if summary is not None:
            self.summary = summary
        self.compaction_count += 1

    def note_failed_compaction(self, tokens: Any) -> None:
        """Remember a failed compaction: the session is not compacted again before it grows."""
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)):
            return
        self.last_compaction_tokens = max(self.last_compaction_tokens, int(tokens))

    def apply(self, result: "CompactionResult", now: float = 0.0) -> bool:
        """Install a successful compaction; False when the result is not usable.

        A failed compaction changes nothing: the session keeps the context it had.
        """
        if result.failed or not result.triggered:
            return False
        self.replace(result.messages, summary=result.summary)
        self.last_compaction_tokens = result.tokens_before
        self.last_compaction_at = now
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"messages": [m.to_dict() for m in self.messages],
                "observed_tokens": self.observed_tokens,
                "estimated_tokens": self.estimated_tokens,
                "summary": self.summary,
                "compactions": self.compaction_count,
                "awaiting_context_report": self.awaiting_context_report}


def extract_key_facts(messages: Sequence[Any]) -> Dict[str, List[str]]:
    """Collect the facts a later turn needs: decisions, changed files and open todos."""
    decisions: List[str] = []
    files: List[str] = []
    todos: List[str] = []
    for raw in messages or []:
        message = ContextMessage.from_any(raw)
        files.extend(message.files)
        text = message.text or ""
        if message.kind == "tool_call":
            files.extend(FILE_PATH_RE.findall(text))
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            lowered = stripped.lower()
            if any(marker in lowered for marker in DECISION_MARKERS):
                decisions.append(stripped)
            checkbox = TODO_LINE_RE.match(stripped)
            if checkbox:
                todos.append(checkbox.group(2).strip())
            elif any(marker in lowered for marker in TODO_MARKERS):
                todos.append(stripped)
    return {"decisions": _dedupe(decisions), "files_changed": _dedupe(files), "todos": _dedupe(todos)}


def _key_notes(messages: Sequence[Any], limit: int = 8) -> List[str]:
    """A few assistant lines that describe what the session did."""
    notes: List[str] = []
    for raw in messages or []:
        message = ContextMessage.from_any(raw)
        if message.is_summary or message.role != "assistant" or message.kind != "text":
            continue
        for line in message.text.splitlines():
            stripped = line.strip()
            if len(stripped) < 8:
                continue
            lowered = stripped.lower()
            if any(marker in lowered for marker in DECISION_MARKERS):
                continue  # already kept as a decision
            if TODO_LINE_RE.match(stripped) or any(marker in lowered for marker in TODO_MARKERS):
                continue  # already kept as a todo
            notes.append(stripped)
            break
    return _dedupe(notes, limit)


def parse_summary(text: Optional[str]) -> Dict[str, List[str]]:
    """Read a summary back into its sections.

    A later compaction merges its new facts into these sections, so the decisions and the
    changed files of the earlier summary survive the next compaction.
    """
    sections: Dict[str, List[str]] = {name: [] for name in SUMMARY_SECTIONS}
    current = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped == SUMMARY_HEADER:
            continue
        matched = None
        for name, title in SECTION_TITLES.items():
            if stripped.lower() == title:
                matched = name
                break
        if matched:
            current = matched
            continue
        if current and stripped.startswith("- "):
            sections[current].append(stripped[2:].strip())
    return {name: items for name, items in sections.items() if items}


def merge_sections(previous: Optional[Dict[str, List[str]]], facts: Optional[Dict[str, List[str]]],
                   policy: Optional[CompactionPolicy] = None) -> Dict[str, Any]:
    """Merge the facts of a new span into the sections of the previous summary.

    The merge happens item by item, not line by line: the decisions and the changed files of
    the earlier summary compete with the new ones on equal footing, so a second compaction
    cannot push the new decisions out of the summary. The items that do not fit into the
    cap are counted, they are never dropped without a trace.
    """
    policy = policy or CompactionPolicy()
    previous = previous or {}
    facts = facts or {}
    sections: Dict[str, List[str]] = {}
    omitted: Dict[str, int] = {}
    for name in SUMMARY_SECTIONS:
        merged = _dedupe(list(previous.get(name) or []) + list(facts.get(name) or []))
        if len(merged) > policy.max_fact_items:
            omitted[name] = len(merged) - policy.max_fact_items
            # The newest facts win: the session moved on, the old ones are the ones to drop.
            merged = merged[-policy.max_fact_items:]
        else:
            omitted[name] = 0
        sections[name] = merged
    return {"sections": sections, "omitted": omitted}


class Summary:
    """The summary of a compacted span: its text, its sections and what it could not keep."""

    def __init__(self, text: str = "", sections: Optional[Dict[str, List[str]]] = None,
                 omitted: Optional[Dict[str, int]] = None):
        self.text = text or ""
        self.sections = sections or {}
        self.omitted = omitted or {}

    @property
    def lines(self) -> int:
        return len([line for line in self.text.splitlines() if line.strip()])

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "sections": {k: list(v) for k, v in self.sections.items()},
                "omitted": dict(self.omitted), "lines": self.lines}


def build_summary(messages: Sequence[Any], facts: Optional[Dict[str, List[str]]] = None,
                  previous: Optional[str] = None, policy: Optional[CompactionPolicy] = None) -> Summary:
    """Build the summary of a compacted span: decisions, changed files, todos and notes.

    The facts of the previous summary are merged in, so a second compaction keeps the
    decisions and the files of the first one. What does not fit into the summary is
    counted in the summary itself and in the result, so nothing is lost silently.
    """
    policy = policy or CompactionPolicy()
    facts = facts if facts is not None else extract_key_facts(messages)
    previous_sections = parse_summary(previous)
    notes = _dedupe(list(previous_sections.get("notes") or []) + _key_notes(messages))
    merged = merge_sections(previous_sections, dict(facts, notes=notes), policy)
    sections = merged["sections"]
    omitted = merged["omitted"]
    # The key sections share the line budget, so no section can push the others out.
    budget = policy.summary_line_budget
    per_section = max(1, (budget - 1) // len(SUMMARY_SECTIONS))
    lines = [SUMMARY_HEADER]
    for name in KEY_SECTIONS:
        items = sections.get(name) or []
        if not items:
            continue
        room = min(per_section, budget - len(lines) - 1)
        if room <= 0:
            omitted[name] = omitted.get(name, 0) + len(items)
            continue
        if len(items) > room:
            omitted[name] = omitted.get(name, 0) + len(items) - room
            items = items[-room:]
        lines.append(SECTION_TITLES[name])
        lines.extend("- " + _clip(item) for item in items)
    if notes:
        room = max(0, budget - len(lines) - 1)
        if room > 0:
            lines.append(SECTION_TITLES["notes"])
            lines.extend("- " + _clip(note) for note in notes[:room])
    dropped = {name: count for name, count in omitted.items() if count}
    if dropped:
        lines.append("note: not in this summary: "
                     + ", ".join("%d %s" % (count, name.replace("_", " "))
                                 for name, count in sorted(dropped.items()))
                     + " (the run log counts them)")
    return Summary("\n".join(lines), sections, omitted)


def summary_prompt(messages: Sequence[Any], policy: CompactionPolicy) -> str:
    """The prompt a model backed summarizer sends: the same sections, from the raw messages."""
    facts = extract_key_facts(messages)
    body = "\n".join(
        "[%s] %s" % (message.role, _clip(message.text, 600))
        for message in (ContextMessage.from_any(m) for m in messages)
    )
    return (
        "Summarize the compacted part of a coding session in at most %d lines. Keep the "
        "decisions, the files that were changed and the open todos; drop repeated tool "
        "output. Answer with the summary only.\n"
        "known decisions: %s\nknown changed files: %s\nknown todos: %s\n\nsession:\n%s"
        % (policy.max_summary_lines, facts["decisions"], facts["files_changed"], facts["todos"], body)
    )


def prompt_summarizer(call: Callable[[str, Optional[str]], str]) -> Callable[[Sequence[Any], CompactionPolicy], str]:
    """Wrap a ``call(prompt, model) -> str`` into a ``summarizer(messages, policy)``.

    The call is expected to use ``policy.summary_model``; a failing call raises and the
    caller falls back to the untouched context.
    """

    def summarizer(messages: Sequence[Any], policy: CompactionPolicy) -> str:
        return call(summary_prompt(messages, policy), policy.summary_model)

    return summarizer


def _cut_index(messages: Sequence[ContextMessage], policy: CompactionPolicy) -> int:
    """Index of the first kept message: the retention window, counted from the newest."""
    total = 0
    index = len(messages)
    kept = 0
    while index > 0 and kept < policy.keep_recent_messages:
        cost = messages[index - 1].tokens
        if kept and total + cost > policy.keep_recent_tokens:
            break
        total += cost
        index -= 1
        kept += 1
    if index == 0:
        # The window would swallow the whole session: keep the newest message only.
        index = len(messages) - 1 if len(messages) > 1 else len(messages)
    return index


class CompactionResult:
    """What a compaction did, and what the run log has to record about it."""

    def __init__(self, triggered: bool = False, failed: bool = False, error: Optional[str] = None,
                 tokens_before: int = 0, tokens_after: int = 0, summary: str = "",
                 messages: Optional[Sequence[Any]] = None, facts: Optional[Dict[str, List[str]]] = None,
                 omitted: Optional[Dict[str, int]] = None,
                 dropped_messages: int = 0, kept_messages: int = 0,
                 threshold: int = 0, summary_model: Optional[str] = None,
                 applied: bool = False, mode: str = "local",
                 tokens_source: str = "estimate", summary_model_applied: bool = False,
                 sections: Optional[Dict[str, List[str]]] = None, warning: Optional[str] = None):
        self.triggered = bool(triggered)
        self.failed = bool(failed)
        self.error = error
        self.tokens_before = int(tokens_before)
        self.tokens_after = int(tokens_after)
        self.summary = summary or ""
        self.messages = list(messages or [])
        self.facts = facts or {"decisions": [], "files_changed": [], "todos": []}
        self.omitted = omitted or {}
        self.dropped_messages = int(dropped_messages)
        self.kept_messages = int(kept_messages)
        self.threshold = int(threshold)
        self.summary_model = summary_model
        self.applied = bool(applied)
        self.mode = mode
        self.tokens_source = tokens_source
        self.summary_model_applied = bool(summary_model_applied)
        self.sections = sections or {}
        self.warning = warning

    @property
    def summary_lines(self) -> int:
        return len([line for line in self.summary.splitlines() if line.strip()])

    def event(self) -> Dict[str, Any]:
        """The run log record of this compaction."""
        event = {
            "type": "context_compaction_failed" if self.failed else "context_compacted",
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "tokens_source": self.tokens_source,
            "summary_lines": self.summary_lines,
            "threshold": self.threshold,
            "summary_model": self.summary_model,
            "summary_model_applied": self.summary_model_applied,
            "applied": self.applied,
            "mode": self.mode,
            "dropped_messages": self.dropped_messages,
            "kept_messages": self.kept_messages,
            "kept_decisions": len(self.facts.get("decisions") or []),
            "kept_files": len(self.facts.get("files_changed") or []),
            "kept_todos": len(self.facts.get("todos") or []),
            "omitted_decisions": int(self.omitted.get("decisions") or 0),
            "omitted_files": int(self.omitted.get("files_changed") or 0),
            "omitted_todos": int(self.omitted.get("todos") or 0),
            "failed": self.failed,
        }
        if self.error:
            event["error"] = self.error
        if self.warning:
            event["warning"] = self.warning
        return event


def compact_context(context: SessionContext, policy: CompactionPolicy,
                    summarizer: Optional[Callable[[Sequence[Any], CompactionPolicy], str]] = None,
                    backend: Optional[Any] = None) -> CompactionResult:
    """Compact the early part of a session context once it crosses the threshold.

    ``backend`` compacts the running session: it is an object with a
    ``compact(facts, policy)`` method that returns a dict with the real numbers
    (``tokens_before``, ``tokens_after``, ``summary``) and either ``applied`` or an
    ``error``. The backend failure is soft: the session keeps the context it had and the
    run continues.

    The result carries the compacted messages; the caller installs them.
    """
    tokens_before = context.tokens
    unchanged = CompactionResult(triggered=False, failed=False, error=None,
                                 tokens_before=tokens_before, tokens_after=tokens_before,
                                 messages=list(context.messages), kept_messages=len(context.messages),
                                 threshold=policy.max_context_tokens,
                                 summary_model=policy.summary_model)
    if not policy.enabled:
        return unchanged
    if tokens_before <= policy.max_context_tokens:
        return unchanged
    if tokens_before < context.last_compaction_tokens + policy.min_growth_tokens:
        # The session did not grow since the last compaction: compacting again would only
        # rewrite the same summary.
        return unchanged

    messages = list(context.messages)
    cut = _cut_index(messages, policy)
    if cut <= 0:
        return CompactionResult(triggered=False, failed=False,
                                error="the retention window covers the whole context",
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                messages=messages, kept_messages=len(messages),
                                threshold=policy.max_context_tokens,
                                summary_model=policy.summary_model)
    early, kept = messages[:cut], messages[cut:]
    previous = early[0].text if early and early[0].is_summary else None
    span = [m for m in early if not m.is_summary]
    facts = extract_key_facts(span)

    if backend is not None:
        return _compact_with_backend(context, policy, backend, facts, messages, kept, cut,
                                     tokens_before)

    try:
        built = build_summary(span, facts, previous=previous, policy=policy)
        summary = summarizer(span, policy) if summarizer else built.text
    except Exception as error:  # a failing summarizer must not end the session
        return CompactionResult(triggered=True, failed=True,
                                error=("%s: %s" % (type(error).__name__, error))[:300],
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                messages=messages, facts=facts,
                                dropped_messages=0, kept_messages=len(messages),
                                threshold=policy.max_context_tokens,
                                summary_model=policy.summary_model)
    summary = (summary or "").strip()
    if not summary:
        # An empty summary would drop the early part of the session without replacing it.
        return CompactionResult(triggered=True, failed=True, error="the summary is empty",
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                messages=messages, facts=facts,
                                dropped_messages=0, kept_messages=len(messages),
                                threshold=policy.max_context_tokens,
                                summary_model=policy.summary_model)
    compacted = [ContextMessage(role="summary", kind="summary", text=summary)] + list(kept)
    tokens_after = sum(message.tokens for message in compacted)
    return CompactionResult(triggered=True, failed=False, error=None,
                            tokens_before=tokens_before, tokens_after=tokens_after,
                            summary=summary, messages=compacted, facts=facts,
                            omitted=built.omitted,
                            dropped_messages=cut, kept_messages=len(kept),
                            threshold=policy.max_context_tokens,
                            summary_model=policy.summary_model,
                            applied=True, mode="local", tokens_source="estimate",
                            sections=built.sections)


def _compact_with_backend(context: SessionContext, policy: CompactionPolicy, backend: Any,
                          facts: Dict[str, List[str]], messages: List[ContextMessage],
                          kept: List[ContextMessage], cut: int, tokens_before: int) -> CompactionResult:
    """Compact the running session through the backend and report its real numbers."""
    try:
        outcome = backend.compact(facts, policy) or {}
    except Exception as error:  # a failing backend must not end the session
        outcome = {"error": "%s: %s" % (type(error).__name__, error)}
    error = outcome.get("error")
    if error or not outcome.get("applied"):
        return CompactionResult(triggered=True, failed=True,
                                error=str(error or "the session could not be compacted")[:300],
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                messages=messages, facts=facts,
                                dropped_messages=0, kept_messages=len(messages),
                                threshold=policy.max_context_tokens,
                                summary_model=policy.summary_model,
                                applied=False, mode="session", tokens_source="backend")
    summary = (outcome.get("summary") or "").strip()
    if not summary:
        # An empty summary would drop the early part of the session without replacing it.
        return CompactionResult(triggered=True, failed=True, error="the summary is empty",
                                tokens_before=tokens_before, tokens_after=tokens_before,
                                messages=messages, facts=facts,
                                dropped_messages=0, kept_messages=len(messages),
                                threshold=policy.max_context_tokens,
                                summary_model=policy.summary_model,
                                applied=False, mode="session", tokens_source="backend")
    compacted = [ContextMessage(role="summary", kind="summary", text=summary)] + list(kept)
    backend_before = outcome.get("tokens_before")
    backend_after = outcome.get("tokens_after")
    tokens_before = int(backend_before) if isinstance(backend_before, (int, float)) else tokens_before
    if isinstance(backend_after, (int, float)) and not isinstance(backend_after, bool) and backend_after > 0:
        tokens_after = int(backend_after)
        tokens_source = "backend"
    else:
        # The backend does not report the size of the compacted session right away (pi
        # reports it with the next assistant turn): the estimate of the compacted context
        # is used and the run log says which of the two numbers it is.
        tokens_after = sum(message.tokens for message in compacted)
        tokens_source = "estimate"
    return CompactionResult(triggered=True, failed=False, error=None,
                            tokens_before=tokens_before, tokens_after=tokens_after,
                            summary=summary, messages=compacted, facts=facts,
                            dropped_messages=cut, kept_messages=len(kept),
                            threshold=policy.max_context_tokens,
                            summary_model=policy.summary_model,
                            applied=True, mode="session",
                            tokens_source=tokens_source,
                            summary_model_applied=bool(outcome.get("summary_model_applied")),
                            sections=outcome.get("sections") or {},
                            warning=outcome.get("warning"))
