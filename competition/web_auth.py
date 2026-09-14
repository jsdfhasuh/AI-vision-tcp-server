"""Single referee account: salted password hash and bounded, revocable sessions.

No plaintext passwords, login cookies or CSRF tokens are logged or stored in the
competition database. A process restart revokes all browser logins.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time

ITERATIONS = 600_000
COOKIE = 'vision_referee'


class LoginLimited(ValueError):
    pass


def credential_record(username: str, password: str) -> dict:
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
        raise ValueError('用户名请使用1～64位英文字母、数字、点、下划线或横线。')
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('密码长度须为12～256个字符，不提供默认密码。')
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return {'format': 1, 'username': username, 'algorithm': 'pbkdf2-sha256',
            'iterations': ITERATIONS, 'salt': salt, 'password_hash': value}


@dataclass(frozen=True)
class Login:
    username: str
    csrf: str
    expires: float


class Auth:
    def __init__(self, credential_path: Path, *, lifetime: int = 8 * 3600):
        if not credential_path.is_file() or credential_path.stat().st_size > 4096:
            raise ValueError('管理员凭据文件缺失或无效，请先运行 tools/init_web_admin.py。')
        d = json.loads(credential_path.read_text(encoding='utf-8'))
        if (not isinstance(d, dict) or d.get('format') != 1 or d.get('algorithm') != 'pbkdf2-sha256'
                or type(d.get('iterations')) is not int or not 600_000 <= d['iterations'] <= 2_000_000
                or not isinstance(d.get('username'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', d['username'])
                or not isinstance(d.get('salt'), str) or not re.fullmatch(r'[0-9a-f]{32}', d['salt'])
                or not isinstance(d.get('password_hash'), str) or not re.fullmatch(r'[0-9a-f]{64}', d['password_hash'])):
            raise ValueError('管理员凭据格式无效，拒绝启动。')
        self.record, self.lifetime = d, lifetime
        self.lock = threading.Lock()
        self.sessions: OrderedDict[str, Login] = OrderedDict()
        self.attempts: OrderedDict[str, deque] = OrderedDict()
        self.global_attempts: deque = deque()

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _limit(self, ip: str) -> None:
        now = time.monotonic()
        with self.lock:
            # Reject the global limit before allocating a new per-IP entry.
            # Otherwise blocked requests from new addresses could grow the map.
            while self.global_attempts and self.global_attempts[0] <= now - 60:
                self.global_attempts.popleft()
            if len(self.global_attempts) >= 20:
                raise LoginLimited('登录尝试过多，请60秒后再试。')
            local = self.attempts.setdefault(ip, deque())
            while local and local[0] <= now - 60:
                local.popleft()
            if len(local) >= 5:
                raise LoginLimited('登录尝试过多，请60秒后再试。')
            self.global_attempts.append(now)
            local.append(now)
            self.attempts.move_to_end(ip)
            while len(self.attempts) > 1024:
                self.attempts.popitem(last=False)

    def login(self, username: str, password: str, ip: str) -> tuple[str, Login] | None:
        self._limit(ip)
        d = self.record
        value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(d['salt']), d['iterations']).hex()
        valid_password = hmac.compare_digest(value, d['password_hash'])
        valid_user = hmac.compare_digest(username.encode(), d['username'].encode())
        if not (valid_password and valid_user):
            return None
        token = secrets.token_urlsafe(32)
        login = Login(username, secrets.token_urlsafe(32), time.monotonic() + self.lifetime)
        with self.lock:
            self._purge()
            self.sessions[self.key(token)] = login
            while len(self.sessions) > 16:
                self.sessions.popitem(last=False)
        return token, login

    def _purge(self) -> None:
        now = time.monotonic()
        for key in list(self.sessions):
            if self.sessions[key].expires <= now:
                del self.sessions[key]

    def get(self, token: str) -> Login | None:
        if not isinstance(token, str) or len(token) > 128:
            return None
        with self.lock:
            self._purge()
            return self.sessions.get(self.key(token))

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(self.key(token), None)
