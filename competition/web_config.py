"""Validated Web/Docker settings. The desktop entry point does not import this."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress
import os


@dataclass(frozen=True)
class WebConfig:
    data_dir: Path = Path('data')
    credentials: Path = Path('secrets/admin.json')
    public_url: str = 'http://localhost:9080'
    host: str = '127.0.0.1'
    port: int = 9080
    tcp_host: str = '127.0.0.1'
    tcp_port: int = 9000
    allow_insecure_http: bool = False
    auto_backup: bool = True

    def __post_init__(self) -> None:
        u = urlsplit(self.public_url)
        if (u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password
                or u.path not in {'', '/'} or u.query or u.fragment or any(c.isspace() for c in self.public_url)):
            raise ValueError('WEB_PUBLIC_URL 必须是完整的 http(s)://主机[:端口]，不能带路径、账号或参数。')
        try:
            _ = u.port
            loopback = u.hostname == 'localhost' or ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:
            loopback = u.hostname == 'localhost'
            # A malformed port must not be swallowed as a hostname parse error.
            _ = u.port
        if u.scheme == 'http' and not loopback and not self.allow_insecure_http:
            raise ValueError('非本机地址默认要求 HTTPS。仅可信隔离网络才可设置 WEB_ALLOW_INSECURE_HTTP=1。')
        for port in (self.port, self.tcp_port):
            if type(port) is not int or not 0 <= port <= 65535:
                raise ValueError('监听端口必须为0～65535；0仅用于自动化测试。')

    @property
    def origin(self) -> str:
        return self.public_url.rstrip('/')

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.public_url).scheme == 'https'

    @classmethod
    def from_env(cls) -> 'WebConfig':
        def flag(name: str, default: str) -> bool:
            value = os.getenv(name, default)
            if value not in {'0', '1'}:
                raise ValueError(f'{name} 只能是0或1。')
            return value == '1'
        return cls(data_dir=Path(os.getenv('DATA_DIR', 'data')),
                   credentials=Path(os.getenv('WEB_ADMIN_FILE', 'secrets/admin.json')),
                   public_url=os.getenv('WEB_PUBLIC_URL', 'http://localhost:9080'),
                   host=os.getenv('WEB_HOST', '127.0.0.1'), port=int(os.getenv('WEB_PORT', '9080')),
                   tcp_host=os.getenv('TCP_HOST', '127.0.0.1'), tcp_port=int(os.getenv('TCP_PORT', '9000')),
                   allow_insecure_http=flag('WEB_ALLOW_INSECURE_HTTP', '0'),
                   auto_backup=flag('AUTO_BACKUP', '1'))
