"""Tests of the private denylist interface of the public-verify scanner (Refs #2034).

The private denylist never lives in this repository: the tests build one from
temporary files and from fictional terms only.  They also pin the contract that
a finding never carries the matched text, only its category, its position and
the hash prefix of the matched text.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import traceback
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "ci" / "public_verify_denylist.py"

# Fictional terms: they are not real hosts, accounts or paths.
HOST_TERM = "gw-fixture-01"
DOMAIN_TERM = "fixture-zone.invalid"
BUS_TERM = "bus-fixture-77"
ACCOUNT_TERM = "acct-fixture-42"
HOLDOUT_TERM = "fixture-holdout-dir"


def _import_module():
    if not MODULE_PATH.is_file():
        raise FileNotFoundError(f"missing module: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("public_verify_denylist", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


denylist = _import_module()


def rule(category, term):
    return f"{category}\t{term}"


class DenylistLoadTests(unittest.TestCase):
    """Loading of the private denylist (env variable, file, fail-closed)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_denylist(self, content, name="denylist.txt"):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_from_the_environment_variable(self):
        env = {denylist.DENYLIST_ENV: rule("host", HOST_TERM) + "\n"}
        matcher = denylist.load(env=env)
        self.assertEqual(len(matcher), 1)
        hits = matcher.scan(f"connect to {HOST_TERM} now")
        self.assertEqual([hit.category for hit in hits], ["host"])

    def test_loads_from_a_file(self):
        path = self.write_denylist(rule("domain", DOMAIN_TERM) + "\n")
        matcher = denylist.load(env={}, denylist_path=path)
        self.assertEqual(len(matcher), 1)
        hits = matcher.scan(f"host {DOMAIN_TERM} is up")
        self.assertEqual([hit.category for hit in hits], ["domain"])

    def test_file_takes_precedence_over_the_environment_variable(self):
        env = {denylist.DENYLIST_ENV: rule("host", HOST_TERM)}
        path = self.write_denylist(rule("bus", BUS_TERM) + "\n")
        matcher = denylist.load(env=env, denylist_path=path)
        self.assertEqual(matcher.scan(HOST_TERM), [])
        self.assertEqual(len(matcher.scan(BUS_TERM)), 1)

    def test_missing_denylist_is_a_configuration_error(self):
        with self.assertRaises(denylist.DenylistError) as ctx:
            denylist.load(env={})
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_missing_file_is_a_configuration_error(self):
        with self.assertRaises(denylist.DenylistError) as ctx:
            denylist.load(env={}, denylist_path=self.root / "absent.txt")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_empty_denylist_is_a_configuration_error(self):
        with self.assertRaises(denylist.DenylistError):
            denylist.load(env={denylist.DENYLIST_ENV: ""})
        path = self.write_denylist("\n\n")
        with self.assertRaises(denylist.DenylistError):
            denylist.load(env={}, denylist_path=path)

    def test_denylist_with_only_comments_is_empty(self):
        path = self.write_denylist("# only a comment\n")
        with self.assertRaises(denylist.DenylistError):
            denylist.load(env={}, denylist_path=path)

    def test_generic_only_skips_the_denylist(self):
        matcher = denylist.load(env={}, generic_only=True)
        self.assertEqual(len(matcher), 0)
        self.assertEqual(matcher.scan(f"{HOST_TERM} {ACCOUNT_TERM}"), [])

    def test_generic_only_skips_a_present_denylist(self):
        env = {denylist.DENYLIST_ENV: rule("host", HOST_TERM)}
        matcher = denylist.load(env=env, generic_only=True)
        self.assertEqual(matcher.scan(HOST_TERM), [])

    def test_invalid_rules_are_configuration_errors(self):
        cases = [
            "secret\tterm",            # unknown category
            "hostterm",                # missing separator
            "host\t",                  # empty term
            "host\tre:[unclosed",      # invalid regular expression
            "host\tre:",               # empty regular expression
        ]
        for line in cases:
            with self.subTest(line=line):
                with self.assertRaises(denylist.DenylistError):
                    denylist.load(env={denylist.DENYLIST_ENV: line + "\n"})

    def test_configuration_errors_do_not_echo_the_denylist_content(self):
        """A configuration error never repeats the private denylist content."""
        category = "priv" + "ate"
        term = "zz" + "secret-fixture"
        group = "grp" + "fixture"
        cases = [
            (f"{category}\t{term}\n", (category, term)),
            (f"host\tre:(?P<{group}>a)(?P<{group}>b)\n", (group,)),
        ]
        for content, forbidden in cases:
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(denylist.DenylistError) as ctx:
                    denylist.load(env={denylist.DENYLIST_ENV: content})
                error = ctx.exception
                rendered = str(error) + "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
                for fragment in forbidden:
                    self.assertNotIn(fragment, rendered)
                if error.__context__ is not None:
                    self.assertTrue(
                        error.__suppress_context__,
                        "the original error must not be chained into the traceback",
                    )

    def test_comments_and_blank_lines_are_ignored(self):
        content = "\n".join(["# a comment", "", rule("host", HOST_TERM), ""]) + "\n"
        matcher = denylist.load(env={denylist.DENYLIST_ENV: content})
        self.assertEqual(len(matcher), 1)


class DenylistMatchTests(unittest.TestCase):
    """Matching rules of the private denylist."""

    def matcher(self, *lines):
        content = "\n".join(lines) + "\n"
        return denylist.load(env={denylist.DENYLIST_ENV: content})

    def test_every_category_is_supported(self):
        for category in ("host", "domain", "bus", "account", "holdout"):
            with self.subTest(category=category):
                term = f"fixture-{category}"
                matcher = self.matcher(rule(category, term))
                hits = matcher.scan(term)
                self.assertEqual([hit.category for hit in hits], [category])

    def test_matching_is_case_insensitive(self):
        matcher = self.matcher(rule("host", HOST_TERM))
        self.assertEqual(len(matcher.scan(HOST_TERM.upper())), 1)
        self.assertEqual(len(matcher.scan(HOST_TERM)), 1)

    def test_matching_respects_word_boundaries(self):
        matcher = self.matcher(rule("host", HOST_TERM))
        self.assertEqual(matcher.scan(f"x{HOST_TERM}"), [])
        self.assertEqual(matcher.scan(f"{HOST_TERM}x"), [])
        self.assertEqual(len(matcher.scan(f"host {HOST_TERM}.")), 1)

    def test_suffix_terms_match_inside_a_longer_name(self):
        matcher = self.matcher(rule("domain", ".fixture.invalid"))
        self.assertEqual(len(matcher.scan("node.fixture.invalid")), 1)
        self.assertEqual(matcher.scan("node.fixture.invalidx"), [])

    def test_regular_expression_rules(self):
        matcher = self.matcher(rule("bus", "re:bus-fixture-[0-9]+"))
        self.assertEqual(len(matcher.scan(BUS_TERM)), 1)
        self.assertEqual(matcher.scan("bus-fixture-xx"), [])

    def test_regular_expression_rules_respect_word_boundaries(self):
        matcher = self.matcher(rule("host", "re:gw-fixture-[0-9]+"))
        self.assertEqual(matcher.scan("x" + HOST_TERM + "y"), [])
        self.assertEqual(matcher.scan(HOST_TERM + "x"), [])
        self.assertEqual(len(matcher.scan(HOST_TERM)), 1)
        self.assertEqual(len(matcher.scan("node." + HOST_TERM)), 1)

    def test_regular_expression_suffix_matches_a_longer_name(self):
        matcher = self.matcher(rule("domain", "re:\\.invalid"))
        self.assertEqual(len(matcher.scan("node.fixture.invalid")), 1)
        self.assertEqual(matcher.scan("node.fixture.invalidx"), [])

    def test_findings_carry_no_raw_text(self):
        matcher = self.matcher(rule("account", ACCOUNT_TERM))
        hits = matcher.scan(f"user {ACCOUNT_TERM} signed in")
        self.assertEqual(len(hits), 1)
        rendered = json.dumps(hits[0].as_dict())
        self.assertNotIn(ACCOUNT_TERM, rendered)
        self.assertNotIn(ACCOUNT_TERM.lower(), rendered)
        self.assertEqual(set(hits[0].as_dict()), {"category", "location", "hash8"})

    def test_finding_reports_the_hash_prefix(self):
        matcher = self.matcher(rule("host", HOST_TERM))
        hits = matcher.scan(f"host {HOST_TERM}")
        expected = hashlib.sha256(HOST_TERM.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(hits[0].hash8, expected)

    def test_finding_hash_is_the_hash_of_the_matched_text(self):
        matcher = self.matcher(rule("host", HOST_TERM))
        matched = HOST_TERM.upper()
        hits = matcher.scan(matched)
        expected = hashlib.sha256(matched.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(hits[0].hash8, expected)
        self.assertNotEqual(hits[0].hash8, hashlib.sha256(HOST_TERM.encode("utf-8")).hexdigest()[:8])

    def test_finding_reports_the_position(self):
        matcher = self.matcher(rule("host", HOST_TERM))
        text = f"first line\nsecond {HOST_TERM}\n"
        hits = matcher.scan(text)
        self.assertEqual(hits[0].line, 2)
        self.assertEqual(hits[0].column, 8)
        self.assertEqual(hits[0].start, text.index(HOST_TERM))
        self.assertEqual(hits[0].as_dict()["location"]["offset"], text.index(HOST_TERM))

    def test_clean_text_has_no_finding(self):
        matcher = self.matcher(rule("host", HOST_TERM), rule("holdout", HOLDOUT_TERM))
        self.assertEqual(matcher.scan("nothing to see here"), [])

    def test_duplicate_findings_are_reported_once(self):
        matcher = self.matcher(rule("host", HOST_TERM), rule("host", HOST_TERM))
        self.assertEqual(len(matcher.scan(HOST_TERM)), 1)


if __name__ == "__main__":
    unittest.main()
