"""Authenticated referee API + public board, for the single-owner headless runtime."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from fastapi import FastAPI, Request, Body, Depends
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .headless import Runtime, ControlError, text, ident
from .web_auth import Auth, Login, LoginLimited, COOKIE
from .web_config import WebConfig

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
WEB_VERSION = '1.3.0-web'


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({'error': {'code': code, 'message': message}}, status_code=status)


class RequestGuard:
    """Bounded request buffering, origin/Host enforcement and response headers.

    Headers from untrusted proxies never establish identity. Configure one public
    origin; reverse proxies must forward the original Host. All writes use JSON.
    """
    def __init__(self, app, config: WebConfig, body_limits: dict[str, int] | None = None):
        self.app, self.config = app, config
        self.body_limits = body_limits or {}

    async def __call__(self, scope, receive, send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        async def safe_send(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers += [(b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                            (b'x-frame-options', b'DENY'), (b'referrer-policy', b'no-referrer'),
                            (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                            (b'content-security-policy', b"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                message = {**message, 'headers': headers}
            await send(message)
        async def reject(status, code, message):
            await error(status, code, message)(scope, receive, safe_send)
        headers = {k.lower(): v.decode('latin1') for k,v in scope['headers']}
        host = headers.get(b'host', '').lower()
        expected = urlsplit(self.config.origin).netloc.lower()
        internal_health = scope['path'] == '/healthz' and host in {'127.0.0.1:9080','localhost:9080','127.0.0.1','localhost'}
        if host != expected and not internal_health:
            await reject(400, 'HOST_REJECTED', '访问主机不匹配，请检查WEB_PUBLIC_URL和反向代理Host。')
            return
        if len(scope.get('raw_path', b'')) + len(scope.get('query_string', b'')) > 4096:
            await reject(414, 'URL_TOO_LONG', '请求地址过长。')
            return
        method = scope['method']
        max_body = self.body_limits.get(scope['path'], 16384) if method == 'POST' else 16384
        if method not in {'GET','HEAD','POST'}:
            await reject(405, 'METHOD_NOT_ALLOWED', '不支持此请求方法。')
            return
        if method == 'POST':
            if headers.get(b'origin') != self.config.origin:
                await reject(403, 'ORIGIN_REJECTED', '请求来源不匹配，请使用配置的Web地址。')
                return
            if headers.get(b'content-type', '').split(';')[0].strip().lower() != 'application/json':
                await reject(415, 'JSON_REQUIRED', '写操作只接受application/json。')
                return
        # Do not let FastAPI parse an unbounded request, even with chunked encoding.
        try:
            length = int(headers.get(b'content-length','0'))
        except ValueError:
            await reject(400, 'BAD_LENGTH', '无效请求长度。')
            return
        if length < 0 or length > max_body:
            await reject(413, 'BODY_TOO_LARGE', f'请求正文最多{max_body // 1024}KiB。')
            return
        body, deadline = bytearray(), asyncio.get_running_loop().time() + 5
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(.01, deadline-asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                await reject(408, 'REQUEST_TIMEOUT', '请求超时。')
                return
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > max_body:
                await reject(413, 'BODY_TOO_LARGE', f'请求正文最多{max_body // 1024}KiB。')
                return
            if not message.get('more_body', False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type':'http.request', 'body':bytes(body), 'more_body':False}
            return await receive()
        await self.app(scope, bounded_receive, safe_send)


def create_app(config: WebConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.auth = Auth(config.credentials)
        runtime = await run_in_threadpool(Runtime, config)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await run_in_threadpool(runtime.close)

    app = FastAPI(title='视觉比赛 Web 裁判服务端', version=WEB_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None, debug=False, redirect_slashes=False)
    app.add_middleware(RequestGuard, config=config)

    @app.exception_handler(ControlError)
    async def control_error(request, exc):
        return error(exc.status, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo submitted credentials or referee values in error responses.
        return error(400, 'INVALID_REQUEST', '参数类型、字段或JSON格式无效。')

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, 'HTTP_ERROR', '请求路径不存在或操作不受支持。')

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return error(400, 'INVALID_VALUE', str(exc))

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        LOG.exception('Unhandled web request error at %s', request.url.path)
        return error(500, 'INTERNAL_ERROR', '服务内部异常，请检查日志。没有把异常自动记为NG。')

    def auth_required(request: Request) -> Login:
        token = request.cookies.get(COOKIE, '')
        login = app.state.auth.get(token)
        if not login:
            raise ControlError('请先登录，或登录已过期。', 'LOGIN_REQUIRED', 401)
        if request.method == 'POST' and not hmac.compare_digest(request.headers.get('x-csrf-token','').encode(), login.csrf.encode()):
            raise ControlError('操作校验失败，请重新登录。', 'CSRF_FAILED', 403)
        return login

    @app.get('/healthz')
    def health():
        r = app.state.runtime
        healthy = not r.closing and not r.engine.fatal and r.cache.thread is not None and r.cache.thread.is_alive()
        return JSONResponse({'ok': healthy}, status_code=200 if healthy else 503)

    @app.post('/api/auth/login')
    def login(request: Request, response: Response, payload: dict = Body(...)):
        if set(payload) != {'username','password'}:
            raise ControlError('登录参数无效。', status=400)
        username = text(payload,'username',64)
        password = payload.get('password')
        if not isinstance(password,str) or not 1 <= len(password) <= 256:
            raise ControlError('用户名或密码不正确。', 'LOGIN_FAILED', 401)
        try:
            password.encode('utf-8')
        except UnicodeError:
            return error(400, 'INVALID_REQUEST', '密码包含无效字符。')
        try:
            answer = app.state.auth.login(username, password, request.client.host if request.client else 'unknown')
        except LoginLimited as exc:
            return error(429, 'LOGIN_LIMITED', str(exc))
        if answer is None:
            return error(401, 'LOGIN_FAILED', '用户名或密码不正确。')
        # Log only the authenticated username, never the submitted password.
        token, identity = answer
        old_token = request.cookies.get(COOKIE, '')
        app.state.auth.logout(old_token)
        app.state.runtime.store.audit(None,None,identity.username,'SYSTEM','WEB_LOGIN','裁判Web登录成功')
        response.set_cookie(COOKIE,token,max_age=app.state.auth.lifetime,path='/',httponly=True,
                            secure=config.secure_cookie,samesite='strict')
        return {'username':identity.username,'csrf':identity.csrf,'version':WEB_VERSION}

    @app.get('/api/auth/me')
    def me(identity: Login = Depends(auth_required)):
        return {'username':identity.username,'csrf':identity.csrf,'version':WEB_VERSION}

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response, identity: Login = Depends(auth_required)):
        app.state.auth.logout(request.cookies.get(COOKIE,''))
        response.delete_cookie(COOKIE,path='/',httponly=True,secure=config.secure_cookie,samesite='strict')
        return {'ok':True}

    @app.get('/api/display')
    def public_display():
        return app.state.runtime.cache.read()

    @app.get('/api/admin/state')
    def state(identity: Login = Depends(auth_required)):
        return app.state.runtime.state()

    @app.get('/api/admin/access-code')
    def access_code(identity: Login = Depends(auth_required)):
        return app.state.runtime.code()

    @app.post('/api/admin/commands/{name}')
    def command(name: str, request: Request, payload: dict = Body(...), identity: Login = Depends(auth_required)):
        return app.state.runtime.command(name,payload,request.headers.get('x-request-id',''),identity.username)

    @app.get('/api/admin/sessions')
    def sessions(team: str = '', mode: str = '', competition: str = '', date_from: str = '',
                 date_to: str = '', limit: int = 30, offset: int = 0, identity: Login = Depends(auth_required)):
        return app.state.runtime.store.search_sessions(team=team,mode=mode,competition=competition,
                                                      date_from=date_from,date_to=date_to,limit=limit,offset=offset)

    @app.get('/api/admin/sessions/{sid}')
    def session(sid: str, offset: int = 0, limit: int = 100, identity: Login = Depends(auth_required)):
        return app.state.runtime.history(sid,offset=offset,limit=limit)

    @app.get('/api/admin/jobs')
    def jobs(identity: Login = Depends(auth_required)):
        return {'jobs':app.state.runtime.jobs.list()}

    @app.get('/api/admin/jobs/{jid}')
    def job(jid: str, identity: Login = Depends(auth_required)):
        return app.state.runtime.jobs.get(ident(jid))

    @app.get('/api/admin/jobs/{jid}/download')
    def download(jid: str, identity: Login = Depends(auth_required)):
        path = app.state.runtime.jobs.download(ident(jid))
        return FileResponse(path, media_type='application/zip', filename='vision-private-'+jid+'.zip')

    # Whitelist all static assets. No arbitrary filesystem route or SPA fallback.
    files = {'/admin/': ('admin/index.html','text/html'), '/admin/admin.css': ('admin/admin.css','text/css'),
             '/admin/admin.js': ('admin/admin.js','application/javascript'),
             '/board/': ('web/index.html','text/html'),
             '/app.js': ('web/app.js','application/javascript'), '/style.css': ('web/style.css','text/css'),
             '/favicon.svg': ('web/favicon.svg','image/svg+xml')}
    def asset_handler(file: str, mime: str):
        def handler():
            return FileResponse(ROOT / file, media_type=mime)
        return handler
    for route, (file,mime) in files.items():
        app.add_api_route(route,asset_handler(file,mime),methods=['GET','HEAD'],include_in_schema=False)

    @app.get('/admin')
    def admin_redirect():
        return RedirectResponse('/admin/',status_code=302)

    @app.get('/board')
    def board_redirect():
        return RedirectResponse('/board/',status_code=302)

    @app.get('/')
    def index():
        return RedirectResponse('/admin/',status_code=302)

    return app
