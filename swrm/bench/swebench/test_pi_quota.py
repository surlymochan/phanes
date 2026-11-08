"""Pi backend quota: the provider balance is read where one exists and shown in `openswarm status`.

No network: the balance endpoint is faked.
"""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "tests"))
import _load  # noqa: E402

xs = _load.load()
DEMO_STATE = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "examples" / "demo-state.json")
POLICY = xs.load_policy()

BALANCE = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "12.50"}]}
MULTI_BALANCE = {"is_available": True,
                 "balance_infos": [{"currency": "CNY", "total_balance": "0.00"},
                                   {"currency": "USD", "total_balance": "12.50"}]}


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload


def policy(provider="deepseek", **pi):
    base = {"provider": provider, "default_model": "deepseek-flash", "require_isolation": False}
    base.update(pi)
    return {"pi": base}


class PiQuotaTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self._env

    def test_probe_reads_the_provider_balance(self):
        with mock.patch.object(xs.urllib.request, "urlopen", return_value=FakeResponse(BALANCE)) as urlopen:
            state = xs.probe_pi(policy())

        self.assertEqual(state["quota"], {"balance": 12.5, "currency": "USD", "available": True,
                                          "balances": [{"currency": "USD", "balance": 12.5}]})
        self.assertIn("api.deepseek.com/user/balance", urlopen.call_args[0][0].full_url)

    def test_probe_reads_every_currency_of_the_provider_balance(self):
        with mock.patch.object(xs.urllib.request, "urlopen", return_value=FakeResponse(MULTI_BALANCE)):
            state = xs.probe_pi(policy())

        self.assertEqual(state["quota"]["balances"], [{"currency": "CNY", "balance": 0.0},
                                                       {"currency": "USD", "balance": 12.5}])
        self.assertEqual(state["quota"]["balance"], 12.5)
        self.assertEqual(state["quota"]["currency"], "USD")

    def test_probe_without_a_key_keeps_the_quota_unknown(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with mock.patch.object(xs.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            state = xs.probe_pi(policy())

        self.assertIsNone(state["quota"])
        self.assertIn("DEEPSEEK_API_KEY", state["quota_error"])

    def test_probe_keeps_the_quota_unknown_for_providers_without_an_endpoint(self):
        with mock.patch.object(xs.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            state = xs.probe_pi(policy(provider="minimax-cn"))

        self.assertIsNone(state["quota"])
        self.assertNotIn("quota_error", state)

    def test_status_shows_the_pi_balance(self):
        st = self.demo_state()
        st["pi"] = {"binary": "/usr/bin/pi", "default_model": "deepseek-flash",
                    "quota": {"balance": 12.5, "currency": "USD", "available": True}}
        output = io.StringIO()
        with mock.patch.object(xs, "get_state", return_value=st), \
                mock.patch.object(xs, "claude_mode", return_value=("light", "fixture")), \
                contextlib.redirect_stdout(output):
            xs.cmd_status(POLICY, False)

        self.assertIn("balance=12.5 USD", output.getvalue())

    def test_status_shows_an_unknown_pi_quota(self):
        st = self.demo_state()
        st["pi"] = {"binary": "/usr/bin/pi", "default_model": "deepseek-flash"}
        output = io.StringIO()
        with mock.patch.object(xs, "get_state", return_value=st), \
                mock.patch.object(xs, "claude_mode", return_value=("light", "fixture")), \
                contextlib.redirect_stdout(output):
            xs.cmd_status(POLICY, False)

        self.assertIn("quota=unknown", output.getvalue())

    def demo_state(self):
        with open(DEMO_STATE) as stream:
            return json.load(stream)

    def test_routing_spills_off_an_exhausted_pi_provider(self):
        st = {"pi": {"binary": "/usr/bin/pi", "default_model": "deepseek-flash",
                     "quota": {"balance": 0.0, "currency": "USD", "available": False}}}
        notes = []
        with mock.patch.object(xs, "pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(xs, "breakers", return_value={}):
            decision, why = xs.plan_pi({"type": "impl", "difficulty": 3, "model": None},
                                       st, self.policy(), False, notes)

        self.assertIsNone(decision)
        self.assertEqual(why, "pi provider balance exhausted")

    def test_routing_uses_a_pi_provider_with_balance(self):
        st = {"pi": {"binary": "/usr/bin/pi", "default_model": "deepseek-flash",
                     "quota": {"balance": 12.5, "currency": "USD", "available": True}}}
        notes = []
        with mock.patch.object(xs, "pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(xs, "breakers", return_value={}):
            decision, why = xs.plan_pi({"type": "impl", "difficulty": 3, "model": None},
                                       st, self.policy(), False, notes)

        self.assertEqual(decision["backend"], "pi")
        self.assertIsNone(why)
        self.assertIn("pi provider balance 12.5 USD", notes)

    def test_routing_uses_a_pi_provider_funded_in_another_currency(self):
        st = {"pi": {"binary": "/usr/bin/pi", "default_model": "deepseek-flash",
                     "quota": {"balance": 12.5, "currency": "USD", "available": True,
                               "balances": [{"currency": "CNY", "balance": 0.0},
                                            {"currency": "USD", "balance": 12.5}]}}}
        notes = []
        with mock.patch.object(xs, "pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(xs, "breakers", return_value={}):
            decision, why = xs.plan_pi({"type": "impl", "difficulty": 3, "model": None},
                                       st, self.policy(), False, notes)

        self.assertEqual(decision["backend"], "pi")
        self.assertIsNone(why)
        self.assertIn("pi provider balance 12.5 USD", notes)

    def test_routing_spills_off_a_pi_provider_without_any_funded_currency(self):
        st = {"pi": {"binary": "/usr/bin/pi", "default_model": "deepseek-flash",
                     "quota": {"balance": 0.0, "currency": "CNY", "available": True,
                               "balances": [{"currency": "CNY", "balance": 0.0},
                                            {"currency": "USD", "balance": 0.0}]}}}
        notes = []
        with mock.patch.object(xs, "pi_bin", return_value="/usr/bin/pi"), \
                mock.patch.object(xs, "breakers", return_value={}):
            decision, why = xs.plan_pi({"type": "impl", "difficulty": 3, "model": None},
                                       st, self.policy(), False, notes)

        self.assertIsNone(decision)
        self.assertEqual(why, "pi provider balance exhausted")

    def policy(self):
        full = dict(POLICY)
        full["pi"] = policy()["pi"]
        return full


if __name__ == "__main__":
    unittest.main()
