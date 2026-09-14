"""Small reusable contestant-side client (standard library only).

Use ONE thread for socket reads and writes. Run image acquisition/inference in
another worker, then send its final result from the I/O thread. This module
never computes OK/NG, applies judge expectations, or aggregates camera results.
"""
from __future__ import annotations

import socket
import time
import uuid
from typing import Any

from competition.protocol import MAX_FRAME, ProtocolError, decode, encode


class ServerRejected(RuntimeError):
    def __init__(self, response: dict[str, Any]):
        self.response = response
        super().__init__(f"{response.get('code')}: {response.get('message')}")


class VisionClient:
    def __init__(self, host: str, port: int, client_id: str, access_code: str):
        self.host, self.port = host, port
        self.client_id, self.access_code = client_id, access_code
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.session_id: str | None = None
        self.last_ping = 0.0

    def connect(self) -> dict[str, Any]:
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=3)
        self.sock.settimeout(0.25)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.send({"v": 1, "type": "hello", "msg_id": uuid.uuid4().hex,
                   "client_id": self.client_id, "access_code": self.access_code})
        message = self.receive(3)
        if not message:
            self.close()
            raise TimeoutError("hello response timed out")
        if message.get("type") == "error":
            self.close()
            raise ServerRejected(message)
        if message.get("v") != 1 or message.get("type") != "hello_ok":
            self.close()
            raise ProtocolError("BAD_SERVER_MESSAGE", "expected hello_ok")
        self.session_id = message["session_id"]
        self.last_ping = time.monotonic()
        return message

    def send(self, message: dict[str, Any]) -> None:
        if not self.sock:
            raise ConnectionError("not connected")
        self.sock.settimeout(0.5)
        self.sock.sendall(encode(message))

    def request(self, kind: str, **fields: Any) -> str:
        if not self.session_id:
            raise ConnectionError("hello is required")
        mid = uuid.uuid4().hex
        self.send({"v": 1, "type": kind, "msg_id": mid,
                   "session_id": self.session_id, **fields})
        return mid

    def receive(self, timeout: float = 0.25) -> dict[str, Any] | None:
        """None means no complete message yet, not a disconnect.

        Partial bytes remain buffered across timeouts; only EOF means disconnect.
        """
        if not self.sock:
            raise ConnectionError("not connected")
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self.buffer:
                pos = self.buffer.index(b"\n")
                frame = bytes(self.buffer[:pos])
                del self.buffer[:pos + 1]
                return decode(frame)
            if len(self.buffer) > MAX_FRAME:
                raise ProtocolError("FRAME_TOO_LARGE", "server frame too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(min(0.25, remaining))
            try:
                raw = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not raw:
                raise ConnectionError("server disconnected")
            self.buffer.extend(raw)

    def heartbeat(self) -> None:
        if time.monotonic() - self.last_ping >= 5:
            self.request("ping")
            self.last_ping = time.monotonic()

    def acknowledge_target(self, revision: int) -> str:
        """Call ONLY AFTER your software has applied/validated the target configuration."""
        return self.request("target_ack", target_revision=revision)

    def make_result(self, round_message: dict[str, Any], verdict: str,
                    details: dict[str, Any] | None = None,
                    msg_id: str | None = None) -> dict[str, Any]:
        if round_message["session_id"] != self.session_id:
            raise ValueError("stale session; result must not be sent to a different team/session")
        if verdict not in {"OK", "NG"}:
            raise ValueError("verdict must be exactly OK or NG")
        message = {"v": 1, "type": "result", "msg_id": msg_id or uuid.uuid4().hex,
                   "session_id": self.session_id, "round_id": round_message["round_id"],
                   "target_revision": round_message["target_revision"], "verdict": verdict}
        if details is not None:
            message["details"] = details
        return message

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        self.sock = None
        self.buffer.clear()
        self.session_id = None

    def __enter__(self) -> "VisionClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
