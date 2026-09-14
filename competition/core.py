"""Competition state machine, independent of Tk and socket implementation."""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from . import protocol
from .storage import Store, json_text, utc_now


class Peer(Protocol):
    name: str
    session_id: str | None
    client_id: str | None
    target_revision: int | None
    alive: bool
    disconnect_reported: bool

    def send(self, message: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class Engine:
    def __init__(self, store: Store, notify: Callable[[str, Any], None] | None = None):
        self.store = store
        self.lock = threading.RLock()
        self.notify = notify or (lambda kind, value: None)
        self.session: dict[str, Any] | None = None
        self.peer: Peer | None = None
        self.active_id: str | None = None
        self.started_ns: dict[str, int] = {}
        self.number = 0
        self.listening = "未监听"
        self.fatal: str | None = None

    def _emit(self, kind: str, value: Any = None) -> None:
        try:
            self.notify(kind, value)
        except Exception:
            # A notification/UI failure must never alter accepted results.
            pass

    def log(self, kind: str, text: str, *, peer: Peer | None = None,
            rid: str | None = None, direction: str = "SYSTEM",
            raw: bytes | None = None, at: str | None = None) -> None:
        sid = peer.session_id if peer and peer.session_id else self.session_id
        self.store.audit(sid, rid, peer.name if peer else "", direction, kind, text, raw, at)
        if kind not in {"ping", "pong"}:
            self._emit("event", {"at": at or utc_now(), "kind": kind, "direction": direction,
                                 "peer": peer.name if peer else "", "text": text[:1500]})

    @property
    def session_id(self) -> str | None:
        return self.session["id"] if self.session else None

    def fail(self, error: BaseException) -> None:
        """Fail closed if persistent evidence cannot be written."""
        with self.lock:
            self.fatal = f"证据保存失败，已冻结测试并断开客户端。请保留数据后重启：{error}"
            if self.peer:
                self.peer.close()
            self._emit("fatal", self.fatal)
            self._emit("dirty")

    def _healthy(self) -> None:
        if self.fatal:
            raise ValueError(self.fatal)

    def new_session(self, team: str, client_id: str, competition: str,
                    access_code: str | None = None, *, mode: str = "PRACTICE") -> dict[str, Any]:
        with self.lock:
            self._healthy()
            if self.active_id:
                raise ValueError("当前轮仍在等待结果，请先结束或取消当前轮。")
            team, client_id = team.strip(), client_id.strip()
            protocol.text({"team": team}, "team", 100)
            protocol.text({"client_id": client_id}, "client_id")
            if competition not in {"packaging", "screw"}:
                raise ValueError("未知赛项")
            if mode not in {"PRACTICE", "OFFICIAL"}:
                raise ValueError("请明确选择练习场次或正式场次。")
            code = access_code or secrets.token_hex(4).upper()
            protocol.text({"access_code": code}, "access_code")
            if self.session:
                self.store.close_session(self.session["id"])
                self.log("SESSION_CLOSED", "裁判切换场次，旧连接与旧测试编号失效。")
                self._emit("session_closed", self.session["id"])
            if self.peer:
                self.peer.close()
                self.peer = None
            session = {"id": uuid.uuid4().hex, "created_utc": utc_now(), "team": team,
                       "client_id": client_id, "competition": competition, "access_code": code,
                       "target_revision": 1, "mode": mode,
                       "target": {"box_type": "BOX_A", "product_model": "", "label_type": ""}
                       if competition == "packaging" else {}}
            self.store.create_session(session)
            self.session = session
            self.started_ns.clear()
            self.number = 0
            self.active_id = None
            self.log("SESSION_CREATED", f"新场次：{team} / {competition} / {client_id} / {mode}")
            self._emit("dirty")
            return dict(session)

    def set_target(self, box_type: str, product_model: str = "", label_type: str = "") -> None:
        with self.lock:
            self._healthy()
            if not self.session or self.session["competition"] != "packaging":
                raise ValueError("请先新建包装箱赛项场次。")
            if self.active_id:
                raise ValueError("等待结果期间不允许修改目标。")
            target = {"box_type": box_type.strip(), "product_model": product_model.strip(),
                      "label_type": label_type.strip()}
            protocol.text(target, "box_type", 128)
            for k in ("product_model", "label_type"):
                if len(target[k]) > 128 or any(ord(c) < 32 for c in target[k]):
                    raise ValueError("型号/标签类型最长128个字符，不能含控制字符。")
            revision = self.session["target_revision"] + 1
            self.store.target(self.session["id"], revision, target)
            self.session["target"] = target
            self.session["target_revision"] = revision
            self.log("TARGET_CHANGED", f"目标版本 {revision}：{json_text(target)}")
            if self.peer and self.peer.alive:
                self.peer.target_revision = None
                self._send(self.peer, self.target_message())
            self._emit("dirty")

    def target_message(self, reply_to: str | None = None) -> dict[str, Any]:
        assert self.session is not None
        msg = {"v": 1, "type": "target", "session_id": self.session["id"],
               "competition": self.session["competition"],
               "target_revision": self.session["target_revision"],
               "target": dict(self.session["target"])}
        if reply_to:
            msg["reply_to"] = reply_to
        return msg

    def _send(self, peer: Peer, message: dict[str, Any]) -> bool:
        try:
            peer.send(message)
            return True
        except (OSError, ConnectionError):
            peer.close()
            return False

    def connected(self, peer: Peer) -> None:
        with self.lock:
            self.log("CONNECTED", "TCP已连接，等待身份握手。", peer=peer)

    def disconnected(self, peer: Peer, reason: str) -> None:
        with self.lock:
            if getattr(peer, "disconnect_reported", False):
                return
            peer.disconnect_reported = True
            if self.fatal:
                if self.peer is peer:
                    self.peer = None
                self._emit("dirty")
                return
            if self.peer is peer:
                if self.active_id:
                    self.store.increment(self.active_id, "disconnects")
                self.peer = None
                self.log("DISCONNECTED", reason + "；本轮原有时限不会因重连重置。",
                         peer=peer, rid=self.active_id)
            else:
                self.log("CONNECTION_CLOSED", reason, peer=peer)
            self._emit("dirty")

    def record_wire(self, peer: Peer, raw: bytes, direction: str,
                    kind: str = "FRAME", at: str | None = None) -> None:
        with self.lock:
            self.log(kind, raw.decode("utf-8", "backslashreplace").rstrip("\n"),
                     peer=peer, direction=direction, raw=raw, at=at)

    def protocol_error(self, peer: Peer, exc: protocol.ProtocolError,
                       reply_to: str | None = None, rid: str | None = None,
                       count: bool = True) -> None:
        # Only authenticated connections can affect per-round counters.
        if self.peer is peer and count:
            row = self.store.get_round(rid) if rid else None
            target_rid = rid if row and row["session_id"] == self.session_id else self.active_id
            if target_rid:
                self.store.increment(target_rid, "protocol_errors")
        self.log(exc.code, str(exc), peer=peer, rid=rid or self.active_id)
        message: dict[str, Any] = {"v": 1, "type": "error", "code": exc.code, "message": str(exc)}
        if reply_to:
            message["reply_to"] = reply_to
        self._send(peer, message)
        self._emit("dirty")

    def receive(self, peer: Peer, raw: bytes) -> bool:
        """Process a complete frame INCLUDING LF. False means a protocol error.

        Timing ends when the complete frame enters this serialized application
        handler, before evidence writes/JSON parsing, not at the camera or NIC.
        """
        with self.lock:
            if self.fatal:
                peer.close()
                return False
            received_ns, received_utc = time.monotonic_ns(), utc_now()
            message: dict[str, Any] = {}
            try:
                self.record_wire(peer, raw, "RX", at=received_utc)
                message = protocol.decode(raw[:-1])
                protocol.validate(message)
                kind = message["type"]
                if kind == "hello":
                    self._hello(peer, message)
                    return True
                if self.peer is not peer or peer.session_id != self.session_id:
                    raise protocol.ProtocolError("NOT_AUTHENTICATED", "send a valid hello first")
                if message["session_id"] != self.session_id:
                    raise protocol.ProtocolError("SESSION_MISMATCH", "session_id does not match the current session")
                if kind == "ping":
                    self._send(peer, {"v": 1, "type": "pong", "reply_to": message["msg_id"],
                                      "session_id": self.session_id})
                elif kind == "get_target":
                    self._send(peer, self.target_message(message["msg_id"]))
                elif kind == "target_ack":
                    assert self.session is not None
                    if message["target_revision"] != self.session["target_revision"]:
                        raise protocol.ProtocolError("TARGET_MISMATCH", "acknowledge the current target_revision")
                    peer.target_revision = message["target_revision"]
                    self._send(peer, {"v": 1, "type": "ack", "reply_to": message["msg_id"],
                                      "session_id": self.session_id, "for": "target_ack"})
                    self.log("TARGET_ACKED", f"客户端已确认目标版本 {peer.target_revision}", peer=peer)
                    self._emit("dirty")
                elif kind == "get_state":
                    self._send(peer, {"v": 1, "type": "state", "reply_to": message["msg_id"],
                                      "session_id": self.session_id,
                                      "active_round": self.public_round(self.active_id) if self.active_id else None})
                elif kind == "result":
                    self._result(peer, message, received_ns, received_utc)
                return True
            except protocol.ProtocolError as exc:
                mid = message.get("msg_id")
                rid = message.get("round_id")
                self.protocol_error(peer, exc, mid if isinstance(mid, str) else None,
                                    rid if isinstance(rid, str) else None)
                # An invalid first handshake must not linger or gain privileges.
                if peer is not self.peer:
                    peer.close()
                return False
            except sqlite3.Error as exc:
                self.fail(exc)
                return False

    def _hello(self, peer: Peer, message: dict[str, Any]) -> None:
        if not self.session:
            raise protocol.ProtocolError("NO_SESSION", "judge must create a session before connecting")
        if self.peer is peer:
            raise protocol.ProtocolError("ALREADY_AUTHENTICATED", "hello may be sent only once per connection")
        if (message["client_id"] != self.session["client_id"] or
                not hmac.compare_digest(message["access_code"].encode("utf-8"), self.session["access_code"].encode("utf-8"))):
            raise protocol.ProtocolError("AUTH_FAILED", "client_id or access_code is incorrect")
        if self.peer and self.peer.alive:
            raise protocol.ProtocolError("BUSY", "another client already owns this session")
        # Count a closed authenticated connection even if its cleanup callback
        # races behind this new handshake. The later callback is idempotent.
        if self.peer and not self.peer.alive:
            self.disconnected(self.peer, "旧连接已关闭，新连接正在恢复")
        self.peer = peer
        peer.session_id = self.session["id"]
        peer.client_id = message["client_id"]
        peer.target_revision = None
        response = self.target_message()
        response.update(type="hello_ok", reply_to=message["msg_id"], heartbeat_interval_s=5,
                        idle_timeout_s=30, max_frame_bytes=protocol.MAX_FRAME)
        self.log("AUTHENTICATED", f"身份已核对：{peer.client_id}", peer=peer)
        self._send(peer, response)
        self._emit("dirty")

    def public_round(self, rid: str) -> dict[str, Any]:
        row = self.store.get_round(rid)
        if row is None:
            raise ValueError("轮次不存在")
        # Whitelist: never include sample name, judge notes, expected or matched.
        return {"v": 1, "type": "round", "session_id": row["session_id"],
                "round_id": rid, "action": row["action"], "target_revision": row["target_revision"],
                "target": json.loads(row["target_json"]), "timeout_ms": row["timeout_ms"],
                "started_utc": row["created_utc"], "timer_restarted": False}

    def start_round(self, case_name: str, expected: str, timeout_ms: int = 0,
                    action: str = "arm", notes: str = "") -> str:
        with self.lock:
            self._healthy()
            if not self.session:
                raise ValueError("请先新建场次。")
            if self.active_id:
                raise ValueError("当前轮还未结束，不能重复开始。")
            peer = self.peer
            if not peer or not peer.alive:
                raise ValueError("参赛客户端未连接或未通过身份握手。")
            if peer.target_revision != self.session["target_revision"]:
                raise ValueError("客户端尚未确认当前目标版本（target_ack）。")
            case_name = case_name.strip()
            protocol.text({"case_name": case_name}, "case_name", 200)
            if expected not in {"OK", "NG"} or action not in {"arm", "trigger"}:
                raise ValueError("预期结果或控制方式无效。")
            if type(timeout_ms) is not int or not 0 <= timeout_ms <= 3_600_000:
                raise ValueError("时限必须为0～3600000毫秒，0表示仅记录耗时。")
            if len(notes) > 2000:
                raise ValueError("备注不能超过2000字符。")
            rid = uuid.uuid4().hex
            start_ns = time.monotonic_ns()
            row = {"id": rid, "session_id": self.session["id"], "number": self.number + 1,
                   "created_utc": utc_now(), "case_name": case_name, "expected": expected,
                   "timeout_ms": timeout_ms, "action": action,
                   "target_revision": self.session["target_revision"],
                   "target_json": json_text(self.session["target"]), "notes": notes, "status": "WAITING"}
            self.store.add_round(row)
            self.number += 1
            self.active_id = rid
            # Includes round creation/persistence and command dispatch, not just inference.
            self.started_ns[rid] = start_ns
            self.log("ROUND_STARTED", f"第{self.number}轮：{case_name}；裁判预期 {expected}；时限 {timeout_ms}ms", rid=rid)
            if not self._send(peer, self.public_round(rid)):
                self.store.update_round(rid, status="INTERRUPTED")
                self.active_id = None
                self.log("ROUND_SEND_FAILED", "本轮命令发送失败，不计为正常检测完成。", rid=rid)
            self._emit("dirty")
            return rid

    def _result(self, peer: Peer, message: dict[str, Any], received_ns: int, received_utc: str) -> None:
        sid, rid, mid = message["session_id"], message["round_id"], message["msg_id"]
        digest = protocol.fingerprint(message)
        receipt = self.store.receipt(sid, mid)
        if receipt:
            if receipt["fingerprint"] != digest:
                raise protocol.ProtocolError("MSG_ID_CONFLICT", "accepted msg_id cannot be reused with different content")
            ack = json.loads(receipt["response_json"])
            ack["duplicate"] = True
            self.store.increment(receipt["round_id"], "retries")
            self.log("IDEMPOTENT_RETRY", "同msg_id同内容重传：不新增结果，重传次数保留。", peer=peer, rid=rid)
            self._send(peer, ack)
            self._emit("dirty")
            return
        row = self.store.get_round(rid)
        if not row or row["session_id"] != sid:
            raise protocol.ProtocolError("UNKNOWN_ROUND", "round_id does not belong to this session")
        if row["actual"] is not None:
            self.store.increment(rid, "duplicates")
            raise protocol.ProtocolError("DUPLICATE_RESULT", "a final result already exists for this round")
        if row["status"] not in {"WAITING", "TIMEOUT"} or rid not in self.started_ns:
            raise protocol.ProtocolError("ROUND_CLOSED", "round was cancelled or interrupted")
        if message["target_revision"] != row["target_revision"]:
            raise protocol.ProtocolError("TARGET_MISMATCH", "result target_revision does not match its round")
        elapsed_ms = max(0.0, (received_ns - self.started_ns[rid]) / 1_000_000)
        late = bool(row["timeout_ms"] and elapsed_ms > row["timeout_ms"])
        status = "LATE" if late else "RECEIVED"
        ack = {"v": 1, "type": "result_ack", "reply_to": mid, "session_id": sid,
               "round_id": rid, "recorded": True, "duplicate": False}
        self.store.record_result(row, message, digest, ack, elapsed_ms, status, received_utc, peer.name)
        if self.active_id == rid:
            self.active_id = None
        # No expected result or correctness feedback is disclosed to competitors.
        self._send(peer, ack)
        self.log("RESULT", f"第{row['number']}轮收到 {message['verdict']}；"
                 f"核对{'一致' if message['verdict'] == row['expected'] else '不一致'}；"
                 f"{status}，{elapsed_ms:.1f}ms", peer=peer, rid=rid)
        self._emit("dirty")

    def tick(self) -> None:
        with self.lock:
            if self.fatal or not self.active_id:
                return
            try:
                rid = self.active_id
                row = self.store.get_round(rid)
                if row and row["timeout_ms"] and (time.monotonic_ns() - self.started_ns[rid]) / 1_000_000 > row["timeout_ms"]:
                    self.store.update_round(rid, status="TIMEOUT")
                    self.active_id = None
                    self.log("TIMEOUT", "时限内没有收到有效最终结果；不会自动补成NG。", rid=rid)
                    self._emit("dirty")
            except sqlite3.Error as exc:
                self.fail(exc)

    def cancel_round(self, reason: str, status: str = "CANCELLED") -> None:
        with self.lock:
            self._healthy()
            if not self.active_id:
                raise ValueError("没有正在等待的轮次。")
            if not reason.strip():
                raise ValueError("必须填写取消/中断原因。")
            if status not in {"CANCELLED", "INTERRUPTED"}:
                raise ValueError("无效状态")
            rid = self.active_id
            self.store.update_round(rid, status=status)
            self.active_id = None
            self.log(status, reason, rid=rid)
            if self.peer and self.peer.alive:
                self._send(self.peer, {"v": 1, "type": "round_cancelled", "session_id": self.session_id,
                                       "round_id": rid})  # Do not disclose private judge notes.
            self._emit("dirty")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            session = dict(self.session) if self.session else None
            peer = self.peer
            return {"session": session, "listening": self.listening, "fatal": self.fatal,
                    "client": f"{peer.client_id} @ {peer.name}" if peer and peer.alive else "未连接",
                    "ready": bool(session and peer and peer.alive and peer.target_revision == session["target_revision"]),
                    "active_id": self.active_id,
                    "rows": self.store.rows(self.session["id"]) if self.session else [],
                    "stats": self.store.stats(self.session["id"]) if self.session else {},
                    "sessions": self.store.sessions()}

    def export(self, sid: str, parent: Path) -> Path:
        # Store uses an independent read transaction; never lock the result path
        # while writing export files or computing hashes on a slow destination.
        return self.store.export(sid, parent)

    def close_session(self) -> None:
        with self.lock:
            if self.fatal:
                return
            if self.active_id:
                self.cancel_round("程序关闭，未完成轮次中断。", "INTERRUPTED")
            if self.session:
                self.store.close_session(self.session["id"])
                self.log("SESSION_CLOSED", "程序正常关闭场次。")
