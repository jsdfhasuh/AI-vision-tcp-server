"""Fixed text reports: project, station, group, observation(s), reported verdict, end.

Text observations get a fresh internal UUID; repeated text is a new detection.
JSON v2 remains an explicit legacy wire format with separate worker-ID semantics.
"""
from __future__ import annotations

import re
import uuid

from . import station_protocol as p

# Commas BEFORE the terminal end field (screw: 6 fields; packaging: 8).
COMMAS = {b'screw': 5, b'packaging': 7, b'hello': 3, b'ping': 1}


class FrameDecoder:
    """Bounded incremental framing, using the final field position, not a substring.

    `end` inside a group or barcode is ordinary data. Text/JSON mode is fixed
    per connection. Optional CR/LF separates frames but never trims fields.
    """
    def __init__(self) -> None:
        self.mode: str | None = None

    def pop(self, buffer: bytearray) -> bytes | None:
        if self.mode != 'json':
            start = 0
            while start < len(buffer) and buffer[start] in (10, 13):
                start += 1
            if start:
                del buffer[:start]
        if not buffer:
            return None
        if self.mode is None:
            probe = bytes(buffer).lstrip(b' \t\r')
            if not probe:
                return None
            self.mode = 'json' if probe[0] == ord('{') else 'text'
        if self.mode == 'json':
            end = buffer.find(b'\n')
            if end < 0:
                return None
            length = end + 1
        else:
            first = buffer.find(b',')
            if first < 0:
                return None
            commas = COMMAS.get(bytes(buffer[:first]))
            if commas is None:
                raise p.ProtocolError('FORMAT', 'unknown text message prefix')
            pos = -1
            for _ in range(commas):
                pos = buffer.find(b',', pos + 1)
                if pos < 0:
                    return None
            length = pos + 4
            if len(buffer) < length:
                return None
            if buffer[pos + 1:length] != b'end':
                raise p.ProtocolError('FORMAT', 'wrong field count or terminator; expected end')
        if length - (self.mode == 'json') > p.MAX_FRAME:
            raise p.ProtocolError('LIMIT', 'frame exceeds 16 KiB')
        raw = bytes(buffer[:length])
        del buffer[:length]
        return raw


def validate_result(message: dict) -> dict:
    """Validate internal text observation data; no group/worker-ID substitution."""
    required = {'v', 'type', 'msg_id', 'project', 'station', 'group_id'}
    if type(message.get('v')) is not int or message['v'] != 2 or message.get('type') != 'result':
        raise p.ProtocolError('FORMAT', 'expected a normalized text result')
    p.plain(message.get('msg_id'), 'msg_id')
    project, _ = p.route(message.get('project'), message.get('station'))
    group = p.plain(message.get('group_id'), 'group_id')
    if group != group.strip() or not group.strip() or ',' in group:
        raise p.ProtocolError('FORMAT', 'group_id must be nonempty, trimmed text without commas')
    if project == 'screw':
        required |= {'screw_count', 'detection_result'}
        count = message.get('screw_count')
        if type(count) is not int or not 0 <= count <= 2_147_483_647:
            raise p.ProtocolError('FORMAT', 'screw_count must be a nonnegative integer')
        verdicts = ('detection_result',)
    else:
        required |= {'barcode', 'logo', 'flame', 'total_result'}
        barcode = p.plain(message.get('barcode'), 'barcode', 512, empty=True)
        if ',' in barcode:
            raise p.ProtocolError('FORMAT', 'barcode cannot contain commas in text protocol')
        verdicts = ('logo', 'flame', 'total_result')
    for key in verdicts:
        if message.get(key) not in ('OK', 'NG'):
            raise p.ProtocolError('FORMAT', f'{key} must be exactly OK or NG')
    if set(message) != required:
        raise p.ProtocolError('FORMAT', 'required field missing or unknown field present')
    return message


def decode_text(raw: bytes) -> dict:
    """Decode only the two current result layouts (plus hello/ping controls)."""
    if not raw or len(raw) > p.MAX_FRAME:
        raise p.ProtocolError('FORMAT', 'empty or oversized text frame')
    try:
        fields = raw.decode('utf-8', 'strict').split(',')
    except UnicodeError as exc:
        raise p.ProtocolError('FORMAT', 'text reports must use UTF-8') from exc
    commas = COMMAS.get(fields[0].encode('utf-8'))
    if commas is None or len(fields) != commas + 1 or fields[-1] != 'end':
        raise p.ProtocolError('FORMAT', 'wrong prefix, field count or end marker')
    message = {'v': 2, 'msg_id': uuid.uuid4().hex}
    try:
        if fields[0] == 'ping':
            message['type'] = 'ping'
            return p.validate(message)
        hello = fields[0] == 'hello'
        project, station = (fields[1], fields[2]) if hello else fields[:2]
        if station not in ('1', '2'):
            raise p.ProtocolError('FORMAT', 'station must be 1 or 2')
        message.update(type='hello' if hello else 'result', project=project, station=int(station))
        if hello:
            return p.validate(message)
        message['group_id'] = fields[2]
        if project == 'screw':
            if not re.fullmatch(r'[0-9]{1,10}', fields[3]):
                raise p.ProtocolError('FORMAT', 'count must be an unsigned decimal integer')
            message.update(screw_count=int(fields[3]), detection_result=fields[4])
        else:
            message.update(barcode=fields[3], logo=fields[4], flame=fields[5], total_result=fields[6])
        return validate_result(message)
    except p.ProtocolError as exc:
        raise p.ProtocolError('FORMAT', str(exc)) from exc


def encode_result(message: dict) -> bytes:
    """Encode a validated group observation; internal v/type/msg_id are not sent."""
    validate_result(message)
    fields = [message['project'], str(message['station']), message['group_id']]
    if message['project'] == 'screw':
        fields.extend([str(message['screw_count']), message['detection_result']])
    else:
        fields.extend([message['barcode'], message['logo'], message['flame'], message['total_result']])
    return (','.join(fields) + ',end').encode('utf-8')


def encode_reply(message: dict) -> bytes:
    """Acknowledge persistence, not the submitted verdict or barcode comparison."""
    kind = message.get('type')
    if kind == 'result_ack' and message.get('recorded') is True:
        return b'ACK,end'
    if kind == 'hello_ok':
        return b'HELLO,end'
    if kind == 'pong':
        return b'PONG,end'
    if kind == 'error':
        code = message.get('code')
        if code not in {'STATION_BUSY', 'STATION_MISMATCH', 'IDENTIFY_FIRST', 'LIMIT'}:
            code = 'FORMAT'
        return f'ERR,{code},end'.encode('ascii')
    raise ValueError('unsupported text response')
