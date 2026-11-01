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


if __name__ == "__main__":
    unittest.main()
