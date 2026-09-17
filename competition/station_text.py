"""Comma-separated station reports terminated by a positional `end` field.

No IDs are required on the wire. Each valid text report gets a fresh internal
UUID, so two identical reports are two observations, never a deduplicated retry.
The JSON v2 codec remains separate, including its durable retry semantics.
"""
from __future__ import annotations

import re
import uuid

from . import station_protocol as p

# Number of commas preceding the final `end` token, NOT substring delimiters.
COMMAS = {b'screw': 4, b'packaging': 6, b'hello': 3, b'ping': 1}


class FrameDecoder:
    """Incremental framing; select text/JSON once per connection.

    Call pop repeatedly after appending a bounded socket chunk to buffer. A
    text boundary is determined by its fixed field count, so a barcode equal
    to `end` or containing `end` never prematurely terminates a report.
    Invalid text boundaries close the connection; guessing resynchronization
    could turn part of an invalid barcode into a new observation.
    """
    def __init__(self) -> None:
        self.mode: str | None = None

    def pop(self, buffer: bytearray) -> bytes | None:
        if self.mode != 'json':
            # Optional line endings BETWEEN frames, never strip field contents.
            start = 0
            while start < len(buffer) and buffer[start] in (10, 13):
                start += 1
            if start:
                del buffer[:start]
        if not buffer:
            return None
        if self.mode is None:
            # JSON v2 already permits insignificant leading space/tab characters.
            probe = bytes(buffer).lstrip(b' \t\r')
            if not probe:
                return None
            self.mode = 'json' if probe[0] == ord('{') else 'text'
        if self.mode == 'json':
            end = buffer.find(b'\n')
            if end < 0:
                return None  # Runtime bounds incomplete frames and their lifetime.
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
            length = pos + 4  # comma, then exactly e n d
            if len(buffer) < length:
                return None
            if buffer[pos + 1:length] != b'end':
                raise p.ProtocolError('FORMAT', 'wrong field count or terminator; expected end')
        # JSON limit excludes LF; text limit includes its end marker.
        if length - (self.mode == 'json') > p.MAX_FRAME:
            raise p.ProtocolError('LIMIT', 'frame exceeds 16 KiB')
        raw = bytes(buffer[:length])
        del buffer[:length]
        return raw


def decode_text(raw: bytes) -> dict:
    """Validate a COMPLETE frame, then normalize to the existing storage model."""
    if not raw or len(raw) > p.MAX_FRAME:
        raise p.ProtocolError('FORMAT', 'empty or oversized text frame')
    try:
        fields = raw.decode('utf-8', 'strict').split(',')
    except UnicodeError as exc:
        raise p.ProtocolError('FORMAT', 'text reports must use UTF-8') from exc
    prefix = fields[0].encode('utf-8')
    commas = COMMAS.get(prefix)
    if commas is None or len(fields) != commas + 1 or fields[-1] != 'end':
        raise p.ProtocolError('FORMAT', 'wrong prefix, field count or end marker')
    message = {'v': 2, 'msg_id': uuid.uuid4().hex}
    try:
        if fields[0] == 'ping':
            message['type'] = 'ping'
        else:
            hello = fields[0] == 'hello'
            project, station = (fields[1], fields[2]) if hello else fields[:2]
            if station not in ('1', '2'):
                raise p.ProtocolError('FORMAT', 'station must be 1 or 2')
            message.update(type='hello' if hello else 'result', project=project, station=int(station))
            if not hello:
                message['worker_id'] = fields[2]
                if project == 'screw':
                    # Bound conversion; reject signs, float syntax and Unicode digits.
                    if not re.fullmatch(r'[0-9]{1,10}', fields[3]):
                        raise p.ProtocolError('FORMAT', 'count must be an unsigned decimal integer')
                    message['screw_count'] = int(fields[3])
                else:
                    message.update(barcode=fields[3], logo=fields[4], flame=fields[5])
        return p.validate(message)
    except p.ProtocolError as exc:
        raise p.ProtocolError('FORMAT', str(exc)) from exc


def encode_result(message: dict) -> bytes:
    """Encode a normalized result for the text client; its msg_id is not sent."""
    p.validate(message)
    if message['type'] != 'result':
        raise ValueError('only result observations can be encoded here')
    fields = [message['project'], str(message['station']), message['worker_id']]
    if message['project'] == 'screw':
        fields.append(str(message['screw_count']))
    else:
        fields.extend([message['barcode'], message['logo'], message['flame']])
    if any(',' in value for value in fields):
        raise ValueError('工号和条码不能含英文逗号；简单文本协议没有转义规则。')
    raw = (','.join(fields) + ',end').encode('utf-8')
    decode_text(raw)  # Keep encoder and server acceptance rules in agreement.
    return raw


def encode_reply(message: dict) -> bytes:
    """No standard, calibration result, internal UUID or JSON on text replies."""
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
