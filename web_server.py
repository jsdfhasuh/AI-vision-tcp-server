"""Pure Web entry point. ONE process, no reloader, no Tk/virtual desktop."""
from __future__ import annotations
import argparse
from dataclasses import replace
import logging
from pathlib import Path
import os

from competition.web_config import WebConfig


def main() -> None:
    parser = argparse.ArgumentParser(description='视觉比赛纯Web服务端（单实例）')
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--host')
    parser.add_argument('--port',type=int)
    parser.add_argument('--tcp-host')
    parser.add_argument('--tcp-port',type=int)
    parser.add_argument('--public-url')
    parser.add_argument('--credentials',type=Path)
    args = parser.parse_args()
    # Disallow accidental process replication against an in-memory match engine.
    for name in ('WEB_CONCURRENCY','UVICORN_WORKERS'):
        if os.getenv(name,'1') != '1':
            parser.error('本程序只支持一个比赛引擎进程，不能设置多个worker。')
    try:
        config = replace(WebConfig.from_env(), **{k:v for k,v in vars(args).items() if v is not None})
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        import uvicorn
        from competition.webapp import create_app
    except ImportError as exc:
        parser.error('请先安装 requirements-web.txt 中的Web依赖：' + str(exc))
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1,
                loop='asyncio',http='h11',ws='none',proxy_headers=False,access_log=False,
                server_header=False,limit_concurrency=48,timeout_keep_alive=5,
                timeout_graceful_shutdown=30, h11_max_incomplete_event_size=16384)

if __name__ == '__main__':
    main()
