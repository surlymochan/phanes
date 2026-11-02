"""`openswarm/lib/transport/websocket.py`: the websocket/stream dispatch
channel for executors with no public or stable IP (Refs #1483).

The module is loaded by path: `openswarm/lib/transport/` is a namespace
package without an `__init__.py`, so it must not be imported as a package.

Coverage asked for by the card: disconnect/reconnect, duplicate-delivery
idempotency, and unauthorized rejection -- plus the end-to-end PoC of a
fake executor with no inbound port claiming a test card over a real socket
and returning an Evidence pointer.
"""

import importlib.util
import pathlib
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
MODULE = HERE.parent / "lib" / "transport" / "websocket.py"

spec = importlib.util.spec_from_file_location("transport_websocket", MODULE)
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)

TOKEN = "test-token-do-not-use-in-production"
CARDS = {9001: {"title": "fixture card for the websocket PoC"}}


def fresh_state():
    return ws.RelayState({"test-token-do-not-use-in-production": "fake-exec"}, CARDS)


class AuthTests(unittest.TestCase):
    def test_authenticate_accepts_the_known_token_only(self):
        state = fresh_state()
        self.assertEqual(state.authenticate(TOKEN), "fake-exec")
        self.assertIsNone(state.authenticate("wrong-token"))
        self.assertIsNone(state.authenticate(""))
        self.assertIsNone(state.authenticate(None))

    def test_lane_state_mapping_never_promotes_a_disconnect_to_failure(self):
        self.assertEqual(ws.lane_state_for("claim"), "in-progress")
        self.assertEqual(ws.lane_state_for("evidence"), "in-progress")
        self.assertEqual(ws.lane_state_for("disconnect"), "lane-ready")
        self.assertIsNone(ws.lane_state_for("merge"))


class IdempotencyTests(unittest.TestCase):
    def test_duplicate_evidence_delivery_is_stored_once(self):
        state = fresh_state()
        assigned = state.claim("fake-exec", 9001, "d-claim-1")
        self.assertEqual(assigned["type"], "assigned")
        run_id = assigned["payload"]["run_id"]
        first = state.submit_evidence("fake-exec", "d-ev-1", 9001, run_id, "evidence://fixture/1")
        second = state.submit_evidence("fake-exec", "d-ev-1", 9001, run_id, "evidence://fixture/1")
        self.assertEqual(first["type"], "ack")
        self.assertFalse(first["payload"]["duplicate"])
        self.assertTrue(second["payload"]["duplicate"])
        self.assertEqual(len(state.evidence_records()), 1)

    def test_claim_replay_returns_the_same_run_id(self):
        state = fresh_state()
        first = state.claim("fake-exec", 9001, "d-claim-1")
        second = state.claim("fake-exec", 9001, "d-claim-1")
        self.assertEqual(first["payload"]["run_id"], second["payload"]["run_id"])

    def test_claim_by_another_executor_is_refused_not_stolen(self):
        state = fresh_state()
        state.claim("fake-exec", 9001, "d-claim-1")
        refused = state.claim("other-exec", 9001, "d-claim-2")
        self.assertEqual(refused["type"], "error")

    def test_evidence_without_an_assignment_is_refused(self):
        state = fresh_state()
        refused = state.submit_evidence("fake-exec", "d-ev-9", 9001, "run-nope", "evidence://x")
        self.assertEqual(refused["type"], "error")
        self.assertEqual(state.evidence_records(), {})

    def test_delivery_id_is_scoped_by_executor_and_operation(self):
        # Review finding 1: the old global delivery-id cache let another
        # executor, or another operation, see a cached response.
        state = fresh_state()
        assigned = state.claim("fake-exec", 9001, "shared-1")
        run_id = assigned["payload"]["run_id"]
        # Another executor reusing the same id must not see the assignment.
        refused = state.claim("other-exec", 9001, "shared-1")
        self.assertEqual(refused["type"], "error")
        self.assertNotEqual(refused.get("payload"), assigned["payload"])
        # The same executor reusing the claim id for evidence must get an
        # evidence ACK, not the cached claim response.
        ack = state.submit_evidence("fake-exec", "shared-1", 9001, run_id,
                                    "evidence://fixture/scoped")
        self.assertEqual(ack["type"], "ack")
        self.assertFalse(ack["payload"]["duplicate"])

    def test_same_delivery_id_for_a_different_request_is_refused(self):
        state = fresh_state()
        assigned = state.claim("fake-exec", 9001, "d-ev-1")
        run_id = assigned["payload"]["run_id"]
        first = state.submit_evidence("fake-exec", "d-ev-1", 9001, run_id,
                                      "evidence://fixture/1")
        self.assertEqual(first["type"], "ack")
        # Same executor/operation/id, different evidence pointer: a client
        # bug, not a replay -- never return the cached ACK for it.
        refused = state.submit_evidence("fake-exec", "d-ev-1", 9001, run_id,
                                        "evidence://fixture/other")
        self.assertEqual(refused["type"], "error")
        self.assertEqual(len(state.evidence_records()), 1)
        self.assertEqual(state.evidence_records()["d-ev-1"]["evidence"],
                         "evidence://fixture/1")

    def test_evidence_delivery_id_recorded_by_another_executor_is_refused(self):
        state = ws.RelayState(
            {"token-a": "exec-a", "token-b": "exec-b"},
            {9001: {"title": "card a"}, 9002: {"title": "card b"}})
        run_a = state.claim("exec-a", 9001, "claim-a")["payload"]["run_id"]
        run_b = state.claim("exec-b", 9002, "claim-b")["payload"]["run_id"]
        first = state.submit_evidence("exec-a", "ev-shared", 9001, run_a,
                                      "evidence://fixture/a")
        self.assertEqual(first["type"], "ack")
        refused = state.submit_evidence("exec-b", "ev-shared", 9002, run_b,
                                        "evidence://fixture/b")
        self.assertEqual(refused["type"], "error")
        records = state.evidence_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records["ev-shared"]["evidence"], "evidence://fixture/a")


class StatusTests(unittest.TestCase):
    def test_report_status_tracks_running_then_completed(self):
        state = fresh_state()
        run_id = state.claim("fake-exec", 9001, "d-claim-1")["payload"]["run_id"]
        running = state.report_status("fake-exec", 9001, run_id, "running")
        self.assertEqual(running["type"], "status-ack")
        self.assertEqual(running["payload"]["status"], "running")
        self.assertEqual(state.status(9001)["status"], "running")
        # Lane mapping: work in flight or done-but-unreviewed stays
        # in-progress; only the control plane may ever say lane-failed.
        self.assertEqual(ws.lane_state_for_status("running"), "in-progress")
        self.assertEqual(ws.lane_state_for_status("completed"), "in-progress")
        self.assertEqual(ws.lane_state_for_status("failed"), "lane-ready")
        self.assertIsNone(ws.lane_state_for_status("stuck"))
        completed = state.report_status("fake-exec", 9001, run_id, "completed")
        self.assertEqual(completed["type"], "status-ack")
        self.assertEqual(state.status(9001)["status"], "completed")

    def test_report_status_refuses_wrong_run_unknown_status_and_strangers(self):
        state = fresh_state()
        run_id = state.claim("fake-exec", 9001, "d-claim-1")["payload"]["run_id"]
        self.assertEqual(state.report_status("fake-exec", 9001, "run-nope",
                                             "running")["type"], "error")
        self.assertEqual(state.report_status("fake-exec", 9001, run_id,
                                             "stuck")["type"], "error")
        self.assertEqual(state.report_status("other-exec", 9001, run_id,
                                             "running")["type"], "error")
        self.assertIsNone(state.status(9001))
        # A status for a card nobody claimed stores nothing either.
        self.assertEqual(state.report_status("fake-exec", 9001, "run-nope",
                                             "failed")["type"], "error")
        self.assertIsNone(state.status(9001))


class SocketPoCTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state()
        self.relay = ws.WebSocketRelay(self.state)
        self.relay.__enter__()
        self.addCleanup(self.relay.stop)
        # The relay thread binds before __enter__ returns, but give the
        # listen socket a moment on slow hosts; connect retries below.
        self.client = None

    def _client(self, token=TOKEN):
        client = ws.ExecutorClient("127.0.0.1", self.relay.port, "fake-exec", token)
        last_error = None
        for _ in range(20):
            try:
                client.connect()
                break
            except (OSError, ConnectionError) as error:
                last_error = error
                time.sleep(0.05)
        else:
            raise last_error
        self.addCleanup(client.close)
        return client

    def test_fake_executor_claims_a_card_and_returns_evidence(self):
        client = self._client()
        assigned = client.claim(9001, "sock-claim-1")
        self.assertEqual(assigned["type"], "assigned")
        self.assertTrue(client.heartbeat())
        ack = client.report_evidence(9001, assigned["payload"]["run_id"],
                                     "evidence://fixture/websocket-poc", "sock-ev-1")
        self.assertEqual(ack["type"], "ack")
        records = self.state.evidence_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records["sock-ev-1"]["evidence"], "evidence://fixture/websocket-poc")

    def test_fake_executor_reports_execution_status_over_the_relay(self):
        client = self._client()
        assigned = client.claim(9001, "sock-claim-1")
        run_id = assigned["payload"]["run_id"]
        running = client.report_status(9001, run_id, "running", "sock-status-1")
        self.assertEqual(running["type"], "status-ack")
        self.assertEqual(running["payload"]["status"], "running")
        self.assertEqual(self.state.status(9001)["status"], "running")
        failed = client.report_status(9001, run_id, "failed", "sock-status-2")
        self.assertEqual(failed["type"], "status-ack")
        self.assertEqual(self.state.status(9001)["status"], "failed")
        # Latest wins, and a bogus status stores nothing new.
        bogus = client.report_status(9001, run_id, "stuck", "sock-status-3")
        self.assertEqual(bogus["type"], "error")
        self.assertEqual(self.state.status(9001)["status"], "failed")

    def test_unauthorized_executor_is_rejected_at_hello(self):
        with self.assertRaises(ConnectionError):
            self._client(token="not-a-real-token")

    def test_disconnect_reconnect_replays_unacked_evidence_exactly_once(self):
        client = self._client()
        assigned = client.claim(9001, "sock-claim-2")
        run_id = assigned["payload"]["run_id"]
        # Simulate the drop *after* sending evidence but before the ack is
        # read: stage it as pending, sever the socket, reconnect, replay.
        message = {"type": "evidence", "id": "sock-ev-2",
                   "payload": {"issue": 9001, "run_id": run_id,
                               "evidence": "evidence://fixture/reconnect"}}
        client.pending["sock-ev-2"] = message
        client.drop()
        client.reconnect()
        self.assertEqual(client.pending, {})
        records = self.state.evidence_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records["sock-ev-2"]["evidence"], "evidence://fixture/reconnect")
        # A second replay of the same delivery id still stores nothing new.
        client.report_evidence(9001, run_id, "evidence://fixture/reconnect", "sock-ev-2")
        self.assertEqual(len(self.state.evidence_records()), 1)

    def test_evidence_sent_but_ack_lost_is_replayed_exactly_once(self):
        # Review finding 2: the old report_evidence registered pending
        # only *after* a successful read, so a drop between send and ACK
        # left the message out of pending and reconnect() never replayed
        # it. Here the frame really is sent, but reading the ACK raises.
        client = self._client()
        assigned = client.claim(9001, "sock-claim-3")
        run_id = assigned["payload"]["run_id"]
        with mock.patch.object(client, "_read_message",
                               side_effect=ConnectionError("ack lost")):
            with self.assertRaises(ConnectionError):
                client.report_evidence(9001, run_id, "evidence://fixture/ack-lost",
                                       "sock-ev-3")
        # The unacked message must already be pending, before any reconnect.
        self.assertIn("sock-ev-3", client.pending)
        # The old test only waited for the record with a timeout and then
        # reconnected anyway, so a slow first delivery could pass via
        # the replay itself instead of via the ACK-lost path. Assert
        # *before* reconnecting that the relay already stored the frame
        # that was sent before the ACK was lost.
        deadline = time.time() + 2.0
        while len(self.state.evidence_records()) < 1 and time.time() < deadline:
            time.sleep(0.02)
        pre_reconnect = self.state.evidence_records()
        self.assertEqual(len(pre_reconnect), 1,
                         "relay must already hold the evidence sent before the ACK was lost")
        self.assertEqual(pre_reconnect["sock-ev-3"]["evidence"],
                         "evidence://fixture/ack-lost")
        client.reconnect()
        self.assertEqual(client.pending, {})
        # The replay must be recognised as a duplicate ACK, not a second
        # store, and the record count must stay at one.
        replay = client.last_replay_replies.get("sock-ev-3", {})
        self.assertEqual(replay.get("type"), "ack")
        self.assertTrue(replay.get("payload", {}).get("duplicate"))
        records = self.state.evidence_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records["sock-ev-3"]["evidence"], "evidence://fixture/ack-lost")


if __name__ == "__main__":
    unittest.main()
