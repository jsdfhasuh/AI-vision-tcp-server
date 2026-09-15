"""Append-only station observations in a separate database; old evidence is untouched."""
from __future__ import annotations

from contextlib import contextmanager, closing
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .storage import DataLock, utc_now, json_text
from .station_protocol import ProtocolError, fingerprint, plain, route

APPLICATION_ID = 0x56535432  # VST2; deliberately distinct from the legacy database.
SCHEMA_VERSION = 1
PUBLIC_COLUMNS = ('id', 'received_utc', 'project', 'station', 'worker_id', 'screw_count',
                  'barcode', 'barcode_status', 'logo', 'flame')
DDL = (
    """CREATE TABLE standards(revision INTEGER PRIMARY KEY, barcode TEXT NOT NULL,
           updated_utc TEXT NOT NULL, actor TEXT NOT NULL)""",
    """CREATE TABLE standard_updates(request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
           revision INTEGER NOT NULL REFERENCES standards(revision))""",
    """CREATE TABLE results(id INTEGER PRIMARY KEY AUTOINCREMENT,
           received_utc TEXT NOT NULL, project TEXT NOT NULL CHECK(project IN ('screw','packaging')),
           station INTEGER NOT NULL CHECK(station IN (1,2)), worker_id TEXT NOT NULL,
           msg_id TEXT NOT NULL, fingerprint TEXT NOT NULL, screw_count INTEGER,
           barcode TEXT, barcode_status TEXT, logo TEXT CHECK(logo IN ('OK','NG')),
           flame TEXT CHECK(flame IN ('OK','NG')), standard_revision INTEGER,
           standard_barcode TEXT, raw_json TEXT NOT NULL,
           UNIQUE(project,station,msg_id),
           CHECK((project='screw' AND screw_count IS NOT NULL AND screw_count>=0
             AND barcode IS NULL AND logo IS NULL AND flame IS NULL)
           OR (project='packaging' AND screw_count IS NULL AND barcode IS NOT NULL
             AND logo IS NOT NULL AND flame IS NOT NULL AND barcode_status IS NOT NULL)))""",
    'CREATE INDEX results_project ON results(project,id DESC)',
    'CREATE INDEX results_station ON results(project,station,id DESC)',
    """CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, at_utc TEXT NOT NULL,
           project TEXT, station INTEGER, peer TEXT NOT NULL, direction TEXT NOT NULL,
           kind TEXT NOT NULL, raw BLOB NOT NULL)""",
    'CREATE INDEX events_project ON events(project,id DESC)',
    """CREATE TRIGGER results_no_update BEFORE UPDATE ON results
           BEGIN SELECT RAISE(ABORT,'accepted observation is immutable'); END""",
    """CREATE TRIGGER results_no_delete BEFORE DELETE ON results
           BEGIN SELECT RAISE(ABORT,'accepted observation is immutable'); END""",
)


class StationStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner = DataLock(self.path.with_suffix('.lock'))
        self.lock = threading.RLock()
        self.closed = False
        self.db = None
        try:
            self.db = sqlite3.connect(self.path, timeout=3, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            app_id = self.db.execute('PRAGMA application_id').fetchone()[0]
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if not tables and version == 0 and app_id == 0:
                self.db.execute('BEGIN IMMEDIATE')
                for statement in DDL:
                    self.db.execute(statement)
                self.db.execute('INSERT INTO standards VALUES(0,?,?,?)', ('', utc_now(), 'system'))
                self.db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                self.db.commit()
            elif (app_id, version) != (APPLICATION_ID, SCHEMA_VERSION):
                raise ValueError('未知或旧版数据库；新接收器仅使用 station-results.sqlite3，不覆盖旧库。')
            required = {'standards', 'standard_updates', 'results', 'events'}
            actual = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= actual:
                raise ValueError('工位数据库结构不完整，请保留原文件检查。')
            self.db.execute('PRAGMA foreign_keys=ON')
            if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise ValueError('无法启用工位数据库 WAL。')
            self.db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            if self.db is not None:
                self.db.close()
            self.owner.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    @contextmanager
    def reader(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()

    def standard(self) -> dict:
        with self.lock:
            return dict(self.db.execute('SELECT * FROM standards ORDER BY revision DESC LIMIT 1').fetchone())

    def set_standard(self, barcode: str, revision: int, request_id: str, actor: str) -> dict:
        plain(barcode, 'barcode', 512, empty=True)
        plain(request_id, 'request_id', 128)
        if len(request_id) < 16 or type(revision) is not int or revision < 0:
            raise ProtocolError('INVALID_FIELD', 'invalid request ID or revision')
        digest = fingerprint({'barcode': barcode, 'revision': revision, 'actor': actor})
        with self.transaction() as db:
            old = db.execute('SELECT * FROM standard_updates WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise ProtocolError('REQUEST_CONFLICT', 'request ID already used with other content')
                return {'ok': True, 'revision': old['revision'], 'duplicate': True}
            latest = db.execute('SELECT revision FROM standards ORDER BY revision DESC LIMIT 1').fetchone()[0]
            if revision != latest:
                raise ProtocolError('STATE_CHANGED', '标准条码已被修改，请重新载入后确认。')
            db.execute('INSERT INTO standards VALUES(?,?,?,?)', (latest + 1, barcode, utc_now(), actor))
            db.execute('INSERT INTO standard_updates VALUES(?,?,?)', (request_id, digest, latest + 1))
            return {'ok': True, 'revision': latest + 1, 'duplicate': False}

    def record(self, message: dict, received: str) -> tuple[dict, bool]:
        digest = fingerprint(message)
        key = (message['project'], message['station'], message['msg_id'])
        with self.transaction() as db:
            old = db.execute('SELECT * FROM results WHERE project=? AND station=? AND msg_id=?', key).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise ProtocolError('MSG_ID_CONFLICT', 'msg_id already recorded with different content')
                return dict(old), True
            standard, version, status = None, None, None
            if message['project'] == 'packaging':
                current = db.execute('SELECT * FROM standards ORDER BY revision DESC LIMIT 1').fetchone()
                standard, version = current['barcode'], current['revision']
                if message['barcode'] == '':
                    status = 'UNREAD'
                elif standard == '':
                    status = 'UNCONFIGURED'
                else:
                    status = 'MATCH' if message['barcode'] == standard else 'MISMATCH'
            values = (received, *key[:2], message['worker_id'], key[2], digest,
                      message.get('screw_count'), message.get('barcode'), status,
                      message.get('logo'), message.get('flame'), version, standard, json_text(message))
            cursor = db.execute('''INSERT INTO results(received_utc,project,station,worker_id,msg_id,fingerprint,
                screw_count,barcode,barcode_status,logo,flame,standard_revision,standard_barcode,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
            result = dict(db.execute('SELECT * FROM results WHERE id=?', (cursor.lastrowid,)).fetchone())
            return result, False

    def event(self, key, peer: str, direction: str, kind: str, raw: bytes, at: str | None = None):
        project, station = key if key else (None, None)
        with self.transaction() as db:
            db.execute('INSERT INTO events(at_utc,project,station,peer,direction,kind,raw) VALUES(?,?,?,?,?,?,?)',
                       (at or utc_now(), project, station, peer, direction, kind, raw))

    def latest(self, project: str, station: int) -> dict | None:
        route(project, station)
        with self.lock:
            row = self.db.execute('SELECT * FROM results WHERE project=? AND station=? ORDER BY id DESC LIMIT 1',
                                  (project, station)).fetchone()
            return dict(row) if row else None

    def records(self, project: str, *, station: int = 0, before: int = 0, limit: int = 100) -> list[dict]:
        where, args = self.filters(project, station, before)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('limit 必须为1～200。')
        with self.reader() as db:
            return [dict(r) for r in db.execute('SELECT * FROM results WHERE ' + where + ' ORDER BY id DESC LIMIT ?', (*args, limit))]

    @staticmethod
    def filters(project, station=0, before=0):
        route(project, 1)
        if type(station) is not int or station not in (0, 1, 2) or type(before) is not int or before < 0:
            raise ValueError('工位或分页参数无效。')
        where, args = 'project=?', [project]
        if station:
            where += ' AND station=?'; args.append(station)
        if before:
            where += ' AND id<?'; args.append(before)
        return where, args

    def logs(self, project: str, limit: int = 50) -> list[dict]:
        route(project, 1)
        with self.reader() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM events WHERE project=? ORDER BY id DESC LIMIT ?', (project, limit))]
        for row in rows:
            row['raw'] = bytes(row['raw']).decode('utf-8', 'backslashreplace').rstrip('\r\n')
        return rows

    @staticmethod
    def public(row: dict | None) -> dict | None:
        return {k: row[k] for k in PUBLIC_COLUMNS} if row else None

    def backup(self) -> Path:
        """Independent read connection; never holds the result writer lock."""
        folder = self.path.parent / 'station-backups'
        folder.mkdir(exist_ok=True)
        target = folder / (uuid.uuid4().hex + '.sqlite3')
        partial = target.with_suffix('.partial')
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise TimeoutError('数据库备份超时。')
        try:
            # Do not start a source transaction before invoking backup.
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3)) as src:
                with closing(sqlite3.connect(partial)) as dest:
                    src.backup(dest, pages=128, progress=progress, sleep=.01)
                    if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('备份完整性检查失败。')
            with partial.open('r+b') as f:
                f.flush(); os.fsync(f.fileno())
            partial.replace(target)
            return target
        finally:
            partial.unlink(missing_ok=True)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.db.close()
                self.owner.close()
