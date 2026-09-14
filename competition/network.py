"""Bounded threaded TCP listener; UI code never runs on these threads."""
from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from collections import deque
from typing import Any

from . import protocol
from .core import Engine
from .storage import utc_now


class WirePeer:
    def __init__(self, sock: socket.socket, address: tuple[str, int], engine: Engine):
        self.sock = sock
        self.name = f"{address[0]}:{address[1]}"
        self.engine = engine
        self.session_id: str | None = None
        self.client_id: str | None = None
        self.target_revision: int | None = None
        self.alive = True
        self.disconnect_reported = False
        self.send_lock = threading.Lock()
        self.sock.settimeout(0.25)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, message: dict[str, Any]) -> None:
        raw = protocol.encode(message)
        with self.send_lock:
            if not self.alive:
                raise ConnectionError("peer closed")
            at = utc_now()
            try:
                self.sock.sendall(raw)
            except OSError:
                # Bytes may have been partially sent; never claim delivery.
                self.engine.record_wire(self, raw, "TX_FAILED", str(message["type"]), at)
                raise
            self.engine.record_wire(self, raw, "TX", str(message["type"]), at)

    def run(self) -> None:
        buffer = bytearray()
        connected_at = last_valid = time.monotonic()
        partial_at: float | None = None
        recent: deque[float] = deque()
        errors = 0
        reason = "远端关闭连接"
        try:
            self.engine.connected(self)
            while self.alive:
                now = time.monotonic()
                if self.session_id is None and now - connected_at > 5:
                    reason = "5秒内未完成身份握手"
                    break
                if self.session_id is not None and now - last_valid > 30:
                    reason = "30秒未收到有效消息/心跳"
                    break
                if partial_at is not None and now - partial_at > 5:
                    reason = "不完整报文超过5秒"
                    break
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                if not buffer:
                    partial_at = time.monotonic()
                buffer.extend(chunk)
                while b"\n" in buffer and self.alive:
                    end = buffer.index(b"\n")
                    raw = bytes(buffer[:end + 1])
                    del buffer[:end + 1]
                    partial_at = time.monotonic() if buffer else None
                    now = time.monotonic()
                    while recent and now - recent[0] > 1:
                        recent.popleft()
                    recent.append(now)
                    if len(recent) > 100:
                        self.engine.record_wire(self, raw, "RX", "RATE_LIMIT_FRAME")
                        with self.engine.lock:
                            self.engine.protocol_error(self, protocol.ProtocolError("RATE_LIMIT", "maximum 100 frames per second"))
                        reason = "消息发送频率超限"
                        self.close()
                        break
                    if end > protocol.MAX_FRAME:
                        self.engine.record_wire(self, raw, "RX", "OVERSIZE_FRAME")
                        with self.engine.lock:
                            self.engine.protocol_error(self, protocol.ProtocolError("FRAME_TOO_LARGE", "frame exceeds 16384 bytes"))
                        reason = "报文长度超限"
                        self.close()
                        break
                    if self.engine.receive(self, raw):
                        last_valid = time.monotonic()
                    else:
                        errors += 1
                        if errors >= 5:
                            reason = "本连接累计5次协议错误"
                            self.close()
                            break
                if len(buffer) > protocol.MAX_FRAME:
                    self.engine.record_wire(self, bytes(buffer), "RX_PARTIAL", "OVERSIZE_PARTIAL")
                    buffer.clear()
                    with self.engine.lock:
                        self.engine.protocol_error(self, protocol.ProtocolError("FRAME_TOO_LARGE", "unterminated frame exceeds 16384 bytes"))
                    reason = "未终止报文长度超限"
                    break
        except sqlite3.Error as exc:
            self.engine.fail(exc)
            reason = "数据库写入异常"
        except OSError as exc:
            reason = f"套接字关闭或通信失败：{exc}"
        except Exception as exc:
            # Unexpected processing failures are visible and must not fabricate an NG.
            reason = f"连接处理异常：{type(exc).__name__}: {exc}"
            self.engine._emit("error", reason)
        finally:
            self.close()
            try:
                if buffer and not self.engine.fatal:
                    self.engine.record_wire(self, bytes(buffer), "RX_PARTIAL", "INCOMPLETE_FRAME")
                    with self.engine.lock:
                        if self.engine.peer is self and self.engine.active_id:
                            self.engine.store.increment(self.engine.active_id, "protocol_errors")
                self.engine.disconnected(self, reason)
            except sqlite3.Error as exc:
                self.engine.fail(exc)


class Service:
    """Single listener, up to 8 transport connections, one authenticated contestant."""
    def __init__(self, engine: Engine):
        self.engine = engine
        self.listener: socket.socket | None = None
        self.stop_event = threading.Event()
        self.registry_lock = threading.Lock()
        self.peers: dict[WirePeer, threading.Thread] = {}
        self.accept_thread: threading.Thread | None = None
        self.timer_thread: threading.Thread | None = None
        self.address: tuple[str, int] | None = None

    def start(self, host: str = "127.0.0.1", port: int = 9000) -> tuple[str, int]:
        if self.listener is not None:
            raise ValueError("服务已经在监听。")
        self.engine._healthy()
        if not 0 <= port <= 65535:
            raise ValueError("端口必须在1～65535之间。")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(8)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise
        self.listener = listener
        self.address = listener.getsockname()
        self.stop_event.clear()
        with self.engine.lock:
            self.engine.listening = f"{self.address[0]}:{self.address[1]}"
            self.engine.log("LISTENING", f"开始监听 {self.engine.listening}")
            self.engine._emit("dirty")
        self.accept_thread = threading.Thread(target=self._accept, name="tcp-accept", daemon=True)
        self.timer_thread = threading.Thread(target=self._timer, name="round-timer", daemon=True)
        self.accept_thread.start()
        self.timer_thread.start()
        return self.address

    def _timer(self) -> None:
        while not self.stop_event.wait(0.05):
            self.engine.tick()

    def _accept(self) -> None:
        listener = self.listener
        assert listener is not None
        while not self.stop_event.is_set():
            try:
                sock, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self.stop_event.is_set():
                sock.close()
                break
            with self.registry_lock:
                if len(self.peers) >= 8:
                    sock.close()
                    continue
                peer = WirePeer(sock, address, self.engine)
                thread = threading.Thread(target=self._run_peer, args=(peer,),
                                          name=f"tcp-{peer.name}", daemon=True)
                self.peers[peer] = thread
                thread.start()

    def _run_peer(self, peer: WirePeer) -> None:
        try:
            peer.run()
        finally:
            with self.registry_lock:
                self.peers.pop(peer, None)

    def stop(self) -> None:
        # All joins happen OUTSIDE engine.lock to avoid disconnect-handler deadlocks.
        with self.engine.lock:
            if self.engine.active_id and not self.engine.fatal:
                self.engine.cancel_round("裁判停止TCP监听，本轮中断。", "INTERRUPTED")
        self.stop_event.set()
        listener, self.listener = self.listener, None
        if listener:
            listener.close()
        if self.accept_thread:
            self.accept_thread.join(2)
        with self.registry_lock:
            peers = list(self.peers.items())
        for peer, _ in peers:
            peer.close()
        for _, thread in peers:
            thread.join(2)
        if self.timer_thread:
            self.timer_thread.join(2)
        with self.engine.lock:
            self.engine.listening = "未监听"
            if not self.engine.fatal:
                self.engine.log("LISTENER_STOPPED", "TCP监听已停止。")
            self.engine._emit("dirty")
