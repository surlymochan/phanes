"""Queued run diagnosis: the blocking layer of a waiting run is named (Refs #1687).

The replay samples are the ones of the night of 2026-11-02: a lane of gores2
that was blocked by a ghost reservation of a run that had already ended, and a
lane of gorelay that was blocked by a host limit of the coordinator topology
that was still an old value.

A ghost is only a ghost when the evidence proves it: the run ended *and* its
executor stopped.  A run whose executor still lives keeps its slot, and a run
whose liveness evidence is missing never becomes a ghost - the verdict stays
``unknown`` instead of naming a reason nobody can prove.
"""
import importlib.util
import json
import pathlib
import tempfile
import unittest

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "lib" / "dispatch" / "queued.py"
_spec = importlib.util.spec_from_file_location("queued_under_test", MODULE_PATH)
queued = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(queued)

# 2026-11-02 22:30 UTC, the sample night.
MIDNIGHT = 1793577600.0
NOW = MIDNIGHT + 22.5 * 3600


def at(hour, minute=0, second=0):
    """The timestamp of a wall clock time of the sample night."""
    return MIDNIGHT + hour * 3600 + minute * 60 + second


def reservation(host, run_id, backend="pi", provider="minimax-cn", created=None):
    """One reservation of the capacity coordinator."""
    return {"host": host, "run_id": run_id, "backend": backend, "provider": provider,
            "created": at(21) if created is None else created}


def record(run_id, host, state, started, backend="pi", provider="minimax-cn", lane=None,
           executor_alive=None):
    """One run record.

    ``executor_alive`` is the liveness evidence the host reports: ``None``
    means the host did not report it, and such a run never becomes a ghost.
    """
    return {"run_id": run_id, "host": host, "lane": lane, "backend": backend,
            "provider": provider, "state": state, "started": started,
            "concurrency_keys": [key for key in (provider, backend, "total") if key],
            "executor_alive": executor_alive}


class GhostReservationReplayTests(unittest.TestCase):
    """M17 on gores2 waited behind the reservation of a run that had ended."""

    def sample(self):
        runs = [
            record("20261102-213000-m17aa", "gores2", "queued", at(21, 30), lane="M17"),
            record("20261102-190000-ghost1", "gores2", "finished", at(19, 0), executor_alive=False),
            record("20261102-213500-live2", "gores2", "running", at(21, 35), executor_alive=True),
        ]
        reservations = [
            reservation("gores2", "20261102-190000-ghost1", created=at(19, 5)),
            reservation("gores2", "20261102-213500-live2", created=at(21, 35)),
        ]
        limits = {"total": 8, "host:gores2": 2, "pi": 4, "minimax-cn": 4}
        policies = {"gores2": {"total": 10, "pi": 4, "minimax-cn": 6}}
        return runs, reservations, limits, policies

    def test_the_ghost_reservation_is_named(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual(section["queued"], 1)
        row = section["runs"][0]
        self.assertEqual(row["run_id"], "20261102-213000-m17aa")
        self.assertEqual(row["reason"], queued.REASON_GHOST)
        self.assertIn("gores2 主机预留 2/2", row["detail"])
        self.assertIn("1 条为已结束 run 残留", row["detail"])
        self.assertIn("20261102-190000-ghost1", row["detail"])
        self.assertEqual(row["ghost_runs"], ["20261102-190000-ghost1"])
        self.assertEqual(section["by_reason"][queued.REASON_GHOST], 1)

    def test_the_reservation_of_a_live_run_is_never_a_ghost(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        ghosts = [ghost["run_id"] for ghost in section["ghosts"]]
        self.assertEqual(ghosts, ["20261102-190000-ghost1"])

    def test_a_running_run_is_never_judged_as_blocked(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual([row["run_id"] for row in section["runs"]], ["20261102-213000-m17aa"])

    def test_without_the_ghost_the_same_run_is_blocked_by_the_host_cap(self):
        """The same two reservations without the ghost are a plain host cap."""
        runs, reservations, limits, policies = self.sample()
        runs[1]["state"] = "running"  # the run behind the reservation is alive
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual(section["ghosts"], [])
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_HOST_CAP)

    def test_a_ghost_that_does_not_fill_the_cap_is_not_the_reason(self):
        """A ghost only blocks when releasing it lifts the limit of the host."""
        runs, reservations, limits, policies = self.sample()
        limits["host:gores2"] = 4  # the host has room: the ghost is not the blocker
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        row = section["runs"][0]
        self.assertNotEqual(row["reason"], queued.REASON_GHOST)
        self.assertEqual(row["ghost_runs"], [])

    def test_a_live_executor_is_never_a_ghost(self):
        """The run ended, but its executor still runs: the slot is in use."""
        runs, reservations, limits, policies = self.sample()
        runs[1]["executor_alive"] = True
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual(section["ghosts"], [])
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_HOST_CAP)

    def test_without_the_liveness_evidence_the_reason_stays_unknown(self):
        """The host did not prove that the executor stopped: no name, no guess."""
        runs, reservations, limits, policies = self.sample()
        runs[1]["executor_alive"] = None
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual(section["ghosts"], [])
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_UNKNOWN)
        self.assertIn("存活证据不足", section["runs"][0]["detail"])
        self.assertIn("20261102-190000-ghost1", section["runs"][0]["detail"])

    def test_missing_record_on_known_host_is_unknown_even_after_ttl(self):
        runs, reservations, limits, policies = self.sample()
        runs = [runs[0], runs[2]]
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gores2"}, now=NOW)
        self.assertEqual(section["ghosts"], [])
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_UNKNOWN)
        self.assertIn("存活证据不足", section["runs"][0]["detail"])

    def test_a_host_without_the_evidence_never_proves_a_ghost(self):
        """The reservation of a run the host does not report stays unproven."""
        runs, reservations, limits, policies = self.sample()
        runs = [runs[0]]
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts=set(), now=NOW)
        self.assertEqual(section["ghosts"], [])
        self.assertNotEqual(section["runs"][0]["reason"], queued.REASON_GHOST)


class StaleHostLimitReplayTests(unittest.TestCase):
    """gorelay waited for a host limit of the topology that was still old."""

    def sample(self):
        runs = [
            record("20261102-220000-relay1", "gorelay", "queued", at(22, 0), backend="agy",
                   provider=None, lane="A2"),
            record("20261102-215500-relay0", "gorelay", "running", at(21, 55), backend="agy",
                   provider=None, executor_alive=True),
        ]
        reservations = [reservation("gorelay", "20261102-215500-relay0", backend="agy",
                                    provider=None, created=at(21, 55))]
        limits = {"total": 8, "host:gorelay": 1, "agy": 2}
        policies = {"gorelay": {"total": 10, "agy": 2}}
        return runs, reservations, limits, policies

    def test_the_host_cap_is_reported_with_its_numbers(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gorelay"}, now=NOW)
        row = section["runs"][0]
        self.assertEqual(row["reason"], queued.REASON_HOST_CAP)
        self.assertIn("gorelay 主机预留 1/1", row["detail"])

    def test_a_host_limit_below_the_lane_count_is_reported_as_an_old_value(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gorelay"},
                                  lane_counts={"gorelay": 2}, now=NOW)
        self.assertIn("疑似旧值", section["runs"][0]["detail"])
        self.assertIn("lane 数 2", section["runs"][0]["detail"])

    def test_a_live_reservation_is_not_a_ghost(self):
        runs, reservations, limits, policies = self.sample()
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  policies=policies, known_hosts={"gorelay"}, now=NOW)
        self.assertEqual(section["ghosts"], [])


class ReservedRunTests(unittest.TestCase):
    """A run that already holds a reservation was admitted by the coordinator."""

    def test_the_admission_limits_never_block_a_reserved_run(self):
        runs = [record("run-queued", "gores2", "queued", at(21, 0))]
        reservations = [reservation("gores2", "run-queued", created=at(21)),
                        reservation("gores2", "live-1", created=at(21))]
        limits = {"total": 8, "host:gores2": 2, "pi": 4, "minimax-cn": 4}
        section = queued.diagnose(runs, reservations=reservations, limits=limits,
                                  known_hosts={"gores2"}, now=NOW)
        row = section["runs"][0]
        self.assertTrue(row["reserved"])
        self.assertNotEqual(row["reason"], queued.REASON_HOST_CAP)
        self.assertNotEqual(row["reason"], queued.REASON_FLEET_CAP)
        self.assertNotEqual(row["reason"], queued.REASON_GHOST)
        self.assertIn("已持有协调器预留", row["detail"])


class LayerTests(unittest.TestCase):
    """Every layer of the admission path gets its own verdict."""

    def queued_run(self, host="gocc", backend="pi", provider="minimax-cn"):
        return [record("run-queued", host, "queued", at(21, 0), backend=backend, provider=provider)]

    def test_a_fresh_queue_is_not_reported(self):
        runs = [record("run-fresh", "gocc", "queued", NOW - 300)]
        section = queued.diagnose(runs, reservations=[], limits={}, policies={}, now=NOW)
        self.assertEqual(section["queued"], 0)
        self.assertEqual(section["runs"], [])
        self.assertFalse(section["needs_human"])

    def test_the_total_budget_of_the_fleet_blocks(self):
        reservations = [reservation("gocc", f"live-{index}", created=at(21)) for index in range(8)]
        section = queued.diagnose(self.queued_run() + [record(row["run_id"], "gocc", "running", at(21))
                                                       for row in reservations], reservations=reservations,
                                  limits={"total": 8, "pi": 4}, now=NOW)
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_FLEET_CAP)
        self.assertIn("全舰队预留 8/8", section["runs"][0]["detail"])

    def test_the_backend_budget_of_the_fleet_blocks(self):
        reservations = [reservation("gocc", f"live-{index}", created=at(21)) for index in range(4)]
        section = queued.diagnose(self.queued_run() + [record(row["run_id"], "gocc", "running", at(21))
                                                       for row in reservations], reservations=reservations,
                                  limits={"total": 8, "pi": 4}, now=NOW)
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_FLEET_CAP)
        self.assertIn("pi", section["runs"][0]["detail"])

    def test_the_provider_budget_of_the_fleet_blocks(self):
        reservations = [reservation("gocc", f"live-{index}", created=at(21)) for index in range(4)]
        section = queued.diagnose(self.queued_run() + [record(row["run_id"], "gocc", "running", at(21))
                                                       for row in reservations], reservations=reservations,
                                  limits={"total": 8, "minimax-cn": 4}, now=NOW)
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_FLEET_CAP)
        self.assertIn("minimax-cn", section["runs"][0]["detail"])

    def test_the_local_policy_of_the_host_blocks(self):
        runs = self.queued_run() + [record("live-1", "gocc", "running", at(21, 30)),
                                    record("live-2", "gocc", "running", at(21, 30))]
        section = queued.diagnose(runs, reservations=[], limits={},
                                  policies={"gocc": {"total": 10, "pi": 2}}, now=NOW)
        self.assertEqual(section["runs"][0]["reason"], queued.REASON_LOCAL_POLICY)
        self.assertIn("gocc 本机 policy 并发 2/2", section["runs"][0]["detail"])


class InputTests(unittest.TestCase):
    """The loaders of the module."""

    def test_the_reservations_of_the_coordinator_are_read(self):
        answer = {"ok": True, "reservations": [
            {"host": "gocc", "run_id": "run-1", "backend": "pi", "provider": "minimax-cn",
             "created": 1700000000}]}
        rows = queued.load_reservations(answer)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "gocc")
        self.assertEqual(rows[0]["run_id"], "run-1")

    def test_the_limits_of_the_coordinator_are_read(self):
        self.assertEqual(queued.load_limits({"limits": {"host:gocc": 4}})["host:gocc"], 4)
        topology = {"hosts": ["gocc"], "limits": {"total": 8, "host:gocc": 2}}
        self.assertEqual(queued.load_limits(None, topology)["host:gocc"], 2)
        self.assertEqual(queued.load_limits({"limits": {"total": "x"}}), {})

    def test_the_snapshot_of_a_host_becomes_run_records(self):
        snapshot = {"active_runs": [{"id": "run-1", "state": "queued", "backend": "pi",
                                     "provider": "minimax-cn", "elapsed_min": 35.0,
                                     "concurrency_keys": ["minimax-cn", "pi", "total"]}]}
        records = queued.records_from_snapshot("gores2", snapshot, NOW)
        self.assertEqual(records[0]["host"], "gores2")
        self.assertEqual(records[0]["state"], "queued")
        self.assertAlmostEqual(NOW - records[0]["started"], 35 * 60, places=3)

    def test_the_ended_runs_of_a_snapshot_carry_the_liveness_evidence(self):
        snapshot = {"active_runs": [], "ended_runs": [
            {"id": "run-old", "state": "finished", "executor_alive": False},
            {"id": "run-live", "state": "dead", "executor_alive": True}]}
        self.assertTrue(queued.snapshot_has_liveness(snapshot))
        records = queued.records_from_snapshot("gores2", snapshot, NOW)
        self.assertEqual([row["run_id"] for row in records], ["run-old", "run-live"])
        self.assertEqual(queued.liveness(records[0]), queued.LIVENESS_GONE)
        self.assertEqual(queued.liveness(records[1]), queued.LIVENESS_ALIVE)

    def test_a_snapshot_without_the_ended_runs_proves_nothing(self):
        snapshot = {"active_runs": []}
        self.assertFalse(queued.snapshot_has_liveness(snapshot))
        self.assertEqual(queued.records_from_snapshot("gores2", snapshot, NOW), [])

    def test_the_lanes_of_a_lanes_file_name_their_host(self):
        lanes = {"lanes": [{"name": "M17", "host": "gores2"}, {"name": "A2", "host": "gorelay"}]}
        hosts, counts = queued.lane_hosts(lanes)
        self.assertEqual(hosts, {"M17": "gores2", "A2": "gorelay"})
        self.assertEqual(counts, {"gores2": 1, "gorelay": 1})

    def test_the_caps_of_a_policy_are_read(self):
        self.assertEqual(queued.policy_caps({"concurrency": {"total": 10, "pi": 4}}), {"total": 10, "pi": 4})
        self.assertEqual(queued.policy_caps({"concurrency": {"pi": 0}}), {})
        self.assertEqual(queued.policy_caps(None), {})

    def test_the_local_states_measure_the_liveness_of_the_executor(self):
        states = {"run-1": {"id": "run-1", "state": "finished", "runner_pid": 999999999,
                            "executor_pid": None}}
        records = queued.records_from_states(states, host="gocc")
        self.assertEqual(records[0]["executor_alive"], False)
        self.assertEqual(queued.liveness(records[0]), queued.LIVENESS_GONE)


class OutputTests(unittest.TestCase):
    """The section is written into health.json and into the brain log."""

    def section(self):
        return queued.diagnose(
            [record("run-queued", "gocc", "queued", at(21, 0)),
             record("live-1", "gocc", "running", at(21))],
            reservations=[reservation("gocc", "live-1", created=at(21))],
            limits={"total": 1}, policies={}, now=NOW)

    def test_the_health_snapshot_keeps_the_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "health.json"
            path.write_text(json.dumps({"at": "2026-11-02 22:00:00", "hosts": {"gocc": "ok"}}))
            self.assertTrue(queued.write_health_section(path, self.section()))
            payload = json.loads(path.read_text())
        self.assertEqual(payload["hosts"], {"gocc": "ok"})
        self.assertEqual(payload["queued"]["queued"], 1)
        self.assertEqual(payload["queued"]["runs"][0]["reason"], queued.REASON_FLEET_CAP)

    def test_an_empty_section_removes_a_stale_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "health.json"
            path.write_text(json.dumps({"hosts": {}}))
            queued.write_health_section(path, self.section())
            empty = queued.diagnose([], reservations=[], limits={}, policies={}, now=NOW)
            queued.write_health_section(path, empty)
            payload = json.loads(path.read_text())
        self.assertNotIn("queued", payload)

    def test_the_brain_log_names_every_blocked_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "log.md"
            lines = queued.brain_log_lines(self.section(), NOW)
            queued.append_brain_log(path, self.section())
            written = path.read_text().splitlines()
        self.assertEqual(len(written), len(lines))
        self.assertIn("run-queued", written[0])
        self.assertIn(queued.REASON_FLEET_CAP, written[0])
        self.assertIn("UTC+8 queued:", written[0])

    def test_without_a_blocked_run_the_brain_log_stays_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "log.md"
            empty = queued.diagnose([], reservations=[], limits={}, policies={}, now=NOW)
            self.assertEqual(queued.append_brain_log(path, empty), 0)
            self.assertFalse(path.exists())

    def test_the_note_of_a_human_hint_reaches_the_brain_log(self):
        section = queued.diagnose([record(f"run-{index}", "gocc", "queued", at(21, 0)) for index in range(2)],
                                   reservations=[], limits={}, policies={}, now=NOW)
        lines = queued.brain_log_lines(section, NOW)
        self.assertTrue(any("需人工排查" in line for line in lines))


class CommandTests(unittest.TestCase):
    """The CLI replays a sample and writes the section into both files."""

    def test_the_cli_writes_the_health_section_and_the_brain_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runs = root / "runs"
            (runs / "20261102-213000-m17aa").mkdir(parents=True)
            (runs / "20261102-213000-m17aa" / "status.json").write_text(json.dumps(
                {"id": "20261102-213000-m17aa", "state": "queued", "lane": "M17", "backend": "pi",
                 "provider": "minimax-cn", "started": at(21, 30)}))
            (runs / "20261102-190000-ghost1").mkdir(parents=True)
            (runs / "20261102-190000-ghost1" / "status.json").write_text(json.dumps(
                {"id": "20261102-190000-ghost1", "state": "finished", "backend": "pi",
                 "provider": "minimax-cn", "started": at(19, 0)}))
            (runs / "20261102-213500-live2").mkdir(parents=True)
            (runs / "20261102-213500-live2" / "status.json").write_text(json.dumps(
                {"id": "20261102-213500-live2", "state": "running", "backend": "pi",
                 "provider": "minimax-cn", "started": at(21, 35)}))
            reservations = root / "reservations.json"
            reservations.write_text(json.dumps({"ok": True, "reservations": [
                {"host": "gores2", "run_id": "20261102-190000-ghost1", "backend": "pi",
                 "provider": "minimax-cn", "created": at(19, 5)},
                {"host": "gores2", "run_id": "20261102-213500-live2", "backend": "pi",
                 "provider": "minimax-cn", "created": at(21, 35)}],
                "limits": {"total": 8, "host:gores2": 2, "pi": 4, "minimax-cn": 4}}))
            health = root / "health.json"
            health.write_text(json.dumps({"hosts": {"gores2": "ok"}}))
            brain = root / "log.md"
            code = queued.main(["--runs", str(runs), "--host", "gores2",
                                "--reservations", str(reservations), "--now", str(NOW),
                                "--health-json", str(health), "--brain-log", str(brain), "--json"])
            payload = json.loads(health.read_text())
            log = brain.read_text()
        self.assertEqual(code, 0)
        self.assertEqual(payload["hosts"], {"gores2": "ok"})
        self.assertEqual(payload["queued"]["runs"][0]["reason"], queued.REASON_GHOST)
        self.assertIn("20261102-190000-ghost1", log)

    def test_the_cli_keeps_a_health_snapshot_without_a_queued_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runs = root / "runs"
            (runs / "run-fresh").mkdir(parents=True)
            (runs / "run-fresh" / "status.json").write_text(json.dumps(
                {"id": "run-fresh", "state": "queued", "started": NOW - 120}))
            health = root / "health.json"
            health.write_text(json.dumps({"hosts": {}}))
            queued.main(["--runs", str(runs), "--now", str(NOW), "--health-json", str(health), "--json"])
            payload = json.loads(health.read_text())
        self.assertNotIn("queued", payload)


if __name__ == "__main__":
    unittest.main()
