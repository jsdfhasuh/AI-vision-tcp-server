"""Headless owner of ONE Engine/Store/TCP service; no Tk imports.

HTTP controls serialize separately from TCP results. Slow checks, exports and
backups run in a bounded background queue without holding the engine lock.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable
import uuid
import zipfile

from .core import Engine
from .dbtools import reader, wal_runtime_warning
from .display import SnapshotCache, DEFAULT_TITLE, DEFAULT_SUBTITLE, validate_branding
from .network import Service
from .storage import Store, utc_now
from .web_config import WebConfig

LOG = logging.getLogger(__name__)


class ControlError(ValueError):
    def __init__(self, message: str, code: str = 'INVALID_OPERATION', status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def text(d: dict, key: str, limit: int, default: str | None = None, *, empty: bool = False) -> str:
    v = d.get(key, default)
    if not isinstance(v, str) or len(v) > limit or (not empty and not v.strip()) or any(ord(c) < 32 for c in v):
        raise ControlError(f'{key} 须为有效文本，最长{limit}字符。', status=400)
    try:
        v.encode('utf-8')
    except UnicodeError as exc:
        raise ControlError(f'{key} 包含无效Unicode字符。', status=400) from exc
    return v.strip()


def ident(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ControlError('场次或任务编号无效。', status=400)
    return value


class Jobs:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='web-maintenance')
        self.items: OrderedDict[str, dict] = OrderedDict()
        self.pending = 0
        self.closed = False

    def submit(self, kind: str, actor: str, sid: str | None = None, *, automatic: bool = False) -> dict:
        with self.lock:
            if self.closed or self.pending >= 8:
                raise ControlError('后台维护队列繁忙，请稍后再试。', 'JOBS_BUSY', 429)
            if kind not in {'backup', 'export', 'check'}:
                raise ControlError('未知维护任务。', status=400)
            jid = uuid.uuid4().hex
            row = {'id': jid, 'kind': kind, 'status': 'QUEUED', 'created_utc': utc_now(),
                   'actor': actor, 'session_id': sid, 'automatic': automatic,
                   'finished_utc': None, 'result': None, 'error': None, '_file': None}
            self.items[jid] = row
            self.pending += 1
            while len(self.items) > 100:
                self.items.popitem(last=False)  # only oldest completed jobs at this bound
            try:
                self.executor.submit(self._run, jid)
            except BaseException:
                self.pending -= 1
                self.items.pop(jid, None)
                raise
            return self._public(row)

    @staticmethod
    def _public(row: dict) -> dict:
        return {**{k: v for k, v in row.items() if not k.startswith('_')}, 'download_ready': bool(row['_file'])}

    def _run(self, jid: str) -> None:
        with self.lock:
            row = self.items[jid]
            row['status'] = 'RUNNING'
        try:
            self.store.audit(None, None, row['actor'], 'SYSTEM', 'WEB_JOB_STARTED', f"{row['kind']} {jid}")
            if row['kind'] == 'check':
                result = self.store.health()
                output = None
            else:
                if shutil.disk_usage(self.store.path.parent).free < 64 * 1024 * 1024:
                    raise OSError('剩余空间低于64MiB，拒绝生成维护副本。')
                if row['kind'] == 'backup':
                    folder = self.store.backup(reason='web_auto' if row['automatic'] else 'web_manual')
                else:
                    folder = self.store.export(row['session_id'], self.store.path.parent / 'exports')
                # ZIP holds only generated evidence, never the live DB or credentials.
                output = None
                if not row['automatic']:
                    downloads = self.store.path.parent / 'downloads'
                    downloads.mkdir(exist_ok=True)
                    partial = downloads / (jid + '.partial')
                    output = downloads / (jid + '.zip')
                    try:
                        with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED) as z:
                            for p in sorted(folder.iterdir()):
                                if p.is_file() and not p.is_symlink():
                                    z.write(p, p.name)
                        partial.replace(output)
                    finally:
                        partial.unlink(missing_ok=True)
                result = {'folder': folder.name, 'contains_private_data': True}
            self.store.audit(None, None, row['actor'], 'SYSTEM', 'WEB_JOB_DONE', f"{row['kind']} {jid}")
            with self.lock:
                row.update(status='DONE', result=result, _file=output)
        except Exception:
            LOG.exception('Maintenance job failed: %s', jid)
            with self.lock:
                row.update(status='FAILED', error='维护任务失败，请检查服务日志、磁盘空间与目录权限；原始记录没有被此任务覆盖。')
        finally:
            with self.lock:
                row['finished_utc'] = utc_now()
                self.pending -= 1

    def get(self, jid: str) -> dict:
        with self.lock:
            if jid not in self.items:
                raise ControlError('任务不存在或服务已重启；已生成的备份仍在数据目录。', 'NOT_FOUND', 404)
            return self._public(self.items[jid])

    def list(self) -> list[dict]:
        with self.lock:
            return [self._public(r) for r in reversed(self.items.values())]

    def download(self, jid: str) -> Path:
        with self.lock:
            row = self.items.get(jid)
            if not row or row['status'] != 'DONE' or not row['_file']:
                raise ControlError('下载文件尚未就绪或任务不存在。', 'NOT_FOUND', 404)
            path = row['_file'].resolve()
        if not path.is_relative_to((self.store.path.parent / 'downloads').resolve()) or not path.is_file():
            raise ControlError('下载文件不存在。', 'NOT_FOUND', 404)
        return path

    def close(self) -> None:
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=False)


class Runtime:
    def __init__(self, config: WebConfig):
        self.config = config
        self.control = threading.RLock()
        self.epoch = uuid.uuid4().hex
        self.receipts: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self.closing = False
        self.store = Store(config.data_dir / 'competition.sqlite3')
        self.jobs = Jobs(self.store)
        self.engine = Engine(self.store)
        self.tcp = Service(self.engine)
        settings = self.store.load_settings(config.data_dir / 'settings.json')
        self.cache = SnapshotCache(self.engine, settings.get('display_title', DEFAULT_TITLE),
                                   settings.get('display_subtitle', DEFAULT_SUBTITLE))
        try:
            self.tcp.start(config.tcp_host, config.tcp_port)
            self.cache.start()
        except BaseException:
            self.tcp.stop()
            self.jobs.close()
            self.store.close()
            raise

    def token(self) -> str:
        e = self.engine
        value = [self.epoch, e.session_id, e.number, e.active_id,
                 e.session['target_revision'] if e.session else None, e.listening]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def state(self) -> dict:
        e = self.engine
        if not e.lock.acquire(timeout=.08):
            raise ControlError('比赛状态正在更新，请稍后刷新。', 'BUSY', 503)
        try:
            session = {k: v for k, v in e.session.items() if k != 'access_code'} if e.session else None
            p = e.peer
            return {'state_token': self.token(), 'generated_utc': utc_now(), 'session': session,
                    'active_id': e.active_id, 'fatal': e.fatal, 'listening': e.listening,
                    'connected': bool(p and p.alive),
                    'ready': bool(session and p and p.alive and p.target_revision == session['target_revision']),
                    'client': p.client_id if p and p.alive else None,
                    'rows': self.store.rows(e.session_id, limit=100) if session else [],
                    'stats': self.store.stats(e.session_id) if session else {},
                    'runtime_warning': wal_runtime_warning(),
                    'branding': {'title': self.cache.title, 'subtitle': self.cache.subtitle}}
        finally:
            e.lock.release()

    def code(self) -> dict:
        with self.engine.lock:
            if not self.engine.session:
                raise ControlError('尚未创建场次。')
            return {'session_id': self.engine.session_id, 'access_code': self.engine.session['access_code']}

    def _auto_backup(self) -> None:
        if self.config.auto_backup:
            try:
                self.jobs.submit('backup', 'system', automatic=True)
            except ControlError:
                LOG.error('Automatic backup queue full; backup not completed. Use manual backup.')

    def command(self, name: str, payload: dict, request_id: str, actor: str) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_id):
            raise ControlError('缺少有效 X-Request-ID。', status=400)
        fingerprint = hashlib.sha256((actor + name + json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)).encode()).hexdigest()
        with self.control:
            if self.closing:
                raise ControlError('服务正在停止。', 'SHUTTING_DOWN', 503)
            if request_id in self.receipts:
                old, result = self.receipts[request_id]
                if old != fingerprint:
                    raise ControlError('同一请求编号不能更改操作或内容。', 'REQUEST_CONFLICT')
                return {**result, 'replayed': True}
            try:
                result = self._execute(name, payload, actor)
                self.store.audit(self.engine.session_id, None, actor, 'SYSTEM', 'WEB_CONTROL', name)
            except sqlite3.Error as exc:
                self.engine.fail(exc)
                raise ControlError('比赛证据写入失败，服务已冻结，请保留数据并检查日志。', 'STORAGE_FAILED', 503) from exc
            self.receipts[request_id] = (fingerprint, result)
            while len(self.receipts) > 512:
                self.receipts.popitem(last=False)
            return {**result, 'replayed': False}

    def _execute(self, name: str, d: dict, actor: str) -> dict:
        allowed = {
            'new_session': {'state_token','team','client_id','competition','mode'},
            'target': {'state_token','box_type','product_model','label_type'},
            'start_round': {'state_token','case_name','expected','timeout_ms','action','notes'},
            'cancel_round': {'state_token','reason'}, 'end_session': {'state_token'},
            'start_listener': {'state_token'}, 'stop_listener': {'state_token'},
            'branding': {'state_token','title','subtitle'},
            'backup': set(), 'check': set(), 'export': {'session_id'},
        }
        if name not in allowed or not isinstance(d, dict) or not set(d).issubset(allowed[name]):
            raise ControlError('未知操作或字段。', status=400)
        if name in {'backup', 'check', 'export'}:
            sid = ident(d.get('session_id')) if name == 'export' else None
            if sid:
                with reader(self.store.path) as db:
                    if not db.execute('SELECT id FROM sessions WHERE id=?', (sid,)).fetchone():
                        raise ControlError('场次不存在。', 'NOT_FOUND', 404)
            return {'job': self.jobs.submit(name, actor, sid)}
        e = self.engine
        with e.lock:
            if d.get('state_token') != self.token():
                raise ControlError('场次或轮次状态已变化，请刷新后重新确认。未执行旧操作。', 'STATE_CHANGED')
            e._healthy()
            if name == 'new_session':
                old = bool(e.session)
                s = e.new_session(text(d,'team',100), text(d,'client_id',128),
                                  text(d,'competition',32), mode=text(d,'mode',16))
                if old:
                    self._auto_backup()
                return {'session_id': s['id']}
            if name == 'target':
                e.set_target(text(d,'box_type',128), text(d,'product_model',128,'',empty=True),
                             text(d,'label_type',128,'',empty=True))
            elif name == 'start_round':
                rid = e.start_round(text(d,'case_name',200), text(d,'expected',2),
                                    d.get('timeout_ms',0), text(d,'action',7,'arm'),
                                    text(d,'notes',2000,'',empty=True))
                return {'round_id': rid}
            elif name == 'cancel_round':
                e.cancel_round(text(d,'reason',500))
            elif name == 'end_session':
                if not e.session or e.active_id:
                    raise ControlError('请先结束或取消当前轮次，再结束场次。')
                e.close_session()
                if e.peer:
                    e.peer.close()
                e.peer, e.session, e.active_id = None, None, None
                e.started_ns.clear()
                e.number = 0
                self._auto_backup()
            elif name == 'branding':
                title, subtitle = validate_branding(text(d,'title',28), text(d,'subtitle',56))
                self.store.save_settings({'display_title': title, 'display_subtitle': subtitle})
                self.cache.set_branding(title, subtitle)
            elif name in {'start_listener', 'stop_listener'}:
                if e.active_id:
                    raise ControlError('等待结果期间不能切换监听，请先取消当前轮。')
        # Joining socket threads while holding Engine.lock would deadlock.
        if name == 'stop_listener':
            self.tcp.stop()
        elif name == 'start_listener':
            self.tcp.start(self.config.tcp_host, self.config.tcp_port)
        return {'ok': True}

    def history(self, sid: str, *, offset: int = 0, limit: int = 100) -> dict:
        ident(sid)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ControlError('分页参数无效。', status=400)
        with reader(self.store.path) as db:
            s = db.execute('SELECT id,created_utc,ended_utc,state,team,client_id,competition,mode,target_revision,target_json FROM sessions WHERE id=?', (sid,)).fetchone()
            if not s:
                raise ControlError('场次不存在。', 'NOT_FOUND', 404)
            rows = [dict(r) for r in db.execute('SELECT * FROM rounds WHERE session_id=? ORDER BY number DESC LIMIT ? OFFSET ?', (sid,limit,offset))]
            return {'session': dict(s), 'rows': rows, 'stats': Store._stats(db,sid), 'offset': offset, 'limit': limit}

    def close(self) -> None:
        with self.control:
            if self.closing:
                return
            self.closing = True
            # Even failed evidence writes must not skip the remaining cleanup.
            # A forced death is recovered as INTERRUPTED on the next startup.
            steps = [('TCP stop', self.tcp.stop),
                     ('session close', self.engine.close_session),
                     ('snapshot stop', self.cache.stop),
                     ('maintenance drain', self.jobs.close)]
            if self.config.auto_backup:
                steps.append(('shutdown backup', lambda: self.store.backup(reason='web_shutdown')))
            steps.append(('database close', self.store.close))
            for label, operation in steps:
                try:
                    operation()
                except Exception:
                    LOG.exception('Shutdown step failed (%s); preserve original data directory.', label)
