"""Explicit schema upgrades. Old data is never silently classified as official."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .dbtools import APPLICATION_ID, SCHEMA_VERSION, backup_database, timestamp, validate_schema

BASE_DDL = """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created_utc TEXT NOT NULL, ended_utc TEXT,
                state TEXT NOT NULL, team TEXT NOT NULL, client_id TEXT NOT NULL,
                competition TEXT NOT NULL, access_code TEXT NOT NULL,
                target_revision INTEGER NOT NULL, target_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                number INTEGER NOT NULL, created_utc TEXT NOT NULL, received_utc TEXT,
                case_name TEXT NOT NULL, expected TEXT NOT NULL,
                timeout_ms INTEGER NOT NULL, action TEXT NOT NULL,
                target_revision INTEGER NOT NULL, target_json TEXT NOT NULL,
                notes TEXT NOT NULL, status TEXT NOT NULL,
                actual TEXT, matched INTEGER, elapsed_ms REAL,
                retries INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
                protocol_errors INTEGER NOT NULL DEFAULT 0, disconnects INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, number)
            );
            CREATE TABLE IF NOT EXISTS receipts (
                session_id TEXT NOT NULL, msg_id TEXT NOT NULL,
                round_id TEXT NOT NULL REFERENCES rounds(id), fingerprint TEXT NOT NULL,
                response_json TEXT NOT NULL, created_utc TEXT NOT NULL,
                PRIMARY KEY(session_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL,
                session_id TEXT, round_id TEXT, peer TEXT, direction TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, raw_b64 TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_session ON audit(session_id, id);
            CREATE INDEX IF NOT EXISTS rounds_session ON rounds(session_id, number);
            PRAGMA user_version=1;

"""


def migrate_to_v2(db: sqlite3.Connection) -> None:
    # No executescript inside this transaction: DDL and user_version move together.
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'LEGACY' CHECK(mode IN ('PRACTICE','OFFICIAL','LEGACY'))")
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL, description TEXT NOT NULL)")
        db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_utc TEXT NOT NULL)")
        db.execute("""CREATE TABLE target_revisions(
            session_id TEXT NOT NULL REFERENCES sessions(id), revision INTEGER NOT NULL,
            target_json TEXT NOT NULL, recorded_utc TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(session_id, revision))""")
        now = timestamp()
        conflicts = db.execute("""SELECT session_id,revision FROM (
            SELECT session_id,target_revision AS revision,target_json FROM rounds
            UNION ALL SELECT id,target_revision,target_json FROM sessions)
            GROUP BY session_id,revision HAVING COUNT(DISTINCT target_json)>1""").fetchall()
        if conflicts:
            raise ValueError("旧库同一目标版本存在冲突快照，拒绝自动合并；请保留升级前备份核验。")
        # Reconstruct only snapshots that actually exist, not missing historical revisions.
        db.execute("""INSERT INTO target_revisions SELECT session_id,target_revision,target_json,MIN(created_utc),'MIGRATED_ROUND'
                      FROM rounds GROUP BY session_id,target_revision""")
        db.execute("""INSERT OR IGNORE INTO target_revisions
                      SELECT id,target_revision,target_json,?,'MIGRATED_CURRENT' FROM sessions""", (now,))
        db.execute("CREATE INDEX sessions_filters ON sessions(mode,competition,created_utc DESC,id)")
        db.execute("CREATE INDEX sessions_time ON sessions(created_utc DESC,id)")
        db.execute("CREATE INDEX rounds_status ON rounds(session_id,status,number)")
        db.execute("CREATE INDEX audit_round ON audit(round_id,id)")
        db.execute("INSERT INTO schema_migrations VALUES(1,?,?)", (now, 'Recognized baseline schema'))
        db.execute("INSERT INTO schema_migrations VALUES(2,?,?)", (now, 'Modes, settings, target history, maintenance'))
        # Applied safety constraints also protect accidental future application writes.
        db.execute("""CREATE TRIGGER sessions_mode_immutable BEFORE UPDATE OF mode ON sessions
                      WHEN OLD.mode IS NOT NEW.mode
                      BEGIN SELECT RAISE(ABORT,'session mode is immutable'); END""")
        db.execute("""CREATE TRIGGER final_result_immutable BEFORE UPDATE OF actual,matched,received_utc,elapsed_ms,status ON rounds
                      WHEN OLD.actual IS NOT NULL AND
                       (NEW.actual IS NOT OLD.actual OR NEW.matched IS NOT OLD.matched OR
                        NEW.received_utc IS NOT OLD.received_utc OR NEW.elapsed_ms IS NOT OLD.elapsed_ms OR NEW.status IS NOT OLD.status)
                      BEGIN SELECT RAISE(ABORT,'accepted result is immutable'); END""")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError('数据库外键异常，迁移已回滚。')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def initialize(db: sqlite3.Connection, path: Path) -> Path | None:
    version = int(db.execute('PRAGMA user_version').fetchone()[0])
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    backup = None
    if version == 0 and not tables:
        db.executescript('BEGIN IMMEDIATE;\n' + BASE_DDL + '\nCOMMIT;')
        version = 1
    else:
        validate_schema(db)  # Unknown/future databases are rejected BEFORE changing them.
        if version == 1:
            backup = backup_database(path, path.parent / 'backups', reason='before_schema_v2')
    if version == 1:
        migrate_to_v2(db)
    validate_schema(db)
    return backup
