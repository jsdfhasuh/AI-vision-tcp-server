"""Versioned catalog/selection operations, serialized with result persistence.

Stored in the existing standards history. Old rows keep their original barcode
and are interpreted as a shared LEGACY item; no historical result is rewritten.
"""
from __future__ import annotations

import json
from typing import Callable

from .standard_json import validate_boxes
from .station_protocol import ProtocolError, fingerprint, plain, route
from .storage import utc_now, json_text


class CatalogMixin:
    def migrate_catalog(self) -> None:
        """The caller backs up existing v1 DB before this atomic migration."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('ALTER TABLE standards ADD COLUMN boxes_json TEXT')
            self.db.execute('ALTER TABLE standards ADD COLUMN selections_json TEXT')
            self.db.execute('ALTER TABLE results ADD COLUMN standard_box_id TEXT')
            self.db.execute('ALTER TABLE results ADD COLUMN standard_box_name TEXT')
            self.db.execute('PRAGMA user_version=2')
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def catalog_from_row(row) -> dict:
        if row['boxes_json'] is None:
            code = row['barcode']
            boxes = [{'id': 'LEGACY', 'name': '原标准条码', 'standard_barcode': code}] if code else []
            selections = {'1': 'LEGACY' if code else None, '2': 'LEGACY' if code else None}
        else:
            boxes = json.loads(row['boxes_json'])
            selections = json.loads(row['selections_json'])
        return {'revision': row['revision'], 'updated_utc': row['updated_utc'], 'actor': row['actor'],
                'boxes': boxes, 'selections': selections}

    def catalog(self) -> dict:
        with self.lock:
            row = self.db.execute('SELECT * FROM standards ORDER BY revision DESC LIMIT 1').fetchone()
            return self.catalog_from_row(row)

    def _change_catalog(self, revision: int, request_id: str, actor: str,
                        digest: str, build: Callable[[dict], tuple[list, dict]]) -> dict:
        plain(request_id, 'request_id', 128)
        plain(actor, 'actor', 64)
        if len(request_id) < 16 or type(revision) is not int or revision < 0:
            raise ProtocolError('INVALID_FIELD', 'invalid request ID or revision')
        with self.transaction() as db:
            old = db.execute('SELECT * FROM standard_updates WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise ProtocolError('REQUEST_CONFLICT', '请求编号已用于其他配置操作。')
                return {'ok': True, 'revision': old['revision'], 'duplicate': True}
            current = self.catalog_from_row(db.execute(
                'SELECT * FROM standards ORDER BY revision DESC LIMIT 1').fetchone())
            if revision != current['revision']:
                raise ProtocolError('STATE_CHANGED', '清单或工位标准已改变，请重新载入后确认。')
            boxes, selections = build(current)
            # Keep old single-standard readers useful only when both slots share one item.
            common = boxes[0]['standard_barcode'] if len(boxes) == 1 and all(
                selections[str(n)] == boxes[0]['id'] for n in (1, 2)) else ''
            new_revision = revision + 1
            db.execute('''INSERT INTO standards(revision,barcode,updated_utc,actor,boxes_json,selections_json)
                          VALUES(?,?,?,?,?,?)''',
                       (new_revision, common, utc_now(), actor, json_text(boxes), json_text(selections)))
            db.execute('INSERT INTO standard_updates VALUES(?,?,?)', (request_id, digest, new_revision))
            return {'ok': True, 'revision': new_revision, 'duplicate': False}

    def set_standard(self, barcode: str, revision: int, request_id: str, actor: str) -> dict:
        """Legacy API explicitly replaces the catalog and applies the one item to both slots."""
        plain(barcode, 'barcode', 512, empty=True)
        # Same fingerprint as v1, so old acknowledged HTTP retries survive migration.
        digest = fingerprint({'barcode': barcode, 'revision': revision, 'actor': actor})
        boxes = [{'id': 'LEGACY', 'name': '原标准条码', 'standard_barcode': barcode}] if barcode else []
        selected = 'LEGACY' if barcode else None
        return self._change_catalog(revision, request_id, actor, digest,
                                    lambda _: (boxes, {'1': selected, '2': selected}))

    def set_catalog(self, boxes: list[dict], revision: int, request_id: str, actor: str) -> dict:
        boxes = validate_boxes(boxes)
        digest = fingerprint({'operation': 'catalog', 'boxes': boxes, 'revision': revision, 'actor': actor})
        def build(current):
            before = {b['id']: b['standard_barcode'] for b in current['boxes']}
            after = {b['id']: b['standard_barcode'] for b in boxes}
            # Deleted IDs and changed barcodes clear selection instead of silently switching targets.
            selected = {str(n): current['selections'][str(n)] for n in (1, 2)}
            for n, bid in selected.items():
                if bid not in after or before.get(bid) != after[bid]:
                    selected[n] = None
            return boxes, selected
        return self._change_catalog(revision, request_id, actor, digest, build)

    def select_standard(self, station: int, box_id: str | None, revision: int,
                        request_id: str, actor: str) -> dict:
        route('packaging', station)
        if box_id is not None:
            plain(box_id, 'box_id', 64)
        digest = fingerprint({'operation': 'select', 'station': station, 'box_id': box_id,
                              'revision': revision, 'actor': actor})
        def build(current):
            if box_id is not None and not any(b['id'] == box_id for b in current['boxes']):
                raise ProtocolError('UNKNOWN_BOX', '选定的包装箱不在当前标准清单内。')
            selected = dict(current['selections'])
            selected[str(station)] = box_id
            return current['boxes'], selected
        return self._change_catalog(revision, request_id, actor, digest, build)

    def expected_box(self, db, station: int) -> tuple[dict, dict | None]:
        current = self.catalog_from_row(db.execute(
            'SELECT * FROM standards ORDER BY revision DESC LIMIT 1').fetchone())
        bid = current['selections'][str(station)]
        box = next((b for b in current['boxes'] if b['id'] == bid), None)
        return current, box
