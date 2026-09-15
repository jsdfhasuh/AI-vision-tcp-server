"""Validate a user-selected JSON file; never interpret its content as a path.

The file is an import source, not a live watched configuration. StationStore
remains authoritative after a successful, revision-checked import.
"""
from __future__ import annotations

from .station_protocol import ProtocolError, decode, plain

MAX_JSON_BYTES = 4096
EXAMPLE_STANDARD = {'standard_barcode': '001234-AbC'}


def parse_standard_json(content: object) -> str:
    """Return the exact barcode, accepting UTF-8 with an optional leading BOM."""
    if not isinstance(content, str):
        raise ValueError('JSON 文件内容必须是 UTF-8 文本。')
    try:
        raw = content.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise ValueError('JSON 文件包含无效的 Unicode 字符。') from exc
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValueError('JSON 文件不能为空，且最多为 4 KiB。')
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    try:
        data = decode(raw)  # Reject duplicate keys, non-finite values and deep nesting.
    except ProtocolError as exc:
        raise ValueError('JSON 格式无效：请使用单个对象，不允许重复字段、注释或尾随逗号。') from exc
    if set(data) != {'standard_barcode'}:
        raise ValueError('JSON 只能包含 standard_barcode 一个字段。')
    try:
        return plain(data['standard_barcode'], 'standard_barcode', 512, empty=True)
    except ProtocolError as exc:
        raise ValueError('standard_barcode 必须是字符串，最多512个字符，不能含控制字符；不要填写数字类型。') from exc
