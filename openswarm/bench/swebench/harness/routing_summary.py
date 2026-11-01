"""Extract the small part of openswarm's skill that helps the planner route work."""
from __future__ import annotations

import re

MAX_CHARS = 4000
FALLBACK_LINES = 40
_SECTION = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
_ROUTING_TITLE = re.compile(
    r"backend|routing|decompos|task|acceptance|verif|test|command|execute|gate|file",
    re.IGNORECASE,
)


def routing_summary(skill: str, max_chars: int = MAX_CHARS, fallback_lines: int = FALLBACK_LINES) -> str:
    """Return relevant heading-delimited sections, or a noted prefix when none match."""
    headings = list(_SECTION.finditer(skill))
    chunks = []
    for index, heading in enumerate(headings):
        if not _ROUTING_TITLE.search(heading.group(2)):
            continue
        end = index + 1
        while end < len(headings) and len(headings[end].group(1)) > len(heading.group(1)):
            end += 1
        if any(
            len(headings[child].group(1)) > len(heading.group(1))
            and _ROUTING_TITLE.search(headings[child].group(2))
            for child in range(index + 1, end)
        ):
            continue
        section_end = headings[end].start() if end < len(headings) else len(skill)
        chunks.append(skill[heading.start():section_end].strip())

    if chunks:
        separator = "\n\n"
        budget = max(1, (max_chars - len(separator) * (len(chunks) - 1)) // len(chunks))
        chunks = [chunk if len(chunk) <= budget else chunk[:budget - 1] + "…" for chunk in chunks]
        summary = separator.join(chunks)
    else:
        summary = "Fallback: no routing-related headings found; showing the first lines of SKILL.md.\n"
        summary += "\n".join(skill.splitlines()[:fallback_lines])
    if len(summary) <= max_chars:
        return summary
    return summary[:max_chars]
