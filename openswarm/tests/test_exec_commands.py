"""Pure unit tests for event command extraction and shell parsing."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402


xs = _load.load()


class EventCommandsTests(unittest.TestCase):
    def test_supported_event_shapes_and_nested_json_inputs(self):
        events = [
            ({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call", "toolCall": {"command": "one"},
            }}}, "one"),
            ({"method": "session/update", "params": {"update": {
                "sessionUpdate": "tool_call_update", "toolCall": {"command": "two"},
            }}}, "two"),
            ({"method": "item/commandExecution/requestApproval", "params": {
                "command": "three",
            }}, "three"),
            ({"type": "tool_call", "command": "four"}, "four"),
            ({"type": "tool_use", "command": "five"}, "five"),
            ({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "input": {"command": "six"}},
            ]}}, "six"),
        ]
        for event, expected in events:
            with self.subTest(event=event):
                commands = xs.event_commands(event)
                self.assertIn(expected, commands)
                if event.get("method") == "session/update":
                    self.assertEqual(commands, ["tool", expected])
                else:
                    self.assertEqual(commands, [expected])

    def test_unsupported_session_update_is_empty(self):
        event = {"method": "session/update", "params": {"update": {
            "sessionUpdate": "agent_message_chunk", "content": {"text": "not a command"},
        }}}
        self.assertEqual(xs.event_commands(event), [])

    def test_json_string_inputs_are_parsed_and_results_are_deduplicated(self):
        event = {"type": "tool_call", "arguments": (
            '[{"command":"first"}, {"rawInput":"{\\"command\\":\\"second\\"}"}, '
            '{"input":"{\\"command\\":\\"first\\"}"}, '
            '{"args":{"command":"third"}}]'
        )}
        self.assertEqual(xs.event_commands(event), ["first", "second", "third"])


class ShellInvocationsTests(unittest.TestCase):
    def test_splits_commands_and_preserves_quoted_semicolons(self):
        self.assertEqual(
            list(xs.shell_invocations("a; b", "/work")),
            [(["a"], "/work"), (["b"], "/work")],
        )
        self.assertEqual(
            list(xs.shell_invocations("printf 'a;b'", "/work")),
            [(["printf", "a;b"], "/work")],
        )

    def test_cd_updates_following_command_cwd(self):
        result = list(xs.shell_invocations("cd /tmp && ls", "/work"))
        self.assertEqual(result[0], (["cd", "/tmp"], "/work"))
        self.assertEqual(result[1], (["ls"], os.path.realpath("/tmp")))

    def test_shell_c_body_is_recursed_and_prefixes_are_skipped(self):
        self.assertEqual(
            list(xs.shell_invocations('sh -c "rm -rf /"', "/work")),
            [(["sh", "-c", "rm -rf /"], "/work"), (["rm", "-rf", "/"], "/work")],
        )
        self.assertEqual(
            list(xs.shell_invocations("FOO=1 env ls", "/work")),
            [(["ls"], "/work")],
        )

    def test_recursion_stops_after_depth_sixteen(self):
        self.assertEqual(list(xs.shell_invocations("echo ok", "/work", depth=17)), [])
        command = "sh -c 'sh -c \\\"echo ok\\\"'"
        self.assertEqual(len(list(xs.shell_invocations(command, "/work", depth=16))), 1)


if __name__ == "__main__":
    unittest.main()
