"""Read-only, whitelisted public display, isolated from the referee UI.

This is a local / trusted-LAN HTTP appliance, NOT a public-internet web service.
Only the background publisher reads Engine/Store; browsers read a bounded cache.
No judge controls, filesystem browser, raw audits, or result writes are exposed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .core import Engine
from .storage import utc_now

LOG = logging.getLogger(__name__)
DEFAULT_TITLE = "工业视觉技能竞赛"
DEFAULT_SUBTITLE = "双赛项现场展示 · 以视觉见实力"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


def validate_branding(title: str, subtitle: str) -> tuple[str, str]:
    for value, limit, name in ((title, 28, "展板标题"), (subtitle, 56, "展板副标题")):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"{name}不能为空，且不能超过{limit}个字符。")
        if any(ord(c) < 32 for c in value):
            raise ValueError(f"{name}不能包含控制字符。")
    return title.strip(), subtitle.strip()


def public_snapshot(engine: Engine, title: str, subtitle: str) -> dict[str, Any] | None:
    """Explicit allowlist: never serialize engine.snapshot() or private row dicts.

    Keep this separate even if the desktop schema changes. None means busy: keep
    the last published snapshot and let its age make it visibly stale.
    """
    if not engine.lock.acquire(timeout=0.04):
        return None
    try:
        session = engine.session
        peer = engine.peer
        connected = bool(session and peer and peer.alive)
        ready = bool(connected and peer.target_revision == session["target_revision"])
        out: dict[str, Any] = {
            "schema": 1, "demo": False, "title": title, "subtitle": subtitle,
            "generated_utc": utc_now(), "session": None,
            "connection": {"listening": engine.listening != "未监听", "connected": connected,
                           "ready": ready, "fault": bool(engine.fatal)},
            "counts": {"total": 0, "received": 0, "ok": 0, "ng": 0, "timeout": 0, "late": 0},
            "latest": None, "recent": [], "timings": [],
        }
        if not session:
            return out
        out["session"] = {"display_id": hashlib.sha256(session["id"].encode()).hexdigest()[:12],
                          "team": session["team"], "competition": session["competition"],
                          "mode": session.get("mode", "LEGACY")}
        # Consistent aggregate across ALL rounds; only bounded rows go to browsers.
        with engine.store.lock:
            db = engine.store.db
            counts = db.execute("""SELECT COUNT(*) total,
                SUM(CASE WHEN actual IS NOT NULL THEN 1 ELSE 0 END) received,
                SUM(CASE WHEN actual='OK' THEN 1 ELSE 0 END) ok,
                SUM(CASE WHEN actual='NG' THEN 1 ELSE 0 END) ng,
                SUM(CASE WHEN status='TIMEOUT' THEN 1 ELSE 0 END) timeout,
                SUM(CASE WHEN status='LATE' THEN 1 ELSE 0 END) late
                FROM rounds WHERE session_id=?""", (session["id"],)).fetchone()
            out["counts"] = {k: int(v or 0) for k, v in dict(counts).items()}
            rows = db.execute("""SELECT id,number,status,actual,elapsed_ms,timeout_ms,
                created_utc,received_utc,action FROM rounds WHERE session_id=?
                ORDER BY number DESC LIMIT 20""", (session["id"],)).fetchall()
            for raw in rows:
                row = dict(raw)
                elapsed = row["elapsed_ms"]
                if row["status"] == "WAITING" and row["id"] == engine.active_id:
                    started = engine.started_ns.get(row["id"])
                    if started is not None:
                        elapsed = max(0.0, (time.monotonic_ns() - started) / 1_000_000)
                item = {"number": row["number"], "status": row["status"], "actual": row["actual"], "action": row["action"],
                        "elapsed_ms": round(elapsed, 1) if elapsed is not None else None,
                        "timeout_ms": row["timeout_ms"], "started_utc": row["created_utc"],
                        "received_utc": row["received_utc"]}
                out["recent"].append(item)
                if row["elapsed_ms"] is not None:
                    out["timings"].append({"number": row["number"],
                                           "elapsed_ms": round(row["elapsed_ms"], 1),
                                           "late": row["status"] == "LATE"})
            out["latest"] = out["recent"][0] if out["recent"] else None
            out["recent"] = out["recent"][:6]
            out["timings"].reverse()
        return out
    finally:
        engine.lock.release()


class SnapshotCache:
    def __init__(self, engine: Engine, title: str, subtitle: str):
        self.engine = engine
        self.lock = threading.Lock()
        self.title, self.subtitle = validate_branding(title, subtitle)
        self.value: dict[str, Any] | None = None
        self.updated = 0.0
        self.sequence = 0
        self.error = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def set_branding(self, title: str, subtitle: str) -> None:
        values = validate_branding(title, subtitle)
        with self.lock:
            self.title, self.subtitle = values

    def refresh(self) -> None:
        with self.lock:
            title, subtitle = self.title, self.subtitle
        value = public_snapshot(self.engine, title, subtitle)
        if value is None:
            return
        with self.lock:
            self.value, self.updated, self.error = value, time.monotonic(), False
            self.sequence += 1

    def read(self) -> dict[str, Any]:
        with self.lock:
            age = (time.monotonic() - self.updated) * 1000 if self.updated else None
            return {"available": self.value is not None, "sequence": self.sequence,
                    "age_ms": round(age, 1) if age is not None else None,
                    "stale": age is None or age > 2500 or self.error,
                    "data": copy.deepcopy(self.value)}

    def start(self) -> None:
        self.stop_event.clear()
        self.refresh()
        self.thread = threading.Thread(target=self._run, name="public-display-publisher", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.wait(0.35):
            try:
                self.refresh()
            except Exception:
                LOG.exception("Public display snapshot refresh failed")
                with self.lock:
                    self.error = True

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError("展板快照线程尚未退出，请保留数据并关闭进程。")


class _BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        sock, addr = super().get_request()
        sock.settimeout(3)
        return sock, addr

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class DisplayService:
    def __init__(self, engine: Engine, title: str = DEFAULT_TITLE, subtitle: str = DEFAULT_SUBTITLE):
        self.cache = SnapshotCache(engine, title, subtitle)
        root = Path(__file__).resolve().parent / "web"
        self.assets = {route: ((root / name).read_bytes(), mime) for route, (name, mime) in ASSETS.items()}
        self.server: _BoundedHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.address: tuple[str, int] | None = None

    @property
    def url(self) -> str:
        if not self.address:
            return ""
        host, port = self.address
        return f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"

    def start(self, host: str = "127.0.0.1", port: int = 9080) -> tuple[str, int]:
        if self.server:
            raise ValueError("展板已启动。")
        if not 0 <= port <= 65535:
            raise ValueError("展板端口必须为0～65535；0仅用于自动分配端口。")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VisionDisplay/1.2"
            sys_version = ""
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:
                pass

            def send_body(self, status: int, body: bytes, mime: str, head: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                                 "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                                 "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if not head:
                    try:
                        self.wfile.write(body)
                    except (OSError, ConnectionError):
                        pass

            def route(self, head: bool = False) -> None:
                if len(self.path) > 2048:
                    self.send_body(414, b"Request target too long", "text/plain", head)
                    return
                try:
                    path = urlsplit(self.path).path
                except ValueError:
                    self.send_body(400, b"Bad request", "text/plain", head)
                    return
                if path == "/api/display":
                    payload = json.dumps(owner.cache.read(), ensure_ascii=False, allow_nan=False,
                                         separators=(",", ":")).encode("utf-8")
                    self.send_body(200, payload, "application/json; charset=utf-8", head)
                elif path in owner.assets:
                    content, mime = owner.assets[path]
                    self.send_body(200, content, mime, head)
                else:
                    self.send_body(404, b"Not found", "text/plain; charset=utf-8", head)

            def do_GET(self) -> None:
                self.route()

            def do_HEAD(self) -> None:
                self.route(True)

            def do_POST(self) -> None:
                self.send_body(405, b"Read-only display", "text/plain; charset=utf-8")

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST

        server = _BoundedHTTPServer((host, port), Handler)
        try:
            self.cache.start()
        except Exception:
            server.server_close()
            raise
        self.server, self.address = server, server.server_address
        self.thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .2},
                                       name="public-display-http", daemon=True)
        self.thread.start()
        return self.address

    def stop(self) -> None:
        server, self.server = self.server, None
        if server:
            server.shutdown()
            server.server_close()
        if self.thread:
            self.thread.join(3)
        self.cache.stop()
        self.address = None
