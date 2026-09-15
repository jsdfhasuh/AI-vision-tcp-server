"""One-shot TCP v2 example. Import send_result() in a contestant program.

Every *new detection* needs a new msg_id; retrying an unacknowledged detection
must reuse the exact original message, including worker_id and payload.
"""
from __future__ import annotations

import argparse
import json
import socket
import uuid

from competition.station_protocol import encode, decode, validate, MAX_FRAME


def send_result(host: str, port: int, message: dict, timeout: float = 5) -> dict:
    validate(message)
    if message['type'] != 'result':
        raise ValueError('send_result only sends result observations')
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode(message))
        with sock.makefile('rb') as stream:
            raw = stream.readline(MAX_FRAME + 2)
        if not raw.endswith(b'\n') or len(raw) > MAX_FRAME + 1:
            raise ConnectionError('connection closed or incomplete/oversize acknowledgement')
        reply = decode(raw[:-1])
        if reply.get('type') != 'result_ack' or reply.get('reply_to') != message['msg_id'] or reply.get('recorded') is not True:
            raise ValueError('server rejected result: ' + json.dumps(reply, ensure_ascii=False))
        return reply


def main():
    parser = argparse.ArgumentParser(description='四工位TCP v2上报示例（不是视觉识别程序）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--project', choices=['screw', 'packaging'], required=True)
    parser.add_argument('--station', type=int, choices=[1, 2], required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--msg-id', help='重试时使用原来的编号；全新检测默认自动生成')
    parser.add_argument('--count', type=int)
    parser.add_argument('--barcode', help='完整字符串；未读到时明确传空字符串')
    parser.add_argument('--logo', choices=['OK', 'NG'])
    parser.add_argument('--flame', choices=['OK', 'NG'])
    args = parser.parse_args()
    message = {'v': 2, 'type': 'result', 'msg_id': args.msg_id or uuid.uuid4().hex,
               'project': args.project, 'station': args.station, 'worker_id': args.worker_id}
    if args.project == 'screw':
        if args.count is None or any(v is not None for v in (args.barcode, args.logo, args.flame)):
            parser.error('螺钉项目只接受 --count。')
        message['screw_count'] = args.count
    else:
        if args.count is not None or any(v is None for v in (args.barcode, args.logo, args.flame)):
            parser.error('包装箱项目必须提供 --barcode、--logo 和 --flame。')
        message.update(barcode=args.barcode, logo=args.logo, flame=args.flame)
    print('发送：' + json.dumps(message, ensure_ascii=False))
    try:
        print('回执：' + json.dumps(send_result(args.host, args.port, message), ensure_ascii=False))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'未确认保存：{exc}\n响应丢失时请核对服务器记录；重试必须保留上方原msg_id和内容。\n')


if __name__ == '__main__':
    main()
