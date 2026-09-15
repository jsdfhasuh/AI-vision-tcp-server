"""Web v1.4: authenticated two-project monitor + limited read-only board."""
from __future__ import annotations

from contextlib import asynccontextmanager
import csv
import hmac
import io
import json
import logging
from pathlib import Path
import re
import sqlite3

from fastapi import FastAPI, Request, Response, Body, Depends
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from .webapp import RequestGuard, error  # Shared HTTP protections; no legacy Runtime is created.
from .web_auth import Auth, COOKIE, Login, LoginLimited
from .web_config import WebConfig
from .station_protocol import ProtocolError, plain
from .station_runtime import StationRuntime
from .storage import csv_safe

ROOT = Path(__file__).resolve().parent
VERSION = '1.4.0-stations'
LOG = logging.getLogger(__name__)


def create_app(config: WebConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.auth = Auth(config.credentials)
        runtime = await run_in_threadpool(StationRuntime, config)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await run_in_threadpool(runtime.close)

    app = FastAPI(title='视觉比赛 TCP 服务器', version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(RequestGuard, config=config)

    @app.exception_handler(ProtocolError)
    async def protocol_error(request, exc):
        status = 409 if exc.code in ('STATE_CHANGED', 'REQUEST_CONFLICT') else 400
        return error(status, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(400, 'INVALID_REQUEST', '参数、字段或 JSON 格式无效。')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, 'HTTP_ERROR', str(exc.detail))

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return error(400, 'INVALID_VALUE', str(exc))

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        app.state.runtime.engine.fail(exc)
        return error(503, 'STORAGE_FAILED', '数据库异常，已停止接收；请保留原始数据检查日志。')

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        LOG.exception('Station web request failed')
        return error(500, 'INTERNAL_ERROR', '操作失败，请检查服务日志；没有自动补成NG。')

    def auth_required(request: Request) -> Login:
        identity = app.state.auth.get(request.cookies.get(COOKIE, ''))
        if not identity:
            raise HTTPException(401, '请先登录，或登录已过期。')
        if request.method == 'POST' and not hmac.compare_digest(
                request.headers.get('x-csrf-token', '').encode(), identity.csrf.encode()):
            raise HTTPException(403, '操作校验失败，请重新登录。')
        return identity

    @app.get('/healthz')
    def health():
        healthy = app.state.runtime.healthy
        return JSONResponse({'ok': healthy}, status_code=200 if healthy else 503)

    @app.post('/api/auth/login')
    def login(request: Request, response: Response, payload: dict = Body(...)):
        if set(payload) != {'username', 'password'}:
            raise ValueError('登录参数无效。')
        username = plain(payload.get('username'), 'username')
        password = plain(payload.get('password'), 'password', 256)
        try:
            answer = app.state.auth.login(username, password, request.client.host if request.client else 'unknown')
        except LoginLimited:
            raise HTTPException(429, '登录尝试过多，请稍后重试。')
        if not answer:
            raise HTTPException(401, '用户名或密码不正确。')
        token, identity = answer
        app.state.auth.logout(request.cookies.get(COOKIE, ''))
        response.set_cookie(COOKIE, token, max_age=app.state.auth.lifetime, path='/',
                            httponly=True, secure=config.secure_cookie, samesite='strict')
        return {'username': identity.username, 'csrf': identity.csrf, 'version': VERSION}

    @app.get('/api/auth/me')
    def me(identity: Login = Depends(auth_required)):
        return {'username': identity.username, 'csrf': identity.csrf, 'version': VERSION}

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response, identity: Login = Depends(auth_required)):
        app.state.auth.logout(request.cookies.get(COOKIE, ''))
        response.delete_cookie(COOKIE, path='/', httponly=True, secure=config.secure_cookie, samesite='strict')
        return {'ok': True}

    @app.get('/api/admin/state')
    def state(project: str = 'screw', identity: Login = Depends(auth_required)):
        runtime = app.state.runtime
        data = runtime.engine.snapshot(project)
        return {**data, 'listening': f'{runtime.tcp.address[0]}:{runtime.tcp.address[1]}', 'version': VERSION}

    @app.get('/api/display')
    def display(project: str = 'screw'):
        # Never includes standard_barcode, revision history, raw logs or IP addresses.
        return app.state.runtime.engine.snapshot(project, public=True)

    @app.post('/api/admin/standard-barcode')
    def standard(request: Request, payload: dict = Body(...), identity: Login = Depends(auth_required)):
        if set(payload) != {'barcode', 'revision'}:
            raise ValueError('仅接受 barcode 和 revision。')
        runtime = app.state.runtime
        if not runtime.healthy:
            raise HTTPException(503, '服务异常，暂不能修改配置。')
        return runtime.store.set_standard(payload['barcode'], payload['revision'],
                                          request.headers.get('x-request-id', ''), identity.username)

    @app.get('/api/admin/records')
    def records(project: str, station: int = 0, before: int = 0, limit: int = 100,
                identity: Login = Depends(auth_required)):
        store = app.state.runtime.store
        rows = [store.public(r) for r in store.records(project, station=station, before=before, limit=limit)]
        return {'records': rows, 'next_before': rows[-1]['id'] if len(rows) == limit else None}

    @app.get('/api/admin/export')
    def export(project: str, station: int = 0, format: str = 'csv', identity: Login = Depends(auth_required)):
        store = app.state.runtime.store
        where, args = store.filters(project, station)
        if format not in ('csv', 'jsonl'):
            raise ValueError('导出格式只能是 csv 或 jsonl。')
        columns = ('id', 'received_utc', 'project', 'station', 'worker_id', 'msg_id', 'screw_count') if project == 'screw' else (
            'id', 'received_utc', 'project', 'station', 'worker_id', 'msg_id', 'barcode',
            'barcode_status', 'logo', 'flame', 'standard_revision', 'standard_barcode')
        def stream():
            # Independent bounded-memory snapshot; exporting never holds the TCP engine lock.
            with store.reader() as db:
                cursor = db.execute('SELECT * FROM results WHERE ' + where + ' ORDER BY id', args)
                text = io.StringIO(newline='')
                writer = csv.writer(text)
                if format == 'csv':
                    writer.writerow(columns); yield '\ufeff' + text.getvalue()
                    text.seek(0); text.truncate(0)
                while True:
                    batch = cursor.fetchmany(200)
                    if not batch:
                        break
                    for row in batch:
                        if format == 'jsonl':
                            yield json.dumps(dict(row), ensure_ascii=False) + '\n'
                        else:
                            writer.writerow([csv_safe(row[c]) for c in columns])
                            yield text.getvalue(); text.seek(0); text.truncate(0)
        mime = 'text/csv' if format == 'csv' else 'application/x-ndjson'
        return StreamingResponse(stream(), media_type=mime,
            headers={'Content-Disposition': f'attachment; filename="{project}-records.{format}"'})

    @app.post('/api/admin/backup')
    def backup(payload: dict = Body(...), identity: Login = Depends(auth_required)):
        if payload:
            raise ValueError('备份请求不接受额外参数。')
        runtime = app.state.runtime
        if runtime.closing or not runtime.backup_lock.acquire(blocking=False):
            raise HTTPException(409, '备份正在运行或服务正在关闭。')
        try:
            path = runtime.store.backup()
            return {'file': path.name, 'download_url': '/api/admin/backups/' + path.name}
        finally:
            runtime.backup_lock.release()

    @app.get('/api/admin/backups/{name}')
    def download(name: str, identity: Login = Depends(auth_required)):
        if not re.fullmatch(r'[0-9a-f]{32}\.sqlite3', name):
            raise HTTPException(404, '备份不存在。')
        folder = (config.data_dir / 'station-backups').resolve()
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, '备份不存在。')
        return FileResponse(path, media_type='application/octet-stream', filename=name)

    files = {'/admin/': ('monitor/index.html', 'text/html'), '/board/': ('monitor/index.html', 'text/html'),
             '/monitor/app.js': ('monitor/app.js', 'application/javascript'),
             '/monitor/style.css': ('monitor/style.css', 'text/css'),
             '/favicon.svg': ('web/favicon.svg', 'image/svg+xml')}
    def handler(file, mime):
        def asset():
            return FileResponse(ROOT / file, media_type=mime)
        return asset
    for path, (file, mime) in files.items():
        app.add_api_route(path, handler(file, mime), methods=['GET', 'HEAD'])

    @app.get('/')
    def root():
        return RedirectResponse('/admin/', status_code=302)

    @app.get('/admin')
    def admin():
        return RedirectResponse('/admin/', status_code=302)

    @app.get('/board')
    def board():
        return RedirectResponse('/board/', status_code=302)

    return app
