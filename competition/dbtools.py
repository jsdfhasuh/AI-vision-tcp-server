"""Read-only inspection, consistent online backup and non-destructive restore.

Only the referee process writes the live database. Maintenance readers do not
checkpoint or write it. Keep data on a local disk, not a shared/network drive.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2
APPLICATION_ID = 0x56435443  # VCTC
BASE_COLUMNS = {
    "sessions": {"id", "created_utc", "ended_utc", "state", "team", "client_id", "competition", "access_code", "target_revision", "target_json"},
    "rounds": {"id", "session_id", "number", "status", "actual", "expected", "matched", "elapsed_ms", "target_revision", "target_json", "action", "timeout_ms", "created_utc", "received_utc", "notes", "case_name", "retries", "duplicates", "protocol_errors", "disconnects"},
    "receipts": {"session_id", "msg_id", "round_id", "fingerprint", "response_json", "created_utc"},
    "audit": {"id", "at_utc", "session_id", "round_id", "peer", "direction", "kind", "text", "raw_b64"},
}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextmanager
def reader(path: Path, *, snapshot: bool = True) -> Iterator[sqlite3.Connection]:
    """Independent, read-only connection; never creates a missing database."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在：{path}")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        if snapshot:
            db.execute("BEGIN")
        yield db
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def validate_schema(db: sqlite3.Connection) -> int:
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version not in (1, SCHEMA_VERSION):
        raise ValueError(f"不支持的数据库版本 {version}；本程序支持1～{SCHEMA_VERSION}，不会强制降级。")
    app_id = int(db.execute("PRAGMA application_id").fetchone()[0])
    if app_id not in (0, APPLICATION_ID) or (version == 2 and app_id != APPLICATION_ID):
        raise ValueError("数据库应用标识不匹配，不是受支持的比赛数据库。")
    required = dict(BASE_COLUMNS)
    if version == 2:
        required.update({"schema_migrations": {"version", "applied_utc", "description"},
                         "target_revisions": {"session_id", "revision", "target_json", "recorded_utc", "source"},
                         "settings": {"key", "value_json", "updated_utc"}})
        required["sessions"] = required["sessions"] | {"mode"}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in required.items():
        if table not in tables or not columns.issubset({r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}):
            raise ValueError(f"数据库表结构不匹配：{table}")
    return version


def wal_runtime_warning() -> str | None:
    v = sqlite3.sqlite_version_info
    patched = v >= (3, 51, 3) or (3, 50, 7) <= v < (3, 51, 0) or (3, 44, 6) <= v < (3, 45, 0)
    if patched:
        return None
    return (f"SQLite {sqlite3.sqlite_version} 未在本工具已知的 WAL-reset 修复版本范围内。"
            "建议部署前使用含官方修复的运行时；本程序保持单写连接，其他连接只读。"
            "不要用外部工具同时写入或执行checkpoint。此提示不是已发现数据损坏。")


def inspect_database(path: Path) -> dict[str, Any]:
    path = path.resolve()
    with reader(path) as db:
        version = validate_schema(db)
        check = [r[0] for r in db.execute("PRAGMA integrity_check")]
        foreign = [tuple(r) for r in db.execute("PRAGMA foreign_key_check")]
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in BASE_COLUMNS}
        modes = {r[0]: r[1] for r in db.execute("SELECT mode,COUNT(*) FROM sessions GROUP BY mode")} if version == 2 else {"LEGACY": counts["sessions"]}
        audit_max = db.execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
        journal = db.execute("PRAGMA journal_mode").fetchone()[0]
    files = {suffix or "main": Path(str(path) + suffix).stat().st_size
             if Path(str(path) + suffix).exists() else 0 for suffix in ("", "-wal", "-shm")}
    return {"path": str(path), "schema_version": version, "sqlite_runtime": sqlite3.sqlite_version,
            "journal_mode": journal, "integrity_ok": check == ["ok"] and not foreign,
            "integrity_check": check, "foreign_key_errors": foreign,
            "counts": counts, "session_modes": modes, "audit_max_id": audit_max,
            "file_bytes": files, "free_disk_bytes": shutil.disk_usage(path.parent).free,
            "runtime_warning": wal_runtime_warning(), "checked_utc": timestamp()}


def backup_database(source: Path, parent: Path, *, reason: str = "manual",
                    timeout_seconds: float = 60.0) -> Path:
    """A completed folder has a checked single DB and a SHA-256 manifest.

    No copy of a live .sqlite3 file is used. An independent source connection
    runs SQLite's online backup API. It holds no Engine or Store Python lock.
    """
    source, parent = source.resolve(), parent.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"数据库不存在：{source}")
    if timeout_seconds <= 0:
        raise ValueError("备份超时必须大于0。")
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir()
    temp = folder / "database.partial.sqlite3"
    target = folder / "database.sqlite3"
    deadline = time.monotonic() + timeout_seconds
    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("备份超过允许时间，未生成成功备份；原数据库没有被修改。")
    try:
        with reader(source, snapshot=False) as src:
            validate_schema(src)
            dest = sqlite3.connect(temp)
            try:
                src.backup(dest, pages=128, progress=progress, sleep=.02)
                dest.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest.close()
        info = inspect_database(temp)
        if not info["integrity_ok"]:
            raise ValueError("备份完整性检查失败，未生成成功备份。")
        # Windows fsync requires a writable handle; r+b preserves the backup bytes.
        with temp.open("r+b") as f:
            f.flush()
            os.fsync(f.fileno())
        temp.replace(target)
        manifest = {"format": "vision-competition-backup-v1", "created_utc": timestamp(),
                    "reason": reason, "schema_version": info["schema_version"],
                    "sha256": digest_file(target), "file": target.name,
                    "counts": info["counts"], "audit_max_id": info["audit_max_id"],
                    "warning": "Contains private referee data and access codes. SHA256 is not a digital signature."}
        with (folder / "manifest.json").open("x", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return folder
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def restore_backup(folder: Path, target_dir: Path) -> Path:
    """Restore only to a NEW/EMPTY data directory; never overwrite live evidence.

    The byte copy is from a completed standalone backup, NOT from a live WAL
    database. Verify the staged copy before publication to avoid check/use races.
    """
    folder, target_dir = folder.resolve(), target_dir.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != "vision-competition-backup-v1" or manifest.get("file") != "database.sqlite3":
        raise ValueError("不支持的备份清单格式。")
    source = folder / "database.sqlite3"
    if any(Path(str(source) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("备份目录包含运行中数据库附属文件；请使用已完成的独立备份目录。")
    if target_dir.exists() and (not target_dir.is_dir() or any(target_dir.iterdir())):
        raise ValueError("恢复目标必须是新建或完全空的数据目录；不会覆盖已有比赛记录。")
    target_dir.mkdir(parents=True, exist_ok=True)
    from .storage import DataLock  # Local import avoids initialization cycle.
    lock = DataLock(target_dir / "competition.lock")
    temp = target_dir / "restore.partial.sqlite3"
    target = target_dir / "competition.sqlite3"
    try:
        # Recheck under application lock; non-cooperating writers are unsupported.
        if any(p.name != "competition.lock" for p in target_dir.iterdir()):
            raise ValueError("恢复目标目录已被其他操作使用。")
        with source.open("rb") as src, temp.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if digest_file(temp) != manifest.get("sha256"):
            raise ValueError("备份SHA256校验失败，拒绝恢复。")
        info = inspect_database(temp)
        if not info["integrity_ok"] or info["schema_version"] != manifest.get("schema_version"):
            raise ValueError("备份结构或完整性检查失败，拒绝恢复。")
        if info["journal_mode"] != "delete":
            raise ValueError("仅接受独立、非WAL格式的已完成备份。")
        temp.replace(target)
        # Store startup will mark unfinished old rounds INTERRUPTED; no timer resumes.
        return target
    finally:
        temp.unlink(missing_ok=True)
        lock.close()


def search_sessions(path: Path, *, team: str = "", mode: str = "", competition: str = "",
                    state: str = "", date_from: str = "", date_to: str = "",
                    limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Paginated private referee query; dates are explicitly UTC calendar dates."""
    from datetime import date, timedelta
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise ValueError("分页参数无效；每页1～200条，起点不能小于0。")
    if not isinstance(team, str) or len(team) > 100:
        raise ValueError("队伍关键词不能超过100字符。")
    choices = {"mode": (mode, {"", "PRACTICE", "OFFICIAL", "LEGACY"}),
               "competition": (competition, {"", "packaging", "screw"}),
               "state": (state, {"", "OPEN", "CLOSED", "INTERRUPTED"})}
    where, params = [], []
    for key, (value, allowed) in choices.items():
        if value not in allowed:
            raise ValueError(f"无效的筛选条件：{key}")
        if value:
            where.append(f"s.{key}=?")
            params.append(value)
    if team:
        where.append("instr(s.team,?)>0")  # literal substring, not LIKE wildcards
        params.append(team)
    start = date.fromisoformat(date_from) if date_from else None
    end = date.fromisoformat(date_to) if date_to else None
    if start and end and start > end:
        raise ValueError("起始日期不能晚于结束日期。")
    if start:
        where.append("s.created_utc>=?")
        params.append(start.isoformat())
    if end:
        where.append("s.created_utc<?")
        params.append((end + timedelta(days=1)).isoformat())
    condition = " WHERE " + " AND ".join(where) if where else ""
    with reader(path) as db:
        if validate_schema(db) != 2:
            raise ValueError("请先用新版服务端完成数据库升级，再使用带用途筛选的历史查询。")
        total = db.execute("SELECT COUNT(*) FROM sessions s" + condition, params).fetchone()[0]
        rows = [dict(r) for r in db.execute("""SELECT s.id,s.created_utc,s.ended_utc,s.team,
                 s.competition,s.mode,s.state,
                 (SELECT COUNT(*) FROM rounds r WHERE r.session_id=s.id) AS rounds,
                 (SELECT COUNT(actual) FROM rounds r WHERE r.session_id=s.id) AS received
                 FROM sessions s""" + condition + " ORDER BY s.created_utc DESC,s.id DESC LIMIT ? OFFSET ?",
                 (*params, limit, offset))]
    return {"total": total, "limit": limit, "offset": offset, "sessions": rows}
