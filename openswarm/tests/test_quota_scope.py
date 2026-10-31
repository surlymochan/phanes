"""Quota/rate-limit breaker scope and unknown-state handling (Phanes issue #21)."""
import copy, json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
POLICY = xs.jload(f"{xs.SKILL_DIR}/policy.json", {})
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)
PROF = {"type": "impl", "difficulty": 3}


class BreakerKeyScope(unittest.TestCase):
    def test_codex_key_is_per_limit_id(self):
        self.assertEqual(xs.rate_limit_breaker_key("codex", {"limit_id": "codex_x"}, "429"), "codex/limit/codex_x")

    def test_ocgo_plan_wide_vs_model_scope(self):
        d = {"model": "m1"}
        self.assertEqual(xs.rate_limit_breaker_key("ocgo", d, "Usage limit exceeded"), "ocgo/all")
        self.assertEqual(xs.rate_limit_breaker_key("ocgo", d, "429 too many requests"), "ocgo/m1")

    def test_unknown_model_never_widens_beyond_backend(self):
        self.assertEqual(xs.rate_limit_breaker_key("agy", {"model": "g"}, "429"), "agy/g")
        self.assertEqual(xs.rate_limit_breaker_key("agy", {}, "429"), "agy/all")

    def test_trip_sets_expiry(self):
        xs.jsave(xs.BREAKERS, {})
        xs.trip_breaker("ocgo/m1", POLICY, "429")
        self.assertIn("ocgo/m1", xs.breakers())
        self.assertNotIn("ocgo/m2", xs.breakers())
        xs.jsave(xs.BREAKERS, {})


class UnknownState(unittest.TestCase):
    def setUp(self):
        xs.jsave(xs.BREAKERS, {})
        self.addCleanup(xs.jsave, xs.BREAKERS, {})

    def test_deepseek_unknown_balance_is_not_selected(self):
        st = copy.deepcopy(BASE)
        st["deepseek"]["balance"] = None
        d, why = xs.plan_deepseek(PROF, st, POLICY, False, [])
        self.assertIsNone(d)
        self.assertIn("unavailable", why)

    def test_deepseek_not_in_default_route_after_codex_limit(self):
        st = copy.deepcopy(BASE)
        st["deepseek"]["balance"] = 100.0
        for model in st["codex"]["models"]:
            xs.trip_breaker(f"codex/limit/{model['limit_id']}", POLICY, "429")
        r = xs.route(PROF, st, POLICY)
        self.assertFalse(r["ok"])
        self.assertNotIn("deepseek", r.get("skipped", {}))

    def test_codex_limit_breaker_blocks_only_that_limit(self):
        st = copy.deepcopy(BASE)
        limits = {m["limit_id"] for m in st["codex"]["models"]}
        blocked = sorted(limits)[0]
        xs.trip_breaker(f"codex/limit/{blocked}", POLICY, "429")
        d, _ = xs.plan_codex(PROF, st, POLICY, False, [])
        if d:
            self.assertNotEqual(d["limit_id"], blocked)

    def test_unknown_quota_opt_in_backend_respects_breaker(self):
        xs.trip_breaker("agy/gemini-x", POLICY, "429")
        st = {"agy": {"binary": "/bin/true"}}
        d, why = xs.plan_agy({**PROF, "model": "gemini-x"}, st, POLICY, True, [])
        self.assertIsNone(d)
        self.assertEqual(why, "agy breaker open")
        d, _ = xs.plan_agy({**PROF, "model": "gemini-y"}, st, POLICY, True, [])
        self.assertEqual(d["model"], "gemini-y")


if __name__ == "__main__":
    unittest.main(verbosity=2)
