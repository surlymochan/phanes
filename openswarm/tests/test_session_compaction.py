"""Session context compaction (Refs #1716).

Covers the compaction engine in ``lib/session/compaction.py`` and its use in the lane turn
loop: the threshold trigger, the facts the summary keeps, the soft failure and the record
the run log gets. The lane turn loop is tested against a pi session that speaks the RPC of
pi: the compaction has to shrink the *running* session, not only the record the executor
keeps.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

sys.path.insert(0, str(HERE))
import _load  # noqa: E402

xs = _load.load()

_spec = importlib.util.spec_from_file_location("session_compaction", ROOT / "lib" / "session" / "compaction.py")
compaction = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compaction)

REPORT = {"status": "done", "summary": "compacted the session", "files_changed": ["parser.py"],
          "tests": "none", "followups": []}


def long_context(rounds=110, distinct=True):
    """A session whose context is about 200k tokens: decisions, edits and repeated tool output.

    With ``distinct`` every round brings its own decision, its own file and its own todo, so
    a test can see whether the summary really keeps them.
    """
    context = compaction.SessionContext()
    context.add({"role": "user", "text": "implement the parser"})
    for index in range(rounds):
        decision = ("Decision: the parser stays in parser.py (step %d)" % index) if distinct else \
            "Decision: the parser stays in parser.py"
        file_name = "parser_%d.py" % index if distinct else "parser.py"
        todo = "- [ ] finish the edge case %d" % index if distinct else "- [ ] finish the edge case"
        context.add({"role": "assistant", "text": decision + "\n%s" % ("d" * 3900)})
        context.add({"role": "assistant", "kind": "tool_call", "text": "edit " + file_name,
                     "files": [file_name]})
        context.add({"role": "tool", "kind": "tool_result", "text": "o" * 3900})
        context.add({"role": "assistant", "text": todo})
    return context


class PolicyTests(unittest.TestCase):
    def test_defaults(self):
        policy = compaction.CompactionPolicy.from_config({})
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.max_context_tokens, compaction.DEFAULT_MAX_CONTEXT_TOKENS)
        self.assertEqual(policy.keep_recent_tokens, compaction.DEFAULT_KEEP_RECENT_TOKENS)
        self.assertEqual(policy.keep_recent_messages, compaction.DEFAULT_KEEP_RECENT_MESSAGES)
        self.assertIsNone(policy.summary_model)

    def test_configured_values_are_used(self):
        policy = compaction.CompactionPolicy.from_config(
            {"enabled": False, "max_context_tokens": 5000, "keep_recent_tokens": 500,
             "keep_recent_messages": 3, "summary_model": "MiniMax-M3.1-Flash", "min_growth_share": 0.5,
             "max_fact_items": 40, "max_summary_lines": 80})
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.max_context_tokens, 5000)
        self.assertEqual(policy.keep_recent_tokens, 500)
        self.assertEqual(policy.keep_recent_messages, 3)
        self.assertEqual(policy.summary_model, "MiniMax-M3.1-Flash")
        self.assertEqual(policy.min_growth_tokens, 2500)
        self.assertEqual(policy.max_fact_items, 40)
        self.assertEqual(policy.max_summary_lines, 80)

    def test_unusable_values_fall_back_to_the_defaults(self):
        policy = compaction.CompactionPolicy.from_config(
            {"max_context_tokens": "many", "keep_recent_tokens": -1, "keep_recent_messages": 0,
             "summary_model": "  ", "min_growth_share": "half", "max_fact_items": 0,
             "max_summary_lines": -5})
        self.assertEqual(policy.max_context_tokens, compaction.DEFAULT_MAX_CONTEXT_TOKENS)
        self.assertEqual(policy.keep_recent_tokens, compaction.DEFAULT_KEEP_RECENT_TOKENS)
        self.assertEqual(policy.keep_recent_messages, compaction.DEFAULT_KEEP_RECENT_MESSAGES)
        self.assertIsNone(policy.summary_model)
        self.assertEqual(policy.min_growth_share, compaction.DEFAULT_MIN_GROWTH_SHARE)
        self.assertEqual(policy.max_fact_items, compaction.DEFAULT_MAX_FACT_ITEMS)
        self.assertEqual(policy.max_summary_lines, compaction.DEFAULT_MAX_SUMMARY_LINES)

    def test_the_shipped_policy_compacts_long_sessions(self):
        policy = xs.jload(str(ROOT / "policy.json"), {}).get("session_compaction") or {}
        parsed = compaction.CompactionPolicy.from_config(policy)
        self.assertTrue(parsed.enabled)
        self.assertGreater(parsed.max_context_tokens, 0)
        self.assertLess(parsed.keep_recent_tokens, parsed.max_context_tokens)


class TokenTests(unittest.TestCase):
    def test_estimate_counts_cjk_and_plain_text(self):
        self.assertEqual(compaction.estimate_tokens(""), 0)
        self.assertEqual(compaction.estimate_tokens(None), 0)
        self.assertEqual(compaction.estimate_tokens("abcd"), 1)
        self.assertEqual(compaction.estimate_tokens("a" * 400), 100)
        self.assertEqual(compaction.estimate_tokens("上下文压缩"), 5)


class CompactionTriggerTests(unittest.TestCase):
    def setUp(self):
        self.policy = compaction.CompactionPolicy(max_context_tokens=1000, keep_recent_tokens=100,
                                                   keep_recent_messages=2)

    def test_a_small_session_is_not_compacted(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "short answer"})
        result = compaction.compact_context(context, self.policy)
        self.assertFalse(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(result.tokens_before, result.tokens_after)
        self.assertEqual(len(result.messages), 1)

    def test_a_disabled_policy_never_compacts(self):
        context = long_context()
        policy = compaction.CompactionPolicy.from_config({"enabled": False, "max_context_tokens": 100})
        result = compaction.compact_context(context, policy)
        self.assertFalse(result.triggered)
        self.assertEqual(len(result.messages), len(context.messages))

    def test_a_session_over_the_threshold_is_compacted(self):
        context = long_context()
        self.assertGreater(context.tokens, self.policy.max_context_tokens)
        result = compaction.compact_context(context, self.policy)
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(result.tokens_before, context.tokens)
        self.assertLess(result.tokens_after, result.tokens_before)
        self.assertGreater(result.dropped_messages, 0)
        self.assertLessEqual(result.kept_messages, self.policy.keep_recent_messages + 1)

    def test_the_compacted_context_fits_into_the_threshold(self):
        context = long_context()
        result = compaction.compact_context(context, self.policy)
        self.assertTrue(context.apply(result))
        self.assertLessEqual(context.tokens, self.policy.max_context_tokens)

    def test_a_second_compaction_needs_growth(self):
        context = long_context()
        first = compaction.compact_context(context, self.policy)
        context.apply(first)
        context.note_tokens(context.tokens + 10)
        again = compaction.compact_context(context, self.policy)
        self.assertFalse(again.triggered, "a session that did not grow must not be compacted again")

    def test_the_summary_is_cumulative(self):
        context = long_context()
        first = compaction.compact_context(context, self.policy)
        context.apply(first)
        context.add({"role": "assistant", "text": "Decision: the parser moves to lexer.py"})
        context.add({"role": "assistant", "text": "the lexer is written"})
        context.add({"role": "assistant", "text": "the tests are green"})
        context.note_tokens(context.last_compaction_tokens + self.policy.min_growth_tokens + 1)
        second = compaction.compact_context(context, self.policy)
        self.assertTrue(second.triggered)
        self.assertIn("lexer.py", second.summary)
        self.assertIn("parser.py", second.summary, "the earlier summary has to survive the next compaction")


class KeyFactTests(unittest.TestCase):
    def test_the_summary_keeps_the_decisions_the_files_and_the_todos(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "Decision: the guard refuses writes outside the worktree"})
        context.add({"role": "assistant", "kind": "tool_call", "text": "edit lib/session/compaction.py",
                     "files": ["lib/session/compaction.py"]})
        context.add({"role": "assistant", "text": "- [ ] write the run log record"})
        context.add({"role": "assistant", "text": "the run log record is written"})
        policy = compaction.CompactionPolicy(max_context_tokens=10, keep_recent_tokens=1,
                                             keep_recent_messages=1)
        result = compaction.compact_context(context, policy)
        self.assertTrue(result.triggered)
        self.assertIn("the guard refuses writes outside the worktree", result.summary)
        self.assertIn("lib/session/compaction.py", result.summary)
        self.assertIn("write the run log record", result.summary)
        self.assertEqual(result.facts["decisions"], ["Decision: the guard refuses writes outside the worktree"])
        self.assertEqual(result.facts["files_changed"], ["lib/session/compaction.py"])
        self.assertEqual(result.facts["todos"], ["write the run log record"])

    def test_the_repeated_tool_output_is_dropped(self):
        context = compaction.SessionContext()
        context.add({"role": "tool", "kind": "tool_result", "text": "same output " * 100})
        policy = compaction.CompactionPolicy(max_context_tokens=10, keep_recent_tokens=1,
                                             keep_recent_messages=1)
        result = compaction.compact_context(context, policy)
        self.assertTrue(result.triggered)
        self.assertNotIn("same output", result.summary)

    def test_a_todo_marker_is_a_todo(self):
        facts = compaction.extract_key_facts([{"role": "assistant", "text": "TODO: wire the broker log"}])
        self.assertEqual(facts["todos"], ["TODO: wire the broker log"])

    def test_a_new_decision_is_not_pushed_out_by_the_earlier_summary(self):
        """The earlier summary and the new facts are merged item by item (Refs #1716)."""
        policy = compaction.CompactionPolicy(max_context_tokens=100000, keep_recent_tokens=1,
                                             keep_recent_messages=1, max_fact_items=4)
        previous = compaction.build_summary(
            [{"role": "assistant", "text": "Decision: the first decision"},
             {"role": "assistant", "kind": "tool_call", "text": "edit first.py", "files": ["first.py"]}],
            policy=policy).text
        merged = compaction.build_summary(
            [{"role": "assistant", "text": "Decision: the second decision"},
             {"role": "assistant", "kind": "tool_call", "text": "edit second.py", "files": ["second.py"]}],
            previous=previous, policy=policy)
        self.assertIn("the second decision", merged.text)
        self.assertIn("second.py", merged.text)
        self.assertIn("the first decision", merged.text, "the earlier decision has to survive")
        self.assertIn("first.py", merged.text, "the earlier file has to survive")

    def test_what_does_not_fit_is_counted_and_not_dropped_silently(self):
        policy = compaction.CompactionPolicy(max_context_tokens=2000, keep_recent_tokens=1,
                                             keep_recent_messages=1, max_fact_items=2)
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "f" * 12000})
        for index in range(6):
            context.add({"role": "assistant", "text": "Decision: decision number %d" % index})
        result = compaction.compact_context(context, policy)
        self.assertTrue(result.triggered)
        self.assertEqual(result.omitted.get("decisions"), 3)
        self.assertNotIn("decision number 0", result.summary)
        self.assertIn("decision number 4", result.summary)
        self.assertIn("not in this summary", result.summary)
        event = result.event()
        self.assertEqual(event["omitted_decisions"], 3)


class BackendTokenTests(unittest.TestCase):
    """The numbers of the run log follow the numbers the backend reports."""

    class Backend:
        """A backend that compacts the session and reports the numbers it knows."""

        def __init__(self, tokens_after=None):
            self.tokens_after = tokens_after

        def compact(self, facts, policy):
            return {"applied": True, "summary": "decisions:\n- keep the parser",
                    "tokens_before": 5000, "tokens_after": self.tokens_after}

    def test_the_size_the_backend_reports_is_the_size_of_the_run_log(self):
        context = long_context()
        policy = compaction.CompactionPolicy(max_context_tokens=100, keep_recent_tokens=10,
                                             keep_recent_messages=1)
        result = compaction.compact_context(context, policy, backend=self.Backend(tokens_after=800))
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(result.tokens_source, "backend")
        self.assertEqual(result.tokens_after, 800)
        self.assertEqual(result.event()["tokens_after"], 800)

    def test_a_backend_that_reports_no_size_does_not_claim_an_empty_context(self):
        """pi reports the size of a compacted session only with the next assistant turn."""
        context = long_context()
        policy = compaction.CompactionPolicy(max_context_tokens=100, keep_recent_tokens=10,
                                             keep_recent_messages=1)
        result = compaction.compact_context(context, policy, backend=self.Backend())
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(result.tokens_source, "estimate")
        self.assertGreater(result.tokens_after, 0, "the run log must not claim an empty context")
        self.assertLess(result.tokens_after, result.tokens_before)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.policy = compaction.CompactionPolicy(max_context_tokens=100, keep_recent_tokens=10,
                                                   keep_recent_messages=1)

    def test_a_failing_summarizer_keeps_the_original_context(self):
        context = long_context()

        def broken(messages, policy):
            raise RuntimeError("the summary model is unavailable")

        result = compaction.compact_context(context, self.policy, summarizer=broken)
        self.assertTrue(result.triggered)
        self.assertTrue(result.failed)
        self.assertIn("the summary model is unavailable", result.error)
        self.assertEqual(result.tokens_after, result.tokens_before)
        self.assertEqual(len(result.messages), len(context.messages))
        self.assertFalse(context.apply(result), "a failed compaction must not replace the context")
        self.assertEqual(len(context.messages), len(long_context().messages))

    def test_an_empty_summary_is_a_failure(self):
        context = long_context()
        result = compaction.compact_context(context, self.policy, summarizer=lambda messages, policy: "  ")
        self.assertTrue(result.failed)
        self.assertEqual(result.tokens_after, result.tokens_before)

    def test_a_failing_backend_keeps_the_original_context(self):
        context = long_context()

        class BrokenBackend:
            def compact(self, facts, policy):
                raise RuntimeError("the session is gone")

        result = compaction.compact_context(context, self.policy, backend=BrokenBackend())
        self.assertTrue(result.triggered)
        self.assertTrue(result.failed)
        self.assertIn("the session is gone", result.error)
        self.assertFalse(result.applied)
        self.assertEqual(len(result.messages), len(context.messages))
        self.assertFalse(context.apply(result))

    def test_a_model_backed_summarizer_gets_the_policy_model(self):
        seen = {}

        def model_call(prompt, model):
            seen["prompt"] = prompt
            seen["model"] = model
            return "decisions:\n- keep the parser"

        policy = compaction.CompactionPolicy(max_context_tokens=100, keep_recent_tokens=10,
                                              keep_recent_messages=1, summary_model="MiniMax-M3.1-Flash")
        context = long_context()
        summarizer = compaction.prompt_summarizer(model_call)
        result = compaction.compact_context(context, policy, summarizer=summarizer)
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(seen["model"], "MiniMax-M3.1-Flash")
        self.assertIn("session:", seen["prompt"])
        self.assertEqual(result.summary, "decisions:\n- keep the parser")


class EventTests(unittest.TestCase):
    def test_the_run_log_record_has_the_token_counts_and_the_summary_lines(self):
        context = long_context()
        policy = compaction.CompactionPolicy(max_context_tokens=1000, keep_recent_tokens=100,
                                              keep_recent_messages=2)
        result = compaction.compact_context(context, policy)
        event = result.event()
        self.assertEqual(event["type"], "context_compacted")
        self.assertEqual(event["tokens_before"], result.tokens_before)
        self.assertEqual(event["tokens_after"], result.tokens_after)
        self.assertEqual(event["summary_lines"], result.summary_lines)
        self.assertGreater(event["summary_lines"], 0)
        self.assertEqual(event["threshold"], policy.max_context_tokens)
        self.assertFalse(event["failed"])
        for field in ("tokens_before", "tokens_after", "summary_lines", "threshold", "summary_model",
                      "applied", "mode", "kept_decisions", "kept_files", "kept_todos"):
            self.assertIn(field, event)

    def test_a_failed_compaction_is_recorded_as_a_failure(self):
        context = long_context()
        policy = compaction.CompactionPolicy(max_context_tokens=1000, keep_recent_tokens=100,
                                              keep_recent_messages=2)
        result = compaction.compact_context(context, policy,
                                            summarizer=lambda messages, policy: (_ for _ in ()).throw(RuntimeError("boom")))
        event = result.event()
        self.assertEqual(event["type"], "context_compaction_failed")
        self.assertTrue(event["failed"])
        self.assertEqual(event["tokens_before"], event["tokens_after"])
        self.assertEqual(event["summary_lines"], 0)
        self.assertIn("boom", event["error"])


class LongSessionTests(unittest.TestCase):
    def test_a_200k_token_session_keeps_its_decisions_and_its_files(self):
        policy = compaction.CompactionPolicy(max_context_tokens=120_000, keep_recent_tokens=24_000,
                                             keep_recent_messages=12)
        context = long_context()
        self.assertGreaterEqual(context.tokens, 200_000)
        result = compaction.compact_context(context, policy)
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertLessEqual(result.tokens_after, policy.max_context_tokens)
        self.assertIn("parser.py", result.summary)
        self.assertIn("the parser stays in parser.py", result.summary)
        self.assertIn("finish the edge case", result.summary)
        self.assertNotIn("o" * 100, result.summary, "the repeated tool output has to be dropped")
        context.apply(result)
        self.assertLessEqual(context.tokens, policy.max_context_tokens)
        self.assertEqual(context.compaction_count, 1)

    def test_a_200k_token_session_keeps_every_decision_and_every_file(self):
        """Every decision and every changed file of a long session has to survive (Refs #1716)."""
        policy = compaction.CompactionPolicy(max_context_tokens=120_000, keep_recent_tokens=24_000,
                                             keep_recent_messages=12)
        context = long_context(rounds=110, distinct=True)
        self.assertGreaterEqual(context.tokens, 200_000)
        result = compaction.compact_context(context, policy)
        self.assertTrue(result.triggered)
        self.assertFalse(result.failed)
        self.assertEqual(result.omitted.get("decisions", 0), 0)
        self.assertEqual(result.omitted.get("files_changed", 0), 0)
        self.assertEqual(result.omitted.get("todos", 0), 0)
        self.assertGreaterEqual(len(result.facts["decisions"]), 100)
        for decision in result.facts["decisions"]:
            self.assertIn(decision, result.summary)
        for name in result.facts["files_changed"]:
            self.assertIn(name, result.summary)
        for todo in result.facts["todos"]:
            self.assertIn(todo, result.summary)
        self.assertIn("step 0", result.summary)
        self.assertIn("parser_0.py", result.summary)
        self.assertLessEqual(result.tokens_after, policy.max_context_tokens)
        # The rounds that were kept verbatim are still in the context of the session.
        context.apply(result)
        kept = "\n".join(message.text for message in context.messages)
        self.assertIn("step 109", kept)

    def test_a_second_compaction_keeps_the_decisions_of_the_first_one(self):
        policy = compaction.CompactionPolicy(max_context_tokens=50_000, keep_recent_tokens=1000,
                                             keep_recent_messages=2)
        context = long_context(rounds=40, distinct=True)
        first = compaction.compact_context(context, policy)
        self.assertTrue(first.triggered)
        context.apply(first)
        # The new work of the session: it falls into the compacted part of the next round.
        context.add({"role": "assistant", "text": "Decision: the last decision of the session"})
        context.add({"role": "assistant", "kind": "tool_call", "text": "edit last.py",
                     "files": ["last.py"]})
        for index in range(6):
            context.add({"role": "assistant", "text": "the parser is written (%d)" % index})
        context.note_tokens(context.last_compaction_tokens + policy.min_growth_tokens + 1)
        second = compaction.compact_context(context, policy)
        self.assertTrue(second.triggered)
        self.assertIn("the last decision of the session", second.summary)
        self.assertIn("last.py", second.summary)
        self.assertIn("step 0", second.summary, "the decisions of the first compaction have to survive")
        self.assertIn("parser_0.py", second.summary, "the files of the first compaction have to survive")


class ContextEventTests(unittest.TestCase):
    """The executor turns the backend events into session context messages."""

    def test_pi_messages_are_normalized(self):
        lane = {"backend": "pi"}
        message = xs.lane_context_message(lane, {"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "done"}]}})
        self.assertEqual((message["role"], message["kind"], message["text"]), ("assistant", "text", "done"))
        tool = xs.lane_context_message(lane, {"type": "tool_execution_start", "toolName": "edit",
                                              "args": {"path": "lib/session/compaction.py"}})
        self.assertEqual(tool["kind"], "tool_call")
        self.assertEqual(tool["files"], ["lib/session/compaction.py"])

    def test_codex_messages_are_normalized(self):
        lane = {"backend": "codex"}
        message = xs.lane_context_message(lane, {"method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": "done"}}})
        self.assertEqual(message["text"], "done")
        change = xs.lane_context_message(lane, {"method": "item/completed", "params": {
            "item": {"type": "fileChange", "changes": [{"path": "parser.py"}]}}})
        self.assertEqual(change["files"], ["parser.py"])

    def test_acp_messages_are_normalized(self):
        lane = {"backend": "cursor"}
        message = xs.lane_context_message(lane, {"method": "session/update", "params": {
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello"}}}})
        self.assertEqual((message["role"], message["text"]), ("assistant", "hello"))
        self.assertIsNone(xs.lane_context_message(lane, {"method": "session/update", "params": {
            "update": {"sessionUpdate": "plan", "entries": []}}}))

    def test_the_reported_context_size_is_read(self):
        self.assertEqual(xs.lane_context_tokens({"backend": "pi"}, {
            "type": "message_end", "message": {"usage": {"input": 100, "cacheRead": 20, "output": 5}}}),
            120)
        self.assertEqual(xs.lane_context_tokens({"backend": "codex"}, {
            "method": "turn/completed", "params": {"usage": {"input_tokens": 900, "cached_input_tokens": 100}}}),
            1000)
        self.assertIsNone(xs.lane_context_tokens({"backend": "pi"}, {"type": "agent_end"}))

    def test_a_compaction_event_is_not_the_size_of_the_current_context(self):
        """pi reports the size a compaction replaced, not the size the session has now."""
        self.assertIsNone(xs.lane_context_tokens({"backend": "pi"}, {
            "type": "compaction_end", "result": {"tokensBefore": 150000}}))


class ExecutorCompactionTests(unittest.TestCase):
    """The lane turn loop compacts the session and writes the record to the run log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="os-session-compaction-")
        self.rdir = pathlib.Path(self.tmp.name)
        self.event_file = str(self.rdir / "events.1.pi.jsonl")
        self.policy = compaction.CompactionPolicy(max_context_tokens=1000, keep_recent_tokens=100,
                                                   keep_recent_messages=2)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        path = pathlib.Path(self.event_file)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_a_compaction_is_written_to_the_run_log(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "Decision: keep the parser in parser.py"})
        context.note_tokens(5000)
        result = xs.session_context_compact({}, context, self.policy, self.event_file)
        self.assertIsNotNone(result)
        self.assertFalse(result.failed)
        records = [event for event in self.events() if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tokens_before"], 5000)
        self.assertLess(records[0]["tokens_after"], 5000)
        self.assertGreater(records[0]["summary_lines"], 0)
        status = json.loads((self.rdir / "status.json").read_text())
        self.assertEqual(status["context_compaction_tokens"], records[0]["tokens_after"])

    def test_a_failed_compaction_keeps_the_context_and_the_run(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "Decision: keep the parser in parser.py"})
        context.note_tokens(5000)
        before = len(context.messages)

        def broken(messages, policy):
            raise RuntimeError("no summary model")

        result = xs.session_context_compact({}, context, self.policy, self.event_file, summarizer=broken)
        self.assertTrue(result.failed)
        self.assertEqual(len(context.messages), before)
        self.assertIsNone(context.summary)
        records = [event for event in self.events() if event.get("type") == "context_compaction_failed"]
        self.assertEqual(len(records), 1)
        self.assertIn("no summary model", records[0]["error"])
        status = json.loads((self.rdir / "status.json").read_text())
        self.assertEqual(status["context_compaction_failed"], records[0]["error"])

    def test_a_small_session_is_not_compacted(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "short"})
        self.assertIsNone(xs.session_context_compact({}, context, self.policy, self.event_file))
        self.assertEqual(self.events(), [])

    def test_the_summary_is_handed_to_the_next_attempt(self):
        context = compaction.SessionContext()
        context.add({"role": "assistant", "text": "Decision: keep the parser in parser.py"})
        context.note_tokens(5000)
        xs.session_context_compact({}, context, self.policy, self.event_file)
        prompt = xs.session_context_prompt(context, "do the task")
        self.assertIn("keep the parser in parser.py", prompt)
        self.assertIn("do the task", prompt)
        self.assertEqual(xs.session_context_prompt(None, "do the task"), "do the task")


class FakePiSession:
    """A lane child that speaks the RPC of a pi session.

    The session keeps its messages, compacts them when the executor asks for it and reports
    the context size of the compacted session. A test can therefore see the running session
    shrink instead of only the record the executor keeps.
    """

    MODELS = [{"provider": "fake", "id": "session-model"}, {"provider": "fake", "id": "summary-model"}]

    def __init__(self, messages=40, message_tokens=250, fail_compact=False, report_size=False):
        self.lane = {"backend": "pi"}
        self.events = []
        self.sent = []
        self.pid = None
        self.pgid = None
        self.alive_flag = True
        # Every message of the session states a decision and the file it belongs to, so a
        # test can see whether the compacted session still holds them.
        self.messages = [{"role": "assistant",
                          "text": "Decision: the parser stays in parser_%d.py\n%s" % (index, "d" * (message_tokens * 4))}
                         for index in range(messages)]
        self.fail_compact = fail_compact
        # pi reports the size of the context only with a fresh assistant response, not
        # right after a compaction; a session that reports it anyway is the exception.
        self.report_size = report_size
        self.answered_since_compaction = True
        self.model = dict(self.MODELS[0])
        self.model_during_compaction = None
        self.instructions = None
        self.compaction_count = 0
        self.tokens_before_compaction = 0
        self.tokens_after_compaction = 0
        self.tokens_reported = 0
        self.aborted = False

    # -- the session itself ------------------------------------------------
    @property
    def context_tokens(self):
        return sum(compaction.estimate_tokens(message["text"]) for message in self.messages)

    def compact(self, instructions):
        """Compact the session the way pi does: a summary plus the recent messages."""
        self.model_during_compaction = dict(self.model)
        self.instructions = instructions
        self.compaction_count += 1
        tokens_before = self.context_tokens
        self.tokens_before_compaction = tokens_before
        self.answered_since_compaction = False
        # The summary of pi keeps the decisions and the changed files of the compacted part.
        dropped = self.messages[:-2]
        lines = ["summary of the compacted part: keep the decisions"]
        for message in dropped:
            lines.append("- " + message["text"].splitlines()[0])
        summary = {"role": "summary", "text": "\n".join(lines)}
        self.messages = [summary] + self.messages[-2:]
        return {"summary": summary["text"], "tokensBefore": tokens_before}

    # -- the pi RPC ---------------------------------------------------------
    def alive(self):
        return self.alive_flag

    def next_event(self, timeout=0):
        if self.events:
            return self.events.pop(0)
        return False

    def send(self, obj):
        self.sent.append(obj)
        command = obj.get("type")
        if command == "prompt":
            self.messages.append({"role": "user", "text": obj.get("message")})
            self.queue_turn()
        elif command == "abort":
            self.aborted = True

    def queue_turn(self):
        # The assistant response carries the size of the context it was sent: after a
        # compaction that is the size of the compacted session.
        self.answered_since_compaction = True
        self.tokens_after_compaction = self.context_tokens
        message = {"role": "assistant", "content": [{"type": "text", "text": json.dumps(REPORT)}],
                   "usage": {"input": self.context_tokens, "output": 10, "cacheRead": 0, "cacheWrite": 0}}
        self.events.append({"type": "message_end", "message": message})
        self.events.append({"type": "agent_end", "messages": [message]})

    def wait_response(self, req_id, timeout=30, event_file=None):
        command = None
        for obj in reversed(self.sent):
            if obj.get("id") == req_id:
                command = obj
                break
        kind = (command or {}).get("type")
        data = {}
        if kind == "get_state":
            data = {"model": dict(self.model)}
        elif kind == "get_available_models":
            data = {"models": [dict(model) for model in self.MODELS]}
        elif kind == "set_model":
            self.model = {"provider": command.get("provider"), "id": command.get("modelId")}
            data = dict(self.model)
        elif kind == "get_session_stats":
            if self.compaction_count and not self.answered_since_compaction and not self.report_size:
                # pi reports the context size only after a fresh assistant response.
                data = {"contextUsage": {"tokens": None}}
            else:
                self.tokens_reported = self.context_tokens
                data = {"contextUsage": {"tokens": self.context_tokens}}
        elif kind == "compact":
            if self.fail_compact:
                return {"id": req_id, "type": "response", "command": "compact", "success": False,
                        "error": "the summary model is unavailable"}
            data = self.compact(command.get("customInstructions"))
        return {"id": req_id, "type": "response", "command": kind, "success": True, "data": data}

    def close(self):
        self.alive_flag = False

    def stderr_text(self):
        return ""


class LaneTurnCompactionTests(unittest.TestCase):
    """A long lane turn compacts its session context and records it in the run log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="os-session-lane-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def policy(self, **overrides):
        policy = {"lane": {"stall_after_s": 0},
                  "session_compaction": {"enabled": True, "max_context_tokens": 1000,
                                         "keep_recent_tokens": 100, "keep_recent_messages": 2}}
        policy["session_compaction"].update(overrides)
        return policy

    def run_turn(self, child, policy=None):
        lane = {"name": "L1", "backend": "pi", "model": "fake-model", "provider": "fake-provider",
                "repo": str(self.root)}
        spec = {"run_id": "session-test", "task": "compact the session", "title": "session test",
                "started": xs.NOW(), "budget_minutes": 60}
        ws = {"path": str(self.root), "root": str(self.root)}
        with mock.patch.object(xs, "load_policy", return_value=policy or self.policy()):
            final, usage, error_class, rc, event_file, detail = xs.lane_run_turn(
                lane, child, spec, "do the task", ws, str(self.root), None, {}, [])
        return final, error_class, event_file

    def events(self, event_file):
        path = pathlib.Path(event_file)
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_a_long_turn_compacts_the_running_session(self):
        child = FakePiSession(messages=40, message_tokens=250)
        before = child.context_tokens
        self.assertGreater(before, 1000)
        final, error_class, event_file = self.run_turn(child)
        self.assertIsNone(error_class)
        self.assertEqual((final or {}).get("status"), "done")
        # The session itself got smaller: the model continues with the compacted context.
        self.assertEqual(child.compaction_count, 1)
        self.assertLess(child.context_tokens, before)
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["applied"], "the compaction has to reach the running session")
        self.assertEqual(records[0]["mode"], "session")
        self.assertEqual(records[0]["tokens_before"], child.tokens_before_compaction)
        self.assertLess(records[0]["tokens_after"], records[0]["tokens_before"])
        self.assertGreater(records[0]["summary_lines"], 0)
        self.assertGreaterEqual(records[0]["kept_decisions"], 0)

    def test_the_size_the_backend_reports_is_the_number_of_the_run_log(self):
        """A backend that knows the size of the compacted session gets its number logged."""
        child = FakePiSession(messages=40, message_tokens=250, report_size=True)
        final, error_class, event_file = self.run_turn(child)
        self.assertIsNone(error_class)
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tokens_source"], "backend")
        self.assertEqual(records[0]["tokens_after"], child.tokens_reported)
        self.assertLess(records[0]["tokens_after"], records[0]["tokens_before"])

    def test_the_real_size_of_the_compacted_session_reaches_the_run_log(self):
        """pi reports the size of the compacted session with the next assistant turn."""
        child = FakePiSession(messages=40, message_tokens=250)
        final, error_class, event_file = self.run_turn(child)
        self.assertIsNone(error_class)
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tokens_source"], "estimate",
                         "the backend does not report the size right after the compaction")
        self.assertGreater(records[0]["tokens_after"], 0, "the run log must not claim an empty context")
        after = [event for event in self.events(event_file)
                 if event.get("type") == "context_compaction_tokens_after"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["tokens"], child.tokens_after_compaction)
        self.assertLess(after[0]["tokens"], after[0]["tokens_before"])
        status = json.loads((self.root / "status.json").read_text())
        self.assertEqual(status["context_compaction_tokens"], after[0]["tokens"])

    def test_a_200k_token_session_shrinks_in_the_running_session(self):
        """The long session is compacted where it runs, not only in the record (Refs #1716)."""
        child = FakePiSession(messages=400, message_tokens=500)
        before = child.context_tokens
        self.assertGreaterEqual(before, 200_000)
        final, error_class, event_file = self.run_turn(
            child, self.policy(max_context_tokens=120_000, keep_recent_tokens=24_000,
                               keep_recent_messages=12))
        self.assertIsNone(error_class)
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(child.compaction_count, 1)
        self.assertLess(child.context_tokens, 120_000,
                        "the running session has to shrink below the threshold")
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["applied"])
        self.assertLess(records[0]["tokens_after"], records[0]["tokens_before"])
        # The decisions and the changed files of the compacted part are still in the session.
        session = "\n".join(message["text"] for message in child.messages)
        self.assertIn("Decision: the parser stays in parser_0.py", session)
        self.assertIn("parser_0.py", session)
        self.assertIn("parser_399.py", session)

    def test_the_session_continues_after_the_compaction(self):
        child = FakePiSession(messages=40, message_tokens=250)
        final, error_class, event_file = self.run_turn(child)
        prompts = [obj.get("message") for obj in child.sent if obj.get("type") == "prompt"]
        self.assertGreaterEqual(len(prompts), 2, "the session has to continue after the compaction")
        self.assertIn("compacted", prompts[-1])
        resumed = [event for event in self.events(event_file) if event.get("type") == "context_compaction_resume"]
        self.assertEqual(len(resumed), 1)
        self.assertTrue(child.aborted, "the agent has to be idle before the session is compacted")

    def test_a_failing_compaction_keeps_the_session_and_the_run(self):
        child = FakePiSession(messages=40, message_tokens=250, fail_compact=True)
        before = child.context_tokens
        final, error_class, event_file = self.run_turn(child)
        self.assertIsNone(error_class, "a failed compaction must not end the run")
        self.assertEqual((final or {}).get("status"), "done")
        self.assertEqual(child.compaction_count, 0)
        self.assertFalse(any(message["role"] == "summary" for message in child.messages),
                         "the session keeps its context")
        self.assertGreaterEqual(child.context_tokens, before)
        records = [event for event in self.events(event_file)
                   if event.get("type") == "context_compaction_failed"]
        self.assertEqual(len(records), 1)
        self.assertIn("the summary model is unavailable", records[0]["error"])
        self.assertFalse(records[0]["applied"])

    def test_the_configured_summary_model_writes_the_summary(self):
        child = FakePiSession(messages=40, message_tokens=250)
        final, error_class, event_file = self.run_turn(child, self.policy(summary_model="summary-model"))
        self.assertIsNone(error_class)
        self.assertEqual(child.model_during_compaction["id"], "summary-model")
        self.assertEqual(child.model["id"], "session-model", "the session model has to be restored")
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(records[0]["summary_model"], "summary-model")
        self.assertTrue(records[0]["summary_model_applied"])

    def test_an_unavailable_summary_model_falls_back_to_the_session_model(self):
        child = FakePiSession(messages=40, message_tokens=250)
        final, error_class, event_file = self.run_turn(child, self.policy(summary_model="missing-model"))
        self.assertIsNone(error_class)
        self.assertEqual(child.compaction_count, 1, "the session is compacted anyway")
        self.assertEqual(child.model_during_compaction["id"], "session-model")
        records = [event for event in self.events(event_file) if event.get("type") == "context_compacted"]
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["summary_model_applied"])
        self.assertIn("missing-model", records[0].get("warning") or "")

    def test_a_short_turn_records_no_compaction(self):
        child = FakePiSession(messages=2, message_tokens=10)
        final, error_class, event_file = self.run_turn(child)
        self.assertIsNone(error_class)
        records = [event for event in self.events(event_file)
                   if event.get("type", "").startswith("context_compact")]
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
