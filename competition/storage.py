"""SQLite evidence store. All access is serialized and results commit before ACK."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .dbtools import reader, backup_database, inspect_database, SCHEMA_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def csv_safe(value: Any) -> Any:
    """Prevent formula injection when human-entered text is opened in a spreadsheet."""
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@"))
                                   or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


class DataLock:
    """Prevent two referee processes from recovering/writing the same database."""
    def __init__(self, path: Path):
        self.file = path.open("a+b")
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.file.close()
            raise RuntimeError("此数据目录已被另一个服务端占用，请关闭另一个程序或改用其他 --data-dir。") from exc

    def close(self) -> None:
        if not self.file.closed:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


UI_SETTING_KEYS = {"host", "port", "team", "client_id", "competition", "box_type", "product_model", "label_type", "timeout", "display_title", "display_subtitle", "session_mode"}


class Store:
    def __init__(self, path: Path):
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.data_lock = DataLock(path.with_suffix(".lock"))
        self.lock = threading.RLock()
        try:
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        except BaseException:
            self.data_lock.close()
            raise
        self.db.row_factory = sqlite3.Row
        self.migration_backup: Path | None = None
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            from .schema import initialize
            self.migration_backup = initialize(self.db, path)
            if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("无法启用本地数据库WAL模式，请检查文件系统。")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            self.db.close()
            self.data_lock.close()
            raise
        try:
            # A process restart never silently resumes an unfinished timed round.
            with self.transaction() as db:
                old = db.execute("SELECT id FROM sessions WHERE state='OPEN'").fetchall()
                for row in old:
                    db.execute("UPDATE rounds SET status='INTERRUPTED' WHERE session_id=? AND status='WAITING'", (row[0],))
                    db.execute("UPDATE sessions SET state='INTERRUPTED', ended_utc=? WHERE id=?", (utc_now(), row[0]))
                    self._audit(db, row[0], None, "", "SYSTEM", "RECOVERY",
                                "上次程序未正常结束：未完成轮次标记为中断，不恢复计时。", None)
        except BaseException:
            self.db.close()
            self.data_lock.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            with self.db:
                yield self.db

    @staticmethod
    def _audit(db: sqlite3.Connection, sid: str | None, rid: str | None,
               peer: str, direction: str, kind: str, message: str,
               raw: bytes | None, timestamp: str | None = None) -> None:
        db.execute("INSERT INTO audit(at_utc,session_id,round_id,peer,direction,kind,text,raw_b64) VALUES(?,?,?,?,?,?,?,?)",
                   (timestamp or utc_now(), sid, rid, peer, direction, kind, message,
                    base64.b64encode(raw).decode("ascii") if raw is not None else None))

    def audit(self, sid: str | None, rid: str | None, peer: str, direction: str,
              kind: str, message: str, raw: bytes | None = None,
              timestamp: str | None = None) -> None:
        with self.transaction() as db:
            self._audit(db, sid, rid, peer, direction, kind, message, raw, timestamp)

    def create_session(self, session: dict[str, Any]) -> None:
        with self.transaction() as db:
            mode = session.get("mode", "PRACTICE")
            if mode not in {"PRACTICE", "OFFICIAL"}:
                raise ValueError("新场次必须明确选择练习或正式。")
            db.execute("""INSERT INTO sessions(id,created_utc,ended_utc,state,team,client_id,
                          competition,access_code,target_revision,target_json,mode)
                          VALUES(?,?,NULL,'OPEN',?,?,?,?,?,?,?)""",
                       (session["id"], session["created_utc"], session["team"], session["client_id"],
                        session["competition"], session["access_code"], session["target_revision"],
                        json_text(session["target"]), mode))
            db.execute("INSERT INTO target_revisions VALUES(?,?,?,?,?)",
                       (session["id"], session["target_revision"], json_text(session["target"]),
                        session["created_utc"], "CREATED"))

    def close_session(self, sid: str, state: str = "CLOSED") -> None:
        with self.transaction() as db:
            db.execute("UPDATE sessions SET state=?,ended_utc=? WHERE id=?", (state, utc_now(), sid))

    def target(self, sid: str, revision: int, target: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO target_revisions VALUES(?,?,?,?,?)",
                       (sid, revision, json_text(target), utc_now(), "JUDGE_CHANGE"))
            db.execute("UPDATE sessions SET target_revision=?,target_json=? WHERE id=?",
                       (revision, json_text(target), sid))

    def add_round(self, row: dict[str, Any]) -> None:
        cols = ("id", "session_id", "number", "created_utc", "case_name", "expected", "timeout_ms",
                "action", "target_revision", "target_json", "notes", "status")
        with self.transaction() as db:
            db.execute(f"INSERT INTO rounds({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
                       tuple(row[c] for c in cols))

    def update_round(self, rid: str, **fields: Any) -> None:
        allowed = {"status", "retries", "duplicates", "protocol_errors", "disconnects", "notes"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("invalid round update fields")
        with self.transaction() as db:
            db.execute(f"UPDATE rounds SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                       (*fields.values(), rid))

    def increment(self, rid: str, field: str) -> None:
        if field not in {"retries", "duplicates", "protocol_errors", "disconnects"}:
            raise ValueError("invalid counter")
        with self.transaction() as db:
            db.execute(f"UPDATE rounds SET {field}={field}+1 WHERE id=?", (rid,))

    def get_round(self, rid: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
            return dict(row) if row else None

    def receipt(self, sid: str, mid: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM receipts WHERE session_id=? AND msg_id=?", (sid, mid)).fetchone()
            return dict(row) if row else None

    def record_result(self, row: dict[str, Any], message: dict[str, Any], digest: str,
                      ack: dict[str, Any], elapsed_ms: float, status: str,
                      received_utc: str, peer: str) -> None:
        """The result, deduplication receipt and acceptance event commit together."""
        with self.transaction() as db:
            if message["verdict"] not in {"OK", "NG"} or status not in {"RECEIVED", "LATE"}:
                raise ValueError("无效的最终结果。")
            saved = db.execute("""UPDATE rounds SET received_utc=?,actual=?,matched=?,elapsed_ms=?,status=?
                                  WHERE id=? AND session_id=? AND actual IS NULL AND status IN ('WAITING','TIMEOUT')""",
                       (received_utc, message["verdict"], int(message["verdict"] == row["expected"]),
                        elapsed_ms, status, row["id"], row["session_id"]))
            if saved.rowcount != 1:
                raise sqlite3.IntegrityError("结果已存在或轮次已经关闭，拒绝覆盖。")
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?)",
                       (row["session_id"], message["msg_id"], row["id"], digest, json_text(ack), received_utc))
            # Private expected/matched values deliberately NEVER appear in outgoing messages.
            self._audit(db, row["session_id"], row["id"], peer, "SYSTEM", "RESULT_RECORDED",
                        f"已持久化 {message['verdict']}；状态 {status}；{elapsed_ms:.3f} ms", None)

    def sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id,created_utc,team,competition,state,mode FROM sessions ORDER BY created_utc DESC LIMIT 500")]

    def rows(self, sid: str, limit: int | None = 1000) -> list[dict[str, Any]]:
        with self.lock:
            sql = "SELECT * FROM rounds WHERE session_id=? ORDER BY number DESC"
            args: tuple[Any, ...] = (sid,)
            if limit is not None:
                sql += " LIMIT ?"
                args += (limit,)
            return [dict(r) for r in self.db.execute(sql, args)]

    def stats(self, sid: str) -> dict[str, int]:
        with self.lock:
            return self._stats(self.db, sid)

    @staticmethod
    def _stats(db: sqlite3.Connection, sid: str) -> dict[str, int]:
        row = db.execute("""SELECT COUNT(*) total, COUNT(actual) received,
              SUM(CASE WHEN matched=1 THEN 1 ELSE 0 END) matched,
              SUM(CASE WHEN status='TIMEOUT' THEN 1 ELSE 0 END) timeout,
              SUM(CASE WHEN status='LATE' THEN 1 ELSE 0 END) late,
              SUM(CASE WHEN status IN ('CANCELLED','INTERRUPTED') THEN 1 ELSE 0 END) interrupted,
              SUM(retries) retries, SUM(duplicates) duplicates,
              SUM(protocol_errors) protocol_errors, SUM(disconnects) disconnects
              FROM rounds WHERE session_id=?""", (sid,)).fetchone()
        return {k: int(v or 0) for k, v in dict(row).items()}

    def load_settings(self, legacy_path: Path | None = None) -> dict[str, Any]:
        """Import the old settings.json once; thereafter SQLite is authoritative."""
        with self.lock:
            loaded = {r["key"]: json.loads(r["value_json"]) for r in self.db.execute("SELECT key,value_json FROM settings")}
        if "_legacy_imported" not in loaded:
            legacy = {}
            if legacy_path and legacy_path.is_file():
                try:
                    value = json.loads(legacy_path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        legacy = {k: v for k, v in value.items() if k in UI_SETTING_KEYS}
                except (ValueError, OSError):
                    # Invalid legacy settings do not invalidate competition evidence.
                    legacy = {}
            with self.transaction() as db:
                for key, value in legacy.items():
                    if isinstance(value, (str, int, bool)):
                        db.execute("INSERT OR IGNORE INTO settings VALUES(?,?,?)", (key, json_text(value), utc_now()))
                db.execute("INSERT OR IGNORE INTO settings VALUES('_legacy_imported','true',?)", (utc_now(),))
            return self.load_settings()
        return {k: v for k, v in loaded.items() if k in UI_SETTING_KEYS}

    def save_settings(self, values: dict[str, Any]) -> None:
        if not set(values).issubset(UI_SETTING_KEYS):
            raise ValueError("不允许保存未知的界面设置。")
        if any(not isinstance(v, (str, int, bool)) or len(str(v)) > 2000 for v in values.values()):
            raise ValueError("界面设置值无效。")
        with self.transaction() as db:
            for key, value in values.items():
                db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_utc=excluded.updated_utc",
                           (key, json_text(value), utc_now()))
            self._audit(db, None, None, "", "SYSTEM", "SETTINGS_SAVED", "已保存界面设置：" + ",".join(sorted(values)), None)

    def search_sessions(self, **filters: Any) -> dict[str, Any]:
        from .dbtools import search_sessions
        return search_sessions(self.path, **filters)

    def health(self) -> dict[str, Any]:
        return inspect_database(self.path)

    def backup(self, parent: Path | None = None, reason: str = "manual") -> Path:
        return backup_database(self.path, parent or self.path.parent / "backups", reason=reason)

    def export(self, sid: str, parent: Path) -> Path:
        """Export a consistent per-session snapshot; original evidence is in audit.jsonl."""
        # A separate read transaction pins all SELECTs to one WAL snapshot.
        # File writing below holds neither the engine lock nor a DB read transaction.
        with reader(self.path) as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None:
                raise ValueError("未找到场次")
            snapshot_utc = utc_now()
            session = dict(session)
            rows = [dict(r) for r in db.execute("SELECT * FROM rounds WHERE session_id=? ORDER BY number", (sid,))]
            audit = [dict(r) for r in db.execute("SELECT * FROM audit WHERE session_id=? ORDER BY id", (sid,))]
            targets = [dict(r) for r in db.execute("SELECT * FROM target_revisions WHERE session_id=? ORDER BY revision", (sid,))]
            stats = self._stats(db, sid)
            audit_max = db.execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
        parent.mkdir(parents=True, exist_ok=True)
        folder = parent / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + sid[:8])
        folder.mkdir()  # Never overwrite an earlier export.
        columns = [
            ("number", "轮次"), ("id", "测试ID"), ("created_utc", "开始时间UTC"),
            ("received_utc", "结果时间UTC"), ("case_name", "样件类别"), ("expected", "裁判预期"),
            ("actual", "客户端结果"), ("matched", "与预期一致_1是0否"), ("status", "状态"),
            ("elapsed_ms", "服务端观测耗时ms"), ("timeout_ms", "时限ms_0不判超时"),
            ("action", "控制方式"), ("target_revision", "目标版本"), ("target_json", "本轮目标"),
            ("retries", "同ID重传次数"), ("duplicates", "不同ID重复次数"),
            ("protocol_errors", "协议错误次数"), ("disconnects", "等待中断线次数"), ("notes", "裁判备注")]
        with (folder / "rounds.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([name for _, name in columns])
            for row in rows:
                writer.writerow([csv_safe(row.get(key)) for key, _ in columns])
        # Secret access code is omitted from summary, but raw incoming hello bytes
        # remain in the evidence log. Export files must stay with authorized judges.
        session.pop("access_code", None)
        summary = {"protocol_version": 1, "database_schema": SCHEMA_VERSION,
                   "snapshot_observed_utc": snapshot_utc, "snapshot_audit_max_id": audit_max,
                   "exported_utc": utc_now(), "session": session, "target_revisions": targets,
                   "statistics": stats, "not_an_automatic_score": True,
                   "timing_basis": "server round dispatch to complete result processing; monotonic clock",
                   "warning": "audit.jsonl contains raw messages, potentially including the session access code; keep private"}
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        with (folder / "audit.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for event in audit:
                f.write(json_text(event) + "\n")
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}
        (folder / "SHA256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
        return folder

    def close(self) -> None:
        with self.lock:
            self.db.close()
            self.data_lock.close()
