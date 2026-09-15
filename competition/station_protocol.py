"""TCP v2: self-contained station observations, never referee rounds or scores."""
from __future__ import annotations

from .protocol import ProtocolError, decode, encode, fingerprint, MAX_FRAME

VERSION = 2
PROJECTS = ('screw', 'packaging')


def plain(value: object, field: str, limit: int = 64, *, empty: bool = False) -> str:
    """Validate without trimming/coercion: barcodes and employee IDs are text."""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ProtocolError('INVALID_FIELD', f'{field} must be text (max {limit})')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise ProtocolError('INVALID_FIELD', f'{field} contains invalid Unicode') from exc
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError('INVALID_FIELD', f'{field} contains control characters')
    return value


def route(project: object, station: object) -> tuple[str, int]:
    if project not in PROJECTS or type(station) is not int or station not in (1, 2):
        raise ProtocolError('INVALID_STATION', 'project must be screw/packaging and station must be integer 1/2')
    return project, station


def validate(message: dict) -> dict:
    if type(message.get('v')) is not int or message['v'] != VERSION:
        raise ProtocolError('BAD_VERSION', 'this endpoint requires TCP v2; v1 rounds are not supported')
    kind = plain(message.get('type'), 'type', 16)
    mid = plain(message.get('msg_id'), 'msg_id')
    if mid != mid.strip():
        raise ProtocolError('INVALID_FIELD', 'msg_id must not have surrounding whitespace')
    required = {'v', 'type', 'msg_id'}
    if kind in ('hello', 'result'):
        project, _ = route(message.get('project'), message.get('station'))
        required |= {'project', 'station'}
    elif kind != 'ping':
        raise ProtocolError('UNKNOWN_TYPE', 'supported types: hello, result, ping')
    if kind == 'result':
        required.add('worker_id')
        worker = plain(message.get('worker_id'), 'worker_id')
        if not worker.strip() or worker != worker.strip():
            raise ProtocolError('INVALID_FIELD', 'worker_id must be nonempty without surrounding whitespace')
        if project == 'screw':
            required.add('screw_count')
            count = message.get('screw_count')
            if type(count) is not int or not 0 <= count <= 2_147_483_647:
                raise ProtocolError('INVALID_COUNT', 'screw_count must be a nonnegative integer')
        else:
            required |= {'barcode', 'logo', 'flame'}
            # Empty means the reader did not read a barcode, not a fabricated value.
            plain(message.get('barcode'), 'barcode', 512, empty=True)
            for key in ('logo', 'flame'):
                if message.get(key) not in ('OK', 'NG'):
                    raise ProtocolError('INVALID_VERDICT', f'{key} must be exactly OK or NG')
    if set(message) != required:
        raise ProtocolError('INVALID_FIELDS', 'required field missing or unknown field present')
    return message
