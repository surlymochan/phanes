"""Registry section in openswarm/docs/EXTERNAL-LANES.md (issue #1474).

The doc addition alone is docs-only, which the external-lane contract
rejects as a thin diff (>= 30 effective code/test lines). This test is
the code side of that card: it pins the registry section so a later
edit cannot silently drop a live agent name or the inbox PR convention.
It does not touch the dispatcher or the agent SDK itself.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def find_doc():
    """Walk up from this test file until openswarm/docs/EXTERNAL-LANES.md is found."""
    here = HERE
    for _ in range(8):
        candidate = os.path.join(here, "openswarm", "docs", "EXTERNAL-LANES.md")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    # Fallback for a checkout rooted at openswarm/ itself.
    candidate = os.path.join(HERE, "..", "..", "docs", "EXTERNAL-LANES.md")
    return os.path.normpath(candidate)


def registry_section(text):
    """Text from the registry heading to the next top-level heading."""
    match = re.search(r"^## Agent name registry \(live\)\s*$", text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


class AgentRegistryTest(unittest.TestCase):
    def setUp(self):
        self.doc = find_doc()
        with open(self.doc, encoding="utf-8") as fh:
            self.text = fh.read()
        self.section = registry_section(self.text)

    def test_doc_exists_and_has_registry_section(self):
        self.assertTrue(os.path.isfile(self.doc), f"missing doc: {self.doc}")
        self.assertNotEqual(self.section, "", "no 'Agent name registry (live)' section in EXTERNAL-LANES.md")

    def test_live_agents_listed(self):
        for name in ("chatgpt-agent", "external-muse"):
            with self.subTest(agent=name):
                self.assertIn(name, self.section)
                self.assertRegex(self.section, rf"\| `{re.escape(name)}` \|")

    def test_inbox_pr_convention(self):
        for name in ("chatgpt-agent", "external-muse"):
            with self.subTest(agent=name):
                self.assertIn(f"external-inbox/{name}", self.section)
        self.assertIn("external-inbox/<name>", self.section)
        self.assertIn("external-inbox", self.section)
        self.assertIn("never merged", self.section)

    def test_registry_is_not_the_allowlist(self):
        # The registry must not be mistaken for the allowlist: the doc has
        # to say so explicitly, or a reader will treat a table row as access.
        self.assertIn("informational only", self.section)
        self.assertIn("external_backends", self.section)

    def test_acceptance_greps(self):
        # Mirrors the issue's acceptance commands, on the doc itself.
        self.assertIn("external-muse", self.text)
        self.assertIn("chatgpt-agent", self.text)


if __name__ == "__main__":
    unittest.main()
