#!/usr/bin/env python3
"""派发器的暂停判定：PauseStore（TTL/scope）+ 旧 PAUSE-* 标记 + 全局 PAUSE（Refs #1592）。

#1592 要求派发引擎的 `paused_lane` 在**同一次判定**里读两处暂停开关：swrm 暂停库
（`$OPENSWARM_HOME/pauses`，带 TTL 与 scope）和旧的 `PAUSE-*` 标记文件，并把**全局
PAUSE**（整个派发循环）也算进去（拆自 #1366 第 3 项）。

本文件把这条契约钉在当前基线上真正做派发判定的组件上：
`tools/lane-autodispatch/autodispatch.py`。openswarm 的 dispatch 引擎
（`openswarm/lib/dispatch/engine.py`）在 #1894「vendored openswarm 换成 swrm 单一来源」
之后已随 openswarm 目录一起删除，暂停判定留在控制面的 AD 里；#1592 的既有实现
（分支 `fleet/CX6-issue1592-20261102-082602-b247ab`，提交 9860a51f0）改的是那份引擎
代码（`pause_reason` 读暂停库 + 旧标记 + 全局 PAUSE，旧标记的 `until=` 过期后不再
停派），所以这里按同一条契约验收现状。

契约（每一条都对应 #1592 的一项验收）：
* **全局 PAUSE**：旧标记 `PAUSE` 与暂停库里的 `all` scope 都让**每一条** lane 停派；
* **暂停库的 scope**：`lane:X` / `host:H` / `backend:B` 只停匹配的 lane；
* **暂停库的 TTL**：过期记录不再是暂停；
* **旧标记**：`PAUSE-<LANE>` / `PAUSE-HOST-<HOST>` / `PAUSE-BACKEND-<BACKEND>` 停匹配的 lane；
* **旧标记自己的截止时间**：标记正文里的 `until=<deadline>`（health-loop 写的
  `auto:cooldown:until=...`）过了以后不再停派，标记文件留在原地作为证据。

基线偏差：旧标记的 `until=` 截止时间被完全忽略——一条早就过期的冷却标记会永远停掉
一条 lane，于是下面三处断言在基线上是红的（红的正是这三处，其余断言已经是绿的：
全局 PAUSE 与暂停库的 TTL/scope 早已在 #1366 里实现，本次不重写）。

验收：`python3 -m unittest discover -s tools/lane-autodispatch -p 'test_pause_scope.py' -v`
"""
import importlib.util
import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH = REPO_ROOT / ".swrm-tmp"  # gitignored scratch root
AD = REPO_ROOT / "tools" / "lane-autodispatch" / "autodispatch.py"

SPEC = importlib.util.spec_from_file_location("autodispatch", AD)
AUTODISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTODISPATCH)


class PauseHarness(unittest.TestCase):
    """The dispatcher under test, with both pause switches in a scratch directory."""

    LANES = {"CX1": {"host": "h1", "backend": "pi"},
             "CX2": {"host": "h2", "backend": "codex"}}

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="pause-scope-", dir=str(SCRATCH)))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # the legacy markers live in the state directory of the dispatcher
        self.state = self.tmp / "ad-state"
        self.state.mkdir(parents=True)
        # the pause store lives in the swrm home of the runtime
        self.home = self.tmp / "swrm-home"
        (self.home / "pauses").mkdir(parents=True)
        self.saved = {"D": AUTODISPATCH.D, "home": os.environ.get("OPENSWARM_HOME")}
        AUTODISPATCH.D = str(self.state)
        os.environ["OPENSWARM_HOME"] = str(self.home)
        self.addCleanup(self.restore)

    def restore(self):
        AUTODISPATCH.D = self.saved["D"]
        if self.saved["home"] is None:
            os.environ.pop("OPENSWARM_HOME", None)
        else:
            os.environ["OPENSWARM_HOME"] = self.saved["home"]

    def store_pause(self, scope, ttl=None, reason="operator"):
        """One pause record in the pause store (the shape the pause CLI writes)."""
        record = {"scope": scope, "created_at": time.time(),
                  "expires_at": None if ttl is None else time.time() + ttl,
                  "reason": reason}
        self.store_path(scope).write_text(json.dumps(record), encoding="utf-8")

    def store_path(self, scope):
        return self.home / "pauses" / (scope.replace(":", "_").replace("/", "_") + ".json")

    def marker(self, name, body=""):
        """One legacy PAUSE marker file in the state directory of the dispatcher."""
        (self.state / name).write_text(body, encoding="utf-8")

    def paused(self, lane, cfg=None):
        """Whether the dispatcher refuses the lane (whatever pause API it exposes).

        The decision function may be the engine style `paused_lane(lane, lc)` or the
        control plane style `lane_paused(lane, cfg, scopes)`; both answer the same
        question, so the test asks whichever one the dispatcher offers.
        """
        cfg = cfg or {"host": "h1", "backend": "pi"}
        for name in ("pause_reason", "lane_paused", "paused_lane"):
            function = getattr(AUTODISPATCH, name, None)
            if function is None:
                continue
            if name == "pause_reason":
                try:
                    return function(lane, cfg) is not None
                except TypeError:
                    continue
            scopes = getattr(AUTODISPATCH, "paused_scopes", None)
            try:
                arity = len(inspect.signature(function).parameters)
            except (TypeError, ValueError):
                arity = 2
            if arity >= 3 and scopes is not None:
                return bool(function(lane, cfg, scopes()))
            return bool(function(lane, cfg))
        self.fail("the dispatcher has no pause decision function "
                  "(paused_lane / lane_paused / pause_reason)")


class PauseScopeTests(PauseHarness):
    """The pause decision itself: which switches are read and what they pause."""

    def test_the_global_pause_marker_stops_every_lane(self):
        """The legacy `PAUSE` marker stops the whole loop, not one lane."""
        self.marker("PAUSE")
        self.assertTrue(self.paused("CX1"))
        self.assertTrue(self.paused("CX2", {"host": "h2", "backend": "codex"}))

    def test_the_global_scope_of_the_pause_store_stops_every_lane(self):
        """The `all` scope of the pause store is the same switch as the global marker."""
        self.store_pause("all")
        self.assertTrue(self.paused("CX1"))
        self.assertTrue(self.paused("CX2", {"host": "h2", "backend": "codex"}))

    def test_the_store_scopes_stop_only_the_matching_lane(self):
        """`lane:X`, `host:H` and `backend:B` are three different scopes."""
        for scope, lane, cfg in (("lane:CX1", "CX1", None),
                                 ("host:h1", "CX1", None),
                                 ("backend:pi", "CX1", None)):
            with self.subTest(scope=scope):
                self.store_pause(scope)
                self.assertTrue(self.paused(lane, cfg))
                self.assertFalse(self.paused("CX2", {"host": "h2", "backend": "codex"}))
                self.store_path(scope).unlink()

    def test_an_expired_store_record_is_not_a_pause(self):
        """The TTL of the pause store is honoured: an expired record pauses nobody."""
        self.store_pause("lane:CX1", ttl=-1)
        self.assertFalse(self.paused("CX1"))

    def test_the_legacy_markers_stop_the_matching_lane(self):
        """The old marker files keep working next to the pause store."""
        for name, lane, cfg in (("PAUSE-CX1", "CX1", None),
                                 ("PAUSE-HOST-h1", "CX1", None),
                                 ("PAUSE-BACKEND-pi", "CX1", None)):
            with self.subTest(marker=name):
                self.marker(name, "operator hold\n")
                self.assertTrue(self.paused(lane, cfg))
                self.assertFalse(self.paused("CX2", {"host": "h2", "backend": "codex"}))
                (self.state / name).unlink()

    def test_a_legacy_marker_with_a_future_deadline_still_stops_the_lane(self):
        """A cooldown that has not expired yet still holds the lane."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) + 600}\n")
        self.assertTrue(self.paused("CX1"))

    def test_a_legacy_marker_whose_deadline_passed_releases_the_lane(self):
        """A cooldown that has expired stops nobody, and the marker stays as evidence.

        The health loop writes `auto:cooldown:until=<deadline>` into the marker
        (``tools/check-health-pause.py``).  The deadline belongs to the marker: once it
        is past, the lane takes new work again.  A marker without a deadline (a manual
        hold) stays a pause forever.
        """
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        self.assertFalse(self.paused("CX1"),
                         "an expired cooldown must not stop the lane any more")
        self.assertTrue((self.state / "PAUSE-BACKEND-pi").exists(),
                        "the expired marker stays on disk as evidence")

    def test_a_legacy_marker_with_a_passed_iso_deadline_releases_the_lane(self):
        """The same deadline written as an ISO timestamp (the other field format)."""
        self.marker("PAUSE-HOST-h1", "auto:cooldown:until=2020-01-01T00:00:00+00:00\n")
        self.assertFalse(self.paused("CX1"),
                         "an expired cooldown must not stop the lane any more")


class TickPauseTests(PauseHarness):
    """The pause decision really gates the dispatch (one tick, everything else faked)."""

    # one lane of the `pi` backend: the backend pause of the tick tests hits this lane
    LANES = {"CX1": {"host": "h1", "backend": "pi"}}
    ISSUES = [{"number": 12, "title": "card", "body": "", "labels": ["lane-ready"]}]

    def run_tick(self):
        """One tick with GitHub, ssh and the dispatch faked; returns the dispatched pairs."""
        dispatched, logs = [], []

        def fake_gh(args, timeout=60):
            if args[:2] == ["issue", "list"]:
                return 0, json.dumps(self.ISSUES), ""
            if args[:2] == ["issue", "view"]:
                number = int(args[2])
                for row in self.ISSUES:
                    if row["number"] == number:
                        return 0, json.dumps({"number": number, "state": "OPEN",
                                               "labels": row["labels"]}), ""
                return 0, json.dumps({"number": number, "state": "CLOSED", "labels": []}), ""
            return 0, "", ""

        def fake_dispatch(lane, cfg, item, base):
            dispatched.append((lane, item["n"]))
            return "run-1", None

        patched = ("D", "gh", "sh", "log", "host_ok", "host_fail", "host_good", "remote",
                   "deps_met", "dispatch", "replenish", "save_state", "status_snapshot")
        saved = {name: getattr(AUTODISPATCH, name) for name in patched}
        try:
            AUTODISPATCH.gh = fake_gh
            AUTODISPATCH.sh = lambda *a, **k: (0, "0" * 40 + "\n", "")
            AUTODISPATCH.log = lambda msg, brain=True: logs.append(msg)
            AUTODISPATCH.host_ok = lambda host: True
            AUTODISPATCH.host_fail = lambda host, why: None
            AUTODISPATCH.host_good = lambda host: None
            AUTODISPATCH.remote = lambda lane, cfg, run="": {"lane": "idle"}
            AUTODISPATCH.deps_met = lambda item: True
            AUTODISPATCH.dispatch = fake_dispatch
            AUTODISPATCH.replenish = lambda k, state: []
            AUTODISPATCH.save_state = lambda state: None
            AUTODISPATCH.status_snapshot = lambda *a, **k: {}
            AUTODISPATCH.VERIFY_REJECT_LOGGED.clear()
            # tick() writes lanes-status.json without closing it: the warning is pre-existing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                AUTODISPATCH.tick(dict(self.LANES), {}, 0)
        finally:
            for name, value in saved.items():
                setattr(AUTODISPATCH, name, value)
        return dispatched, logs

    def test_a_free_lane_takes_the_card(self):
        """The baseline of the tick harness: without a pause the lane works."""
        dispatched, _ = self.run_tick()
        self.assertEqual(dispatched, [("CX1", 12)])

    def test_the_global_pause_stops_the_whole_loop(self):
        """The global PAUSE marker keeps every lane idle."""
        self.marker("PAUSE")
        dispatched, _ = self.run_tick()
        self.assertEqual(dispatched, [])

    def test_an_expired_cooldown_lets_the_lane_take_a_card_again(self):
        """A cooldown that expired must not keep the lane idle for good."""
        self.marker("PAUSE-BACKEND-pi", f"auto:cooldown:until={int(time.time()) - 60}\n")
        dispatched, _ = self.run_tick()
        self.assertEqual(dispatched, [("CX1", 12)],
                         "the expired cooldown still holds the lane")


if __name__ == "__main__":
    unittest.main()
