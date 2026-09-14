from __future__ import annotations

import base64
import csv
import hashlib
import json
import select
import socket
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from competition import protocol
from competition.core import Engine
from competition.network import Service
from competition.storage import Store, csv_safe
from vision_client import VisionClient


def mid() -> str:
    return uuid.uuid4().hex


class FakePeer:
    def __init__(self):
        self.name = "127.0.0.1:12345"
        self.session_id = None
        self.client_id = None
        self.target_revision = None
        self.alive = True
        self.disconnect_reported = False
        self.sent = []

    def send(self, message):
        if not self.alive:
            raise ConnectionError("closed")
        self.sent.append(json.loads(protocol.encode(message)))

    def close(self):
        self.alive = False


class ProtocolTests(unittest.TestCase):
    def test_round_trip_chinese(self):
        msg = {"v": 1, "type": "hello", "msg_id": "中文编号", "client_id": "vision-01", "access_code": "ABCD1234"}
        self.assertEqual(protocol.decode(protocol.encode(msg)[:-1]), msg)
        protocol.validate(msg)

    def test_crlf(self):
        self.assertEqual(protocol.decode(b'{"x":1}\r'), {"x": 1})

    def test_empty(self):
        for value in (b"", b" ", b"\r"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.decode(value)

    def test_duplicate_key(self):
        with self.assertRaisesRegex(protocol.ProtocolError, "duplicate"):
            protocol.decode(b'{"v":1,"v":1}')

    def test_invalid_utf8(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"x":"\xff"}')

    def test_bom_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'\xef\xbb\xbf{}')

    def test_nonfinite(self):
        for value in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e9999}'):
            with self.assertRaises(protocol.ProtocolError):
                protocol.decode(value)

    def test_lone_surrogate(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"x":"\\ud800"}')

    def test_top_level_not_object(self):
        for raw in (b"[]", b"true", b"42", b"null"):
            with self.assertRaises(protocol.ProtocolError):
                protocol.decode(raw)

    def test_depth(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b'{"x":' + b"[" * 30 + b"0" + b"]" * 30 + b"}")

    def test_oversized(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode(b" " * (protocol.MAX_FRAME + 1))

    def test_boolean_version(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"v": True, "type": "ping", "msg_id": "a", "session_id": "s"})

    def test_unknown_type(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"v": 1, "type": "give_expected", "msg_id": "a"})

    def test_unknown_fields(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"v": 1, "type": "ping", "msg_id": "a", "session_id": "s", "extra": 3})

    def test_missing_fields(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.validate({"v": 1, "type": "hello", "msg_id": "a"})

    def test_fingerprint_key_order(self):
        self.assertEqual(protocol.fingerprint({"a": 1, "b": 2}), protocol.fingerprint({"b": 2, "a": 1}))

    def test_csv_injection(self):
        for text in ("=1+1", "+formula", "-formula", "@sum", "  =x", "\ttext"):
            self.assertTrue(csv_safe(text).startswith("'"))
        self.assertEqual(csv_safe(123), 123)
        self.assertEqual(csv_safe("正常"), "正常")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.engine = Engine(self.store)
        self.session = self.engine.new_session("测试队伍", "vision-01", "packaging", "ABCD1234")
        self.sid = self.session["id"]
        self.peer = self.authorize()

    def tearDown(self):
        self.engine.close_session()
        self.store.close()
        self.temp.cleanup()

    def authorize(self, acknowledge=True):
        peer = FakePeer()
        self.engine.receive(peer, protocol.encode({"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "ABCD1234"}))
        if acknowledge:
            self.send("target_ack", peer=peer, target_revision=self.engine.session["target_revision"])
        return peer

    def send(self, kind, peer=None, **kw):
        peer = peer or self.peer
        msg = {"v": 1, "type": kind, "msg_id": mid(), "session_id": self.engine.session_id, **kw}
        self.engine.receive(peer, protocol.encode(msg))
        return peer.sent[-1]

    def start(self, expected="OK", timeout=0, **kw):
        return self.engine.start_round("裁判私密样件类别", expected, timeout, notes="私密备注SECRET", **kw)

    def result(self, rid, verdict="OK", **kw):
        return self.send("result", round_id=rid, target_revision=1, verdict=verdict, **kw)

    def test_ok(self):
        rid = self.start()
        self.assertTrue(self.result(rid)["recorded"])
        row = self.store.get_round(rid)
        self.assertEqual((row["actual"], row["matched"], row["status"]), ("OK", 1, "RECEIVED"))
        self.assertGreaterEqual(row["elapsed_ms"], 0)

    def test_ng_is_correct_when_expected_ng(self):
        rid = self.start(expected="NG")
        self.result(rid, "NG")
        self.assertEqual(self.store.get_round(rid)["matched"], 1)

    def test_wrong_verdict_still_ack_not_score(self):
        rid = self.start(expected="NG")
        ack = self.result(rid, "OK")
        self.assertTrue(ack["recorded"])
        self.assertNotIn("matched", ack)
        self.assertNotIn("expected", ack)
        self.assertEqual(self.store.get_round(rid)["matched"], 0)

    def test_private_data_never_sent(self):
        rid = self.start(expected="NG")
        self.send("get_state")
        self.result(rid, "OK")
        outgoing = json.dumps(self.peer.sent, ensure_ascii=False)
        for secret in ("裁判私密样件类别", "私密备注SECRET", '"expected"', '"matched"'):
            self.assertNotIn(secret, outgoing)

    def test_no_timeout_by_default(self):
        rid = self.start()
        self.engine.started_ns[rid] -= 10_000_000_000
        self.engine.tick()
        self.assertEqual(self.store.get_round(rid)["status"], "WAITING")

    def test_timeout_does_not_fabricate_ng(self):
        rid = self.start(timeout=50)
        self.engine.started_ns[rid] -= 100_000_000
        self.engine.tick()
        row = self.store.get_round(rid)
        self.assertEqual(row["status"], "TIMEOUT")
        self.assertIsNone(row["actual"])
        self.assertIsNone(row["matched"])

    def test_late_message(self):
        rid = self.start(timeout=50)
        self.engine.started_ns[rid] -= 100_000_000
        self.engine.tick()
        self.result(rid)
        self.assertEqual(self.store.get_round(rid)["status"], "LATE")

    def test_late_before_timer_tick(self):
        rid = self.start(timeout=50)
        self.engine.started_ns[rid] -= 100_000_000
        self.result(rid)
        self.assertEqual(self.store.get_round(rid)["status"], "LATE")

    def test_late_does_not_complete_new_round(self):
        old = self.start(timeout=50)
        self.engine.started_ns[old] -= 100_000_000
        self.engine.tick()
        new = self.start()
        self.result(old)
        self.assertEqual(self.engine.active_id, new)
        self.assertIsNone(self.store.get_round(new)["actual"])

    def test_same_id_retry(self):
        rid = self.start()
        msg_id = mid()
        first = self.result(rid, msg_id=msg_id)
        retry = self.result(rid, msg_id=msg_id)
        self.assertFalse(first["duplicate"])
        self.assertTrue(retry["duplicate"])
        self.assertEqual(self.store.get_round(rid)["retries"], 1)
        self.assertEqual(self.store.stats(self.sid)["received"], 1)

    def test_new_id_duplicate(self):
        rid = self.start()
        self.result(rid)
        reply = self.result(rid, "NG")
        row = self.store.get_round(rid)
        self.assertEqual(reply["code"], "DUPLICATE_RESULT")
        self.assertEqual(row["actual"], "OK")
        self.assertEqual(row["duplicates"], 1)

    def test_same_id_changed_content(self):
        rid = self.start()
        message_id = mid()
        self.result(rid, msg_id=message_id)
        reply = self.result(rid, "NG", msg_id=message_id)
        self.assertEqual(reply["code"], "MSG_ID_CONFLICT")
        self.assertEqual(self.store.get_round(rid)["actual"], "OK")

    def test_unknown_round(self):
        self.assertEqual(self.result(mid())["code"], "UNKNOWN_ROUND")

    def test_lowercase_verdict(self):
        rid = self.start()
        self.assertEqual(self.result(rid, "ok")["code"], "INVALID_VERDICT")
        self.assertIsNone(self.store.get_round(rid)["actual"])

    def test_result_boolean_revision(self):
        rid = self.start()
        result = self.send("result", round_id=rid, verdict="OK", target_revision=True)
        self.assertEqual(result["code"], "INVALID_FIELD")

    def test_result_target_revision_mismatch(self):
        rid = self.start()
        result = self.send("result", round_id=rid, verdict="OK", target_revision=2)
        self.assertEqual(result["code"], "TARGET_MISMATCH")

    def test_target_change_requires_ack(self):
        self.engine.set_target("BOX_B", "MODEL2", "LABEL2")
        with self.assertRaisesRegex(ValueError, "target_ack"):
            self.start()
        self.send("target_ack", target_revision=2)
        rid = self.start()
        self.assertEqual(self.store.get_round(rid)["target_revision"], 2)

    def test_target_locked_while_waiting(self):
        self.start()
        with self.assertRaises(ValueError):
            self.engine.set_target("BOX_B")

    def test_late_result_uses_old_target_snapshot(self):
        rid = self.start(timeout=50)
        self.engine.started_ns[rid] -= 100_000_000
        self.engine.tick()
        self.engine.set_target("BOX_B")
        self.send("target_ack", target_revision=2)
        self.result(rid)
        row = self.store.get_round(rid)
        self.assertEqual(row["status"], "LATE")
        self.assertEqual(json.loads(row["target_json"])["box_type"], "BOX_A")

    def test_double_start_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            self.start()

    def test_switch_session_while_waiting_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            self.engine.new_session("x", "vision-01", "screw")

    def test_cancel_rejects_late_result(self):
        rid = self.start()
        self.engine.cancel_round("裁判原因SECRET")
        self.assertEqual(self.result(rid)["code"], "ROUND_CLOSED")
        self.assertEqual(self.store.get_round(rid)["status"], "CANCELLED")
        self.assertNotIn("裁判原因SECRET", json.dumps(self.peer.sent, ensure_ascii=False))

    def test_reconnect_timer_not_reset(self):
        rid = self.start()
        start = self.engine.started_ns[rid]
        self.peer.close()
        self.engine.disconnected(self.peer, "test")
        self.peer = self.authorize()
        self.assertEqual(self.engine.started_ns[rid], start)
        self.result(rid)
        self.assertEqual(self.store.get_round(rid)["disconnects"], 1)

    def test_reconnect_before_old_disconnect_callback(self):
        rid = self.start()
        old_peer = self.peer
        old_peer.close()
        self.peer = self.authorize()
        self.engine.disconnected(old_peer, "late callback")
        self.assertIs(self.engine.peer, self.peer)
        self.assertEqual(self.store.get_round(rid)["disconnects"], 1)
        self.result(rid)
        self.assertEqual(self.store.get_round(rid)["matched"], 1)

    def test_unicode_wrong_access_code_is_rejected(self):
        other = FakePeer()
        self.engine.receive(other, protocol.encode({"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "错误密码"}))
        self.assertEqual(other.sent[-1]["code"], "AUTH_FAILED")
        self.assertFalse(other.alive)

    def test_ack_retry_after_reconnect(self):
        rid = self.start()
        message_id = mid()
        self.result(rid, msg_id=message_id)
        self.peer.close()
        self.engine.disconnected(self.peer, "test")
        self.peer = self.authorize()
        self.assertTrue(self.result(rid, msg_id=message_id)["duplicate"])

    def test_old_session_rejected(self):
        old_id = self.sid
        self.engine.new_session("第二队", "vision-01", "screw", "ABCD1234")
        self.peer = self.authorize()
        self.assertEqual(self.send("ping", session_id=old_id)["code"], "SESSION_MISMATCH")

    def test_screw_session_empty_target(self):
        session = self.engine.new_session("螺钉队", "vision-01", "screw", "ABCD1234")
        self.assertEqual(session["target"], {})
        self.peer = self.authorize()
        rid = self.start(expected="NG", action="trigger")
        self.result(rid, "NG")
        self.assertEqual(self.store.get_round(rid)["action"], "trigger")

    def test_wrong_code(self):
        other = FakePeer()
        self.engine.receive(other, protocol.encode({"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "WRONG"}))
        self.assertEqual(other.sent[-1]["code"], "AUTH_FAILED")
        self.assertFalse(other.alive)

    def test_busy_second_client(self):
        other = self.authorize(acknowledge=False)
        self.assertEqual(other.sent[-1]["code"], "BUSY")
        self.assertIs(self.engine.peer, self.peer)

    def test_identity_required(self):
        other = FakePeer()
        self.engine.receive(other, protocol.encode({"v": 1, "type": "ping", "msg_id": mid(), "session_id": self.sid}))
        self.assertEqual(other.sent[-1]["code"], "NOT_AUTHENTICATED")

    def test_handshake_no_target_ack_not_ready(self):
        self.peer.close()
        self.engine.disconnected(self.peer, "test")
        self.peer = self.authorize(False)
        with self.assertRaises(ValueError):
            self.start()

    def test_get_target(self):
        response = self.send("get_target")
        self.assertEqual(response["target"]["box_type"], "BOX_A")

    def test_ping(self):
        self.assertEqual(self.send("ping")["type"], "pong")

    def test_db_failure_never_acknowledged(self):
        rid = self.start()
        self.peer.sent.clear()
        with patch.object(self.store, "record_result", side_effect=sqlite3.OperationalError("disk full")):
            self.engine.receive(self.peer, protocol.encode({"v": 1, "type": "result", "msg_id": mid(),
                "session_id": self.sid, "round_id": rid, "target_revision": 1, "verdict": "OK"}))
        self.assertTrue(self.engine.fatal)
        self.assertFalse(self.peer.alive)
        self.assertFalse(any(m["type"] == "result_ack" for m in self.peer.sent))
        self.assertIsNone(self.store.get_round(rid)["actual"])

    def test_export_original_bytes_and_hashes(self):
        rid = self.start()
        self.result(rid)
        directory = self.engine.export(self.sid, Path(self.temp.name) / "export")
        data = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(data["not_an_automatic_score"])
        self.assertNotIn("access_code", data["session"])
        manifest = json.loads((directory / "SHA256.json").read_text())
        for name, checksum in manifest.items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), checksum)
        with (directory / "rounds.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(len(list(csv.reader(f))), 2)
        events = [json.loads(s) for s in (directory / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        raw = [base64.b64decode(e["raw_b64"]) for e in events if e["raw_b64"]]
        self.assertTrue(any(b'"type":"result"' in r for r in raw))

    def test_no_export_overwrite(self):
        one = self.engine.export(self.sid, Path(self.temp.name) / "export")
        two = self.engine.export(self.sid, Path(self.temp.name) / "export")
        self.assertNotEqual(one, two)

    def test_second_process_data_lock(self):
        with self.assertRaisesRegex(RuntimeError, "占用"):
            Store(self.store.path)

    def test_restart_marks_unfinished_interrupted(self):
        rid = self.start()
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.engine = Engine(self.store)
        self.assertEqual(self.store.get_round(rid)["status"], "INTERRUPTED")
        self.assertEqual(self.store.sessions()[0]["state"], "INTERRUPTED")

    def test_ten_alternating_rounds(self):
        for i in range(10):
            expected = "OK" if i % 2 == 0 else "NG"
            rid = self.start(expected=expected)
            self.result(rid, expected)
        stats = self.store.stats(self.sid)
        self.assertEqual((stats["total"], stats["received"], stats["matched"]), (10, 10, 10))


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "network.sqlite3")
        self.engine = Engine(self.store)
        self.session = self.engine.new_session("网络测试", "vision-01", "packaging", "ABCD1234")
        self.service = Service(self.engine)
        self.addr = self.service.start(port=0)
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass
        self.service.stop()
        self.engine.close_session()
        self.store.close()
        self.temp.cleanup()

    def connect(self):
        sock = socket.create_connection(self.addr, timeout=2)
        sock.settimeout(2)
        self.sockets.append(sock)
        return sock

    @staticmethod
    def read(sock):
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("EOF")
            data.extend(chunk)
        return protocol.decode(bytes(data[:-1]))

    def hello(self, sock):
        sock.sendall(protocol.encode({"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "ABCD1234"}))
        msg = self.read(sock)
        self.assertEqual(msg["type"], "hello_ok")
        sock.sendall(protocol.encode({"v": 1, "type": "target_ack", "msg_id": mid(), "session_id": self.session["id"], "target_revision": 1}))
        self.assertEqual(self.read(sock)["type"], "ack")

    def test_fragmentation(self):
        sock = self.connect()
        msg = protocol.encode({"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "ABCD1234"})
        sock.sendall(msg[:13])
        self.assertFalse(select.select([sock], [], [], 0.05)[0])
        for chunk in (msg[13:29], msg[29:]):
            sock.sendall(chunk)
        self.assertEqual(self.read(sock)["type"], "hello_ok")

    def test_coalesced_frames(self):
        sock = self.connect()
        self.hello(sock)
        ids = [mid() for _ in range(3)]
        sock.sendall(b"".join(protocol.encode({"v": 1, "type": "ping", "msg_id": m, "session_id": self.session["id"]}) for m in ids))
        self.assertEqual([self.read(sock)["reply_to"] for _ in ids], ids)

    def test_crlf_on_wire(self):
        sock = self.connect()
        msg = {"v": 1, "type": "hello", "msg_id": mid(), "client_id": "vision-01", "access_code": "ABCD1234"}
        sock.sendall(protocol.encode(msg)[:-1] + b"\r\n")
        self.assertEqual(self.read(sock)["type"], "hello_ok")

    def test_unterminated_oversize(self):
        sock = self.connect()
        sock.sendall(b"x" * (protocol.MAX_FRAME + 1))
        self.assertEqual(self.read(sock)["code"], "FRAME_TOO_LARGE")

    def test_invalid_json_counted_not_result(self):
        sock = self.connect()
        self.hello(sock)
        rid = self.engine.start_round("valid", "OK")
        self.read(sock)
        sock.sendall(b'NOT_OK\n')
        self.assertEqual(self.read(sock)["code"], "INVALID_JSON")
        self.assertIsNone(self.store.get_round(rid)["actual"])
        self.assertEqual(self.store.get_round(rid)["protocol_errors"], 1)

    def test_stop_interrupts_round(self):
        sock = self.connect()
        self.hello(sock)
        rid = self.engine.start_round("test", "OK")
        self.read(sock)
        self.service.stop()
        self.assertEqual(self.store.get_round(rid)["status"], "INTERRUPTED")

    def test_sdk_ten_rounds(self):
        client = VisionClient(*self.addr, "vision-01", "ABCD1234")
        try:
            hello = client.connect()
            client.acknowledge_target(hello["target_revision"])
            self.assertEqual(client.receive(2)["type"], "ack")
            for i in range(10):
                verdict = "OK" if i % 2 == 0 else "NG"
                self.engine.start_round(f"cycle {i}", verdict)
                command = client.receive(2)
                result = client.make_result(command, verdict)
                client.send(result)
                self.assertTrue(client.receive(2)["recorded"])
            self.assertEqual(self.store.stats(self.session["id"])["matched"], 10)
        finally:
            client.close()

    def test_server_restart_listening(self):
        self.service.stop()
        address = self.service.start(port=0)
        sock = socket.create_connection(address, timeout=2)
        sock.close()
        self.assertIsNotNone(self.service.listener)

    def test_transport_disconnect_and_reconnect(self):
        client = VisionClient(*self.addr, "vision-01", "ABCD1234")
        hello = client.connect()
        client.acknowledge_target(1)
        client.receive(2)
        rid = self.engine.start_round("reconnect", "OK")
        command = client.receive(2)
        start_ns = self.engine.started_ns[rid]
        client.close()
        deadline = time.monotonic() + 2
        while self.engine.peer is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            client.connect()
            client.acknowledge_target(1)
            client.receive(2)
            client.request("get_state")
            state = client.receive(2)
            self.assertEqual(state["active_round"]["round_id"], rid)
            self.assertEqual(self.engine.started_ns[rid], start_ns)
            client.send(client.make_result(command, "OK"))
            self.assertTrue(client.receive(2)["recorded"])
            self.assertEqual(self.store.get_round(rid)["disconnects"], 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
