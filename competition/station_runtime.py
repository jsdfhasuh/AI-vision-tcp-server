"""Four independently owned TCP station slots, sharing one durable observation store."""
from __future__ import annotations

from collections import deque
import logging
import os
import socket
import sqlite3
import threading
import time

from . import station_protocol as p
from .storage import utc_now
from .station_store import StationStore
from .web_config import WebConfig

LOG = logging.getLogger(__name__)


class StationEngine:
    def __init__(self, store: StationStore):
        self.store = store
        self.lock = threading.RLock()
        self.peers = {}
        self.fatal = None

    def fail(self, exc):
        with self.lock:
            self.fatal = '数据保存失败，已停止接收。请保留数据目录并检查磁盘和服务日志。'
            LOG.error('Station storage failure: %s', exc)
            for peer in list(self.peers.values()):
                peer.close()

    def receive(self, peer, raw: bytes) -> bool:
        with self.lock:
            if self.fatal:
                peer.close()
                return False
            at = utc_now()
            message = None
            try:
                message = p.validate(p.decode(raw[:-1]))
                if message['type'] in ('hello', 'result'):
                    key = p.route(message['project'], message['station'])
                    if peer.key and peer.key != key:
                        raise p.ProtocolError('STATION_MISMATCH', 'one connection cannot switch project or station')
                    owner = self.peers.get(key)
                    if owner is not None and owner is not peer and owner.alive:
                        raise p.ProtocolError('STATION_BUSY', 'this station already has a live connection')
                    if peer.key is None:
                        peer.key = key
                        self.peers[key] = peer
                elif peer.key is None:
                    raise p.ProtocolError('IDENTIFY_FIRST', 'send hello or a complete result first')
                self.store.event(peer.key, peer.name, 'RX', message['type'], raw, at)
                reply = {'v': 2, 'reply_to': message['msg_id']}
                if message['type'] == 'hello':
                    reply.update(type='hello_ok', project=peer.key[0], station=peer.key[1],
                                 heartbeat_interval_s=5, idle_timeout_s=30, max_frame_bytes=p.MAX_FRAME)
                elif message['type'] == 'ping':
                    reply.update(type='pong')
                else:
                    row, duplicate = self.store.record(message, at)
                    if not duplicate:
                        peer.worker_id = message['worker_id']
                        peer.last_result_id = row['id']
                    # ACK acknowledges persistence only. Never disclose the standard or match result.
                    reply.update(type='result_ack', recorded=True, duplicate=duplicate, record_id=row['id'])
                peer.send(reply)
                return True
            except p.ProtocolError as exc:
                self.store.event(peer.key, peer.name, 'RX_ERROR', exc.code, raw, at)
                reply = {'v': 2, 'type': 'error', 'code': exc.code, 'message': str(exc)}
                if isinstance(message, dict) and isinstance(message.get('msg_id'), str):
                    reply['reply_to'] = message['msg_id']
                peer.send(reply)
                return False

    def disconnected(self, peer, reason):
        with self.lock:
            if peer.key and self.peers.get(peer.key) is peer:
                self.peers.pop(peer.key, None)
            if not self.fatal:
                self.store.event(peer.key, peer.name, 'SYSTEM', 'DISCONNECTED', reason.encode('utf-8'))

    def snapshot(self, project: str, *, public: bool = False) -> dict:
        p.route(project, 1)
        with self.lock:
            stations = []
            for number in (1, 2):
                peer = self.peers.get((project, number))
                latest = self.store.latest(project, number)
                connected = bool(peer and peer.alive and not self.fatal)
                stations.append({'station': number, 'connected': connected,
                    'worker_id': peer.worker_id if connected else None,
                    'address': peer.name if connected and not public else None,
                    'last_is_current': bool(connected and latest and peer.last_result_id == latest['id']),
                    'latest': (self.store.public if public else self.store.admin_row)(latest)})
            answer = {'project': project, 'stations': stations, 'fatal': self.fatal,
                      'generated_utc': utc_now()}
            if not public:
                answer['standard'] = self.store.standard()
                if project == 'packaging':
                    answer['catalog'] = self.store.catalog()
        # Query through independent read connections, outside the engine lock.
        answer['records'] = [(self.store.public if public else self.store.admin_row)(r) for r in self.store.records(project)]
        if not public:
            answer['logs'] = self.store.logs(project)
        return answer


class StationPeer:
    def __init__(self, sock, address, engine: StationEngine):
        self.sock, self.engine = sock, engine
        self.name = f'{address[0]}:{address[1]}'
        self.key = None
        self.worker_id = None
        self.last_result_id = None
        self.alive = True
        self.sock.settimeout(.25)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def send(self, message: dict):
        raw = p.encode(message)
        try:
            self.sock.sendall(raw)
        except OSError:
            self.engine.store.event(self.key, self.name, 'TX_FAILED', message['type'], raw)
            raise
        self.engine.store.event(self.key, self.name, 'TX', message['type'], raw)

    def run(self):
        buffer = bytearray()
        connected_at = last_valid = time.monotonic()
        partial_at = None
        frames = deque()
        errors = 0
        reason = '远端关闭连接'
        try:
            while self.alive:
                now = time.monotonic()
                if self.key is None and now - connected_at > 5:
                    reason = '5秒内未标识工位'; break
                if self.key is not None and now - last_valid > 30:
                    reason = '30秒未收到有效消息或心跳'; break
                if partial_at is not None and now - partial_at > 5:
                    reason = '不完整报文超过5秒'; break
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                if not buffer:
                    partial_at = time.monotonic()
                buffer.extend(chunk)
                while b'\n' in buffer and self.alive:
                    end = buffer.index(b'\n')
                    raw = bytes(buffer[:end + 1]); del buffer[:end + 1]
                    partial_at = time.monotonic() if buffer else None
                    now = time.monotonic()
                    while frames and now - frames[0] > 1:
                        frames.popleft()
                    frames.append(now)
                    if len(frames) > 100 or end > p.MAX_FRAME:
                        reason = '报文超长或频率超过100条/秒'
                        self.engine.store.event(self.key, self.name, 'RX_ERROR', 'LIMIT', raw)
                        self.send({'v': 2, 'type': 'error', 'code': 'LIMIT', 'message': reason})
                        self.close(); break
                    if self.engine.receive(self, raw):
                        last_valid = time.monotonic()
                    else:
                        errors += 1
                        if errors >= 5:
                            reason = '累计5次协议错误'; self.close(); break
                if len(buffer) > p.MAX_FRAME:
                    reason = '未终止报文长度超过16KiB'; break
        except sqlite3.Error as exc:
            self.engine.fail(exc); reason = '数据库写入失败'
        except OSError:
            reason = '套接字关闭或发送失败'
        except Exception:
            LOG.exception('Unexpected station connection failure')
            reason = '连接处理异常，未伪造检测结果'
        finally:
            self.close()
            try:
                if buffer and not self.engine.fatal:
                    self.engine.store.event(self.key, self.name, 'RX_PARTIAL', 'INCOMPLETE', bytes(buffer))
                self.engine.disconnected(self, reason)
            except sqlite3.Error as exc:
                self.engine.fail(exc)


class StationService:
    def __init__(self, engine: StationEngine):
        self.engine = engine
        self.listener = None
        self.address = None
        self.thread = None
        self.lock = threading.Lock()
        self.clients = {}
        self.stopping = threading.Event()

    def start(self, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port)); sock.listen(16); sock.settimeout(.2)
        except BaseException:
            sock.close(); raise
        self.listener, self.address = sock, sock.getsockname()
        self.thread = threading.Thread(target=self._accept, name='station-accept', daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopping.is_set():
            try:
                sock, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                if self.stopping.is_set() or len(self.clients) >= 16 or self.engine.fatal:
                    sock.close(); continue
                peer = StationPeer(sock, address, self.engine)
                thread = threading.Thread(target=self._run, args=(peer,), daemon=True)
                self.clients[peer] = thread
                thread.start()

    def _run(self, peer):
        try:
            peer.run()
        finally:
            with self.lock:
                self.clients.pop(peer, None)

    def stop(self):
        self.stopping.set()
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(2)
        with self.lock:
            clients = list(self.clients.items())
        for peer, _ in clients:
            peer.close()
        deadline = time.monotonic() + 10
        for _, thread in clients:
            thread.join(max(0, deadline - time.monotonic()))
        if any(t.is_alive() for _, t in clients):
            raise RuntimeError('TCP线程尚未退出，不能关闭正在使用的数据库。')


class StationRuntime:
    def __init__(self, config: WebConfig):
        self.config, self.closing = config, False
        self.backup_lock = threading.Lock()
        self.store = StationStore(config.data_dir / 'station-results.sqlite3')
        self.engine = StationEngine(self.store)
        self.tcp = StationService(self.engine)
        try:
            self.tcp.start(config.tcp_host, config.tcp_port)
        except BaseException:
            self.tcp.stop(); self.store.close(); raise

    @property
    def healthy(self):
        return bool(not self.closing and not self.engine.fatal and self.tcp.thread and self.tcp.thread.is_alive())

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.tcp.stop()
        try:
            if self.config.auto_backup:
                with self.backup_lock:
                    self.store.backup()
        except Exception:
            LOG.exception('Shutdown backup failed; original station database is preserved')
        finally:
            self.store.close()
