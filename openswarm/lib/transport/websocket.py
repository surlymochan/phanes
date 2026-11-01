"""WebSocket / stream dispatch channel for openswarm (Refs #1483).

The problem: some executors sit behind an unstable IP or have no public IP
at all, so the control plane cannot SSH in to them. This module lets such an
executor dial *out* over a WebSocket and pull work instead: it claims a card,
reports status, and returns an Evidence pointer, all over one outbound
connection. It sits beside the existing SSH fleet and the GitHub external
lane; it does not replace either, and it never changes a GitHub label itself
-- the control plane maps the events below onto the lane state machine.

The module is deliberately standard-library only (no ``websockets`` package
to install on a minimal executor host) and side-effect free apart from the
sockets it opens. It is a PoC, not a production relay:

* Authentication is a bearer token presented in the ``hello`` message and
  compared with :func:`hmac.compare_digest`. Tokens are never logged. The
  PoC speaks plain ``ws://``; production deployments must terminate TLS
  (``wss://``) in front of it and rotate tokens, or the token is visible
  on the wire.
* The relay holds its cards in memory. A real deployment persists
  assignments and evidence next to the Task/Run/Evidence ledger; the
  in-memory store here exists so the protocol (auth, claim, idempotent
  evidence, reconnect) can be tested offline with fixtures.
* This is a transport, not a second scheduler: the relay only hands out
  cards the control plane offered it, and review/merge still happen through
  the lane. See ``openswarm/docs/transport-websocket.md``.

Wire protocol (JSON text frames, RFC 6455):

    executor -> relay: {"type": "hello", "payload": {"executor": "<name>", "token": "<token>"}}
    relay    -> executor: {"type": "welcome", "payload": {"executor": "<name>"}}
    executor -> relay: {"type": "claim", "id": "<delivery-id>", "payload": {"issue": 1483}}
    relay    -> executor: {"type": "assigned", "id": "<delivery-id>",
                           "payload": {"issue": 1483, "run_id": "run-..."}}
    executor -> relay: {"type": "evidence", "id": "<delivery-id>",
                        "payload": {"issue": 1483, "run_id": "run-...",
                                    "evidence": "<pointer>"}}
    relay    -> executor: {"type": "ack", "id": "<delivery-id>",
                           "payload": {"duplicate": false}}
    either   -> either:   {"type": "error", "payload": {"error": "<reason>"}}
    either   -> either:   WebSocket ping/pong frames (heartbeat)

Idempotency: every ``claim`` and ``evidence`` message carries a client
chosen ``id`` (the delivery id). Replaying a message with the same id
returns the original response instead of creating a second assignment or a
second evidence record. That is what makes reconnect safe: after a drop the
executor reconnects, re-authenticates, and replays any unacknowledged
evidence with the same delivery id; the relay stores it exactly once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import socketserver
import struct
import threading
import time
from typing import Any, Dict, Optional, Tuple

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Lane states the control plane maps each relay event onto. The relay
# itself never touches GitHub; this table is the contract for the caller.
LANE_STATE_FOR_EVENT = {
    "claim": "in-progress",      # card leaves lane-ready, executor holds it
    "heartbeat": "in-progress",  # lease still alive, nothing new to schedule
    "evidence": "in-progress",   # work reported; review/merge still pending
    "disconnect": "lane-ready",  # lease expiry requeues; never lane-failed
}


def lane_state_for(event: str) -> Optional[str]:
    """The lane state a relay event maps to, or None for unknown events."""
    return LANE_STATE_FOR_EVENT.get(event)


# --------------------------------------------------------------------------
# Relay state: auth, assignments, idempotent evidence
# --------------------------------------------------------------------------

class RelayState:
    """In-memory relay state: tokens, offered cards, assignments, evidence.

    ``tokens`` maps a bearer token to an executor name. ``cards`` maps an
    issue number to the card dict the control plane offered (a fixture in
    tests, a real card in production). Everything else is derived.
    """

    def __init__(self, tokens: Dict[str, str], cards: Dict[Any, Dict[str, Any]]) -> None:
        self.tokens = dict(tokens)
        self.cards = {int(issue): dict(card) for issue, card in cards.items()}
        self._lock = threading.Lock()
        self._assignments: Dict[int, Dict[str, Any]] = {}
        self._responses: Dict[str, Dict[str, Any]] = {}
        self._evidence: Dict[str, Dict[str, Any]] = {}

    def authenticate(self, token: Any) -> Optional[str]:
        """The executor name for ``token``, or None. Constant-time compare."""
        if not isinstance(token, str) or not token:
            return None
        for known, executor in self.tokens.items():
            if hmac.compare_digest(known, token):
                return executor
        return None

    def claim(self, executor: str, issue: Any, delivery_id: str) -> Dict[str, Any]:
        """Assign ``issue`` to ``executor``; a replay returns the original.

        A card already held by another executor is an error, not a steal:
        the lane's lease, not this transport, decides when a card is free.
        """
        if delivery_id in self._responses:
            return self._responses[delivery_id]
        with self._lock:
            if delivery_id in self._responses:
                return self._responses[delivery_id]
            try:
                number = int(issue)
            except (TypeError, ValueError):
                response = {"type": "error", "payload": {"error": "issue must be a number"}}
                self._responses[delivery_id] = response
                return response
            if number not in self.cards:
                response = {"type": "error", "payload": {"error": f"no such card: {number}"}}
                self._responses[delivery_id] = response
                return response
            held = self._assignments.get(number)
            if held is not None and held["executor"] != executor:
                response = {"type": "error",
                            "payload": {"error": f"card {number} held by {held['executor']}"}}
                self._responses[delivery_id] = response
                return response
            if held is None:
                held = {"issue": number, "executor": executor,
                        "run_id": f"run-{number}-{executor}"}
                self._assignments[number] = held
            response = {"type": "assigned", "id": delivery_id, "payload": dict(held)}
            self._responses[delivery_id] = response
            return response

    def submit_evidence(self, executor: str, delivery_id: str,
                        issue: Any, run_id: Any, evidence: Any) -> Dict[str, Any]:
        """Store an evidence pointer exactly once per delivery id.

        The pointer is opaque (a URL, a ledger key, a fixture path); the
        relay stores it and never dereferences it, so a replayed pointer
        cannot double-count a delivery.
        """
        if delivery_id in self._responses:
            stored = self._responses[delivery_id]
            replay = dict(stored)
            replay["payload"] = dict(stored.get("payload", {}))
            replay["payload"]["duplicate"] = True
            return replay
        with self._lock:
            if delivery_id in self._responses:
                stored = self._responses[delivery_id]
                replay = dict(stored)
                replay["payload"] = dict(stored.get("payload", {}))
                replay["payload"]["duplicate"] = True
                return replay
            try:
                number = int(issue)
            except (TypeError, ValueError):
                response = {"type": "error", "payload": {"error": "issue must be a number"}}
                self._responses[delivery_id] = response
                return response
            held = self._assignments.get(number)
            if held is None or held["executor"] != executor or held["run_id"] != run_id:
                response = {"type": "error",
                            "payload": {"error": f"no assignment for {executor} on {number}"}}
                self._responses[delivery_id] = response
                return response
            if not isinstance(evidence, str) or not evidence:
                response = {"type": "error", "payload": {"error": "evidence pointer must be non-empty"}}
                self._responses[delivery_id] = response
                return response
            record = {"issue": number, "executor": executor, "run_id": run_id,
                      "evidence": evidence}
            self._evidence[delivery_id] = record
            response = {"type": "ack", "id": delivery_id,
                        "payload": {"duplicate": False, "record": dict(record)}}
            self._responses[delivery_id] = response
            return response

    def evidence_records(self) -> Dict[str, Dict[str, Any]]:
        """A copy of every stored evidence record, keyed by delivery id."""
        with self._lock:
            return {key: dict(record) for key, record in self._evidence.items()}

    def assignment(self, issue: Any) -> Optional[Dict[str, Any]]:
        """The current assignment of ``issue``, or None."""
        try:
            return dict(self._assignments[int(issue)])
        except (KeyError, TypeError, ValueError):
            return None


# --------------------------------------------------------------------------
# RFC 6455 framing (minimal: single frames, text/ping/pong/close)
# --------------------------------------------------------------------------

def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode_frame(payload: bytes, opcode: int = 1, mask: bool = False) -> bytes:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([(0x80 if mask else 0) | length])
    elif length < 65536:
        header += bytes([(0x80 if mask else 0) | 126]) + struct.pack("!H", length)
    else:
        header += bytes([(0x80 if mask else 0) | 127]) + struct.pack("!Q", length)
    if not mask:
        return header + payload
    key = os.urandom(4)
    masked = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return header + key + masked


def _read_exact(stream: Any, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: Any) -> Tuple[int, bytes]:
    """One frame: (opcode, payload). Raises ConnectionError on close/EOF."""
    first = _read_exact(stream, 2)
    opcode = first[0] & 0x0F
    masked = bool(first[1] & 0x80)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(stream, 8))[0]
    mask_key = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
    return opcode, payload


def _send_text(sock: socket.socket, message: Dict[str, Any]) -> None:
    sock.sendall(_encode_frame(json.dumps(message).encode("utf-8"), opcode=1))


# --------------------------------------------------------------------------
# Relay server: one outbound connection per executor, no inbound ports
# --------------------------------------------------------------------------

class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        stream = self.request.makefile("rb")
        try:
            headers = self._read_http_headers(stream)
        except (ConnectionError, ValueError):
            return
        key = headers.get("sec-websocket-key")
        if headers.get("upgrade", "").lower() != "websocket" or not key:
            return
        response = ("HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {_ws_accept(key)}\r\n\r\n")
        try:
            self.request.sendall(response.encode("ascii"))
        except OSError:
            return
        executor: Optional[str] = None
        while True:
            try:
                opcode, payload = _read_frame(stream)
            except (ConnectionError, OSError, struct.error):
                return
            if opcode == 8:  # close
                return
            if opcode == 9:  # ping -> pong, the heartbeat
                try:
                    self.request.sendall(_encode_frame(payload, opcode=10))
                except OSError:
                    return
                continue
            if opcode != 1:  # binary/continuation: not part of the PoC
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._reply({"type": "error", "payload": {"error": "message must be JSON"}})
                continue
            if not isinstance(message, dict):
                self._reply({"type": "error", "payload": {"error": "message must be an object"}})
                continue
            kind = message.get("type")
            data = message.get("payload") or {}
            if not isinstance(data, dict):
                self._reply({"type": "error", "payload": {"error": "payload must be an object"}})
                continue
            if kind == "hello":
                claimed = self.server.state.authenticate(data.get("token"))
                if claimed is None or (data.get("executor") and data["executor"] != claimed):
                    self._reply({"type": "error", "payload": {"error": "unauthorized"}})
                    return
                executor = claimed
                self._reply({"type": "welcome", "payload": {"executor": executor}})
                continue
            if executor is None:
                self._reply({"type": "error", "payload": {"error": "hello first"}})
                continue
            if kind == "claim":
                self._reply(self.server.state.claim(
                    executor, data.get("issue"), str(message.get("id") or "")))
            elif kind == "evidence":
                self._reply(self.server.state.submit_evidence(
                    executor, str(message.get("id") or ""),
                    data.get("issue"), data.get("run_id"), data.get("evidence")))
            else:
                self._reply({"type": "error", "payload": {"error": f"unknown type: {kind}"}})

    def _read_http_headers(self, stream: Any) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        first = stream.readline(65536)
        if not first:
            raise ConnectionError("empty handshake")
        while True:
            line = stream.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
            if b":" in line:
                name, value = line.decode("iso-8859-1").split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return headers

    def _reply(self, message: Dict[str, Any]) -> None:
        try:
            _send_text(self.request, message)
        except OSError:
            pass


class _RelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], state: RelayState) -> None:
        self.state = state
        super().__init__(address, _RelayHandler)


class WebSocketRelay:
    """A relay listening on an ephemeral port; the executor dials out."""

    def __init__(self, state: RelayState, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server = _RelayServer((host, port), state)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "WebSocketRelay":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------
# Executor client: outbound only, reconnects and replays unacked evidence
# --------------------------------------------------------------------------

class ExecutorClient:
    """A fake (PoC) executor: no inbound port, dials out, claims, reports.

    ``pending`` holds evidence messages not yet acked; :meth:`reconnect`
    re-authenticates and replays them with the same delivery ids, so the
    relay's idempotency -- not a second delivery -- absorbs the replay.
    """

    def __init__(self, host: str, port: int, executor: str, token: str) -> None:
        self.host = host
        self.port = port
        self.executor = executor
        self.token = token
        self.sock: Optional[socket.socket] = None
        self.stream: Any = None
        self.pending: Dict[str, Dict[str, Any]] = {}

    def connect(self) -> Dict[str, Any]:
        """Open the socket, handshake, hello. Returns the welcome message."""
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=5.0)
        sock.settimeout(5.0)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET /dispatch HTTP/1.1\r\n"
                   f"Host: {self.host}:{self.port}\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        stream = sock.makefile("rb")
        status = stream.readline(65536)
        if b"101" not in status:
            sock.close()
            raise ConnectionError(f"handshake failed: {status!r}")
        while True:
            line = stream.readline(65536)
            if not line or line in (b"\r\n", b"\n"):
                break
        self.sock = sock
        self.stream = stream
        self._send({"type": "hello",
                    "payload": {"executor": self.executor, "token": self.token}})
        welcome = self._read_message()
        if welcome.get("type") != "welcome":
            self.close()
            raise ConnectionError(f"authentication rejected: {welcome!r}")
        return welcome

    def reconnect(self) -> Dict[str, Any]:
        """Re-authenticate, then replay every unacked evidence message."""
        welcome = self.connect()
        for delivery_id, message in list(self.pending.items()):
            self._send(message)
            reply = self._read_message()
            if reply.get("type") == "ack":
                self.pending.pop(delivery_id, None)
        return welcome

    def claim(self, issue: int, delivery_id: str) -> Dict[str, Any]:
        self._send({"type": "claim", "id": delivery_id, "payload": {"issue": issue}})
        return self._read_message()

    def report_evidence(self, issue: int, run_id: str, evidence: str,
                        delivery_id: str) -> Dict[str, Any]:
        message = {"type": "evidence", "id": delivery_id,
                   "payload": {"issue": issue, "run_id": run_id, "evidence": evidence}}
        self._send(message)
        reply = self._read_message()
        if reply.get("type") == "ack" and not reply.get("payload", {}).get("duplicate"):
            self.pending.pop(delivery_id, None)
        else:
            self.pending[delivery_id] = message
        return reply

    def heartbeat(self) -> bool:
        """One ping/pong round trip; False when the connection is dead."""
        if self.sock is None:
            return False
        try:
            self.sock.sendall(_encode_frame(b"ping", opcode=9, mask=True))
            while True:
                opcode, _payload = _read_frame(self.stream)
                if opcode == 10:
                    return True
                if opcode == 8:
                    return False
        except (OSError, ConnectionError, struct.error):
            return False

    def drop(self) -> None:
        """Simulate a severed connection: close without a close frame."""
        self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.stream = None

    def _send(self, message: Dict[str, Any]) -> None:
        assert self.sock is not None, "not connected"
        self.sock.sendall(_encode_frame(json.dumps(message).encode("utf-8"),
                                        opcode=1, mask=True))

    def _read_message(self) -> Dict[str, Any]:
        assert self.stream is not None, "not connected"
        while True:
            opcode, payload = _read_frame(self.stream)
            if opcode == 1:
                return json.loads(payload.decode("utf-8"))
            if opcode == 8:
                raise ConnectionError("relay closed the connection")
            if opcode == 9 and self.sock is not None:
                self.sock.sendall(_encode_frame(payload, opcode=10, mask=True))
