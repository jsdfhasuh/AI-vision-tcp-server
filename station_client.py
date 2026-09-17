"""One-shot client for station + group reports. No automatic text retries.

Legacy JSON v2 helpers retain worker-ID and msg_id semantics separately.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import time
import uuid

from competition.station_protocol import encode, decode, validate, MAX_FRAME
from competition.station_text import encode_result


def send_result(host: str, port: int, message: dict, timeout: float = 5) -> dict:
    """Legacy JSON v2 API; callers manage original msg_id when retrying."""
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


def send_text_result(host: str, port: int, message: dict, timeout: float = 5) -> str:
    """Send one group observation, wait for ACK,end. Timeout means outcome unknown."""
    wire = encode_result(message)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')
    with socket.create_connection((host, port), timeout=timeout) as sock:
        deadline = time.monotonic() + timeout
        sock.settimeout(timeout)
        sock.sendall(wire)
        buffer = bytearray()
        while b',end' not in buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('ACK timeout; check server records before sending again')
            sock.settimeout(left)
            chunk = sock.recv(128)
            if not chunk:
                raise ConnectionError('connection closed before ACK; result may already be stored')
            buffer.extend(chunk)
            if len(buffer) > 256:
                raise ConnectionError('invalid/oversize text acknowledgement')
        reply = bytes(buffer[:buffer.index(b',end') + 4])
        if reply != b'ACK,end':
            raise ValueError('server rejected text result: ' + reply.decode('utf-8', 'replace'))
        return reply.decode('ascii')


def main():
    parser = argparse.ArgumentParser(description='工位号＋组别号，上报数量/条码及OK/NG，end结尾')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    parser.add_argument('--project', choices=['screw', 'packaging'], required=True)
    parser.add_argument('--station', type=int, choices=[1, 2], required=True)
    parser.add_argument('--group-id', help='文本必填，例如 G1 或 001；不是工位号/工号')
    parser.add_argument('--result', choices=['OK', 'NG'], help='文本必填：螺钉检测结果或包装箱总结果')
    parser.add_argument('--worker-id', help='仅旧 --json-v2 使用，不与组别号混用')
    parser.add_argument('--json-v2', action='store_true', help='显式兼容旧JSON v2（不携带新字段）')
    parser.add_argument('--msg-id', help='仅JSON v2重试使用；文本没有检测编号')
    parser.add_argument('--count', type=int)
    parser.add_argument('--barcode', help='完整字符串；未读到时明确传空字符串')
    parser.add_argument('--logo', choices=['OK', 'NG'])
    parser.add_argument('--flame', choices=['OK', 'NG'])
    args = parser.parse_args()
    message = {'v': 2, 'type': 'result', 'msg_id': args.msg_id or uuid.uuid4().hex,
               'project': args.project, 'station': args.station}
    if args.json_v2:
        if args.worker_id is None or args.group_id is not None or args.result is not None:
            parser.error('旧JSON v2使用 --worker-id，不接受 --group-id 或 --result。')
        message['worker_id'] = args.worker_id
    else:
        if args.group_id is None or args.result is None or args.worker_id is not None or args.msg_id is not None:
            parser.error('文本必须提供 --group-id 和 --result；不接受 --worker-id 或 --msg-id。')
        message['group_id'] = args.group_id
        message['detection_result' if args.project == 'screw' else 'total_result'] = args.result
    if args.project == 'screw':
        if args.count is None or any(v is not None for v in (args.barcode, args.logo, args.flame)):
            parser.error('螺钉项目使用 --count，不接受条码/LOGO/火焰字段。')
        message['screw_count'] = args.count
    else:
        if args.count is not None or any(v is None for v in (args.barcode, args.logo, args.flame)):
            parser.error('包装箱必须提供 --barcode、--logo 和 --flame，不接受 --count。')
        message.update(barcode=args.barcode, logo=args.logo, flame=args.flame)
    try:
        if args.json_v2:
            print('发送：' + json.dumps(message, ensure_ascii=False))
            print('回执：' + json.dumps(send_result(args.host, args.port, message), ensure_ascii=False))
        else:
            print('发送：' + encode_result(message).decode('utf-8'))
            print('回执：' + send_text_result(args.host, args.port, message))
    except (OSError, ValueError) as exc:
        advice = 'JSON重试必须保留原msg_id和内容。' if args.json_v2 else '文本每次重发都会新增记录，请勿盲目重发。'
        parser.exit(1, f'未确认保存：{exc}\n请核对服务器记录；{advice}\n')


if __name__ == '__main__':
    main()
