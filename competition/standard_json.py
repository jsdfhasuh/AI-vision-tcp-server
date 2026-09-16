"""Strict UTF-8 standard imports. Files are data, never paths or watched config."""
from __future__ import annotations

import json
import re

from .station_protocol import ProtocolError, decode, plain

MAX_JSON_BYTES = 4096  # Legacy helper, retained for existing Python callers.
MAX_CATALOG_BYTES = 256 * 1024
MAX_BOXES = 1000
IMPORT_BODY_BYTES = 2 * 1024 * 1024  # Room for JSON string escaping of a 256 KiB file.
EXAMPLE_STANDARD = {'standard_barcode': '001234-AbC'}
EXAMPLE_CATALOG = {'boxes': [
    {'id': 'BOX01', 'name': '1号包装箱', 'standard_barcode': '001234-AbC'},
    {'id': 'BOX02', 'name': '2号包装箱', 'standard_barcode': '005678-DeF'},
    {'id': 'BOX03', 'name': '3号包装箱', 'standard_barcode': '009012-GhI'},
]}


def _raw(content: object, maximum: int) -> bytes:
    if not isinstance(content, str):
        raise ValueError('JSON 文件内容必须是 UTF-8 文本。')
    try:
        raw = content.encode('utf-8', 'strict')
    except UnicodeError as exc:
        raise ValueError('JSON 文件包含无效的 Unicode 字符。') from exc
    if not raw or len(raw) > maximum:
        raise ValueError(f'JSON 文件不能为空，且最多为 {maximum // 1024} KiB。')
    return raw.removeprefix(b'\xef\xbb\xbf')


def parse_standard_json(content: object) -> str:
    """Legacy single-standard helper: still strict and limited to 4 KiB."""
    try:
        data = decode(_raw(content, MAX_JSON_BYTES))
    except ProtocolError as exc:
        raise ValueError('JSON 格式无效：不允许重复字段、注释或尾随逗号。') from exc
    if set(data) != {'standard_barcode'}:
        raise ValueError('JSON 只能包含 standard_barcode 一个字段。')
    return plain(data['standard_barcode'], 'standard_barcode', 512, empty=True)


def validate_boxes(boxes: object) -> list[dict]:
    """Validate the whole list before any DB change; same barcodes may be reused."""
    if not isinstance(boxes, list) or len(boxes) > MAX_BOXES:
        raise ValueError(f'boxes 必须是数组，最多 {MAX_BOXES} 条。')
    result, seen = [], set()
    for i, box in enumerate(boxes, 1):
        if not isinstance(box, dict) or set(box) != {'id', 'name', 'standard_barcode'}:
            raise ValueError(f'第{i}条必须且只能包含 id、name、standard_barcode。')
        bid = plain(box['id'], 'id', 64)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', bid):
            raise ValueError(f'第{i}条 id 仅限字母、数字、下划线、短横线，首字符须为字母或数字。')
        if bid in seen:
            raise ValueError(f'第{i}条 id 重复：{bid}。')
        seen.add(bid)
        name = plain(box['name'], 'name', 100)
        if not name.strip():
            raise ValueError(f'第{i}条名称不能为空白。')
        barcode = plain(box['standard_barcode'], 'standard_barcode', 512)
        # No trimming, case folding, numeric coercion, or Unicode normalization.
        result.append({'id': bid, 'name': name, 'standard_barcode': barcode})
    if len(json.dumps({'boxes': result}, ensure_ascii=False).encode('utf-8')) > MAX_CATALOG_BYTES:
        raise ValueError('标准清单过大，最多256 KiB。')
    return result


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('JSON 不允许重复字段。')
        obj[key] = value
    return obj


def _invalid_constant(value):
    raise ValueError('JSON 不允许 NaN 或 Infinity。')


def parse_catalog_json(content: object) -> dict:
    """Return {boxes, legacy}; accept the old single object without guessing fields."""
    raw = _raw(content, MAX_CATALOG_BYTES)
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError('JSON 格式无效：不允许重复字段、注释或尾随逗号。') from exc
    if not isinstance(data, dict):
        raise ValueError('JSON 顶层必须是一个对象。')
    if set(data) == {'standard_barcode'}:
        code = plain(data['standard_barcode'], 'standard_barcode', 512, empty=True)
        boxes = [{'id': 'LEGACY', 'name': '原标准条码', 'standard_barcode': code}] if code else []
        return {'boxes': boxes, 'legacy': True}
    if set(data) != {'boxes'}:
        raise ValueError('JSON 顶层只能含 boxes，或兼容的 standard_barcode。')
    return {'boxes': validate_boxes(data['boxes']), 'legacy': False}
