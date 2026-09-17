"""Schema v3 migration and immutable group/reported-result regressions."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from competition.station_store import StationStore, APPLICATION_ID, DDL
from competition.packaging_catalog import CatalogMixin
from competition.station_protocol import fingerprint, ProtocolError
from competition.station_text import decode_text
from competition.storage import utc_now


def old_database(path: Path, version: int = 2):
    db=sqlite3.connect(path)
    try:
        for sql in DDL:db.execute(sql)
        db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute('PRAGMA user_version=1')
        db.execute('INSERT INTO standards VALUES(0,?,?,?)',('',utc_now(),'system'))
        db.execute('INSERT INTO standards VALUES(1,?,?,?)',('001-AbC',utc_now(),'admin'))
        db.execute('INSERT INTO standard_updates VALUES(?,?,?)',('a'*32,fingerprint({'barcode':'001-AbC','revision':0,'actor':'admin'}),1))
        m={'v':2,'type':'result','project':'packaging','station':1,'worker_id':'OLD001','msg_id':'old-id','barcode':'001-AbC','logo':'OK','flame':'NG'}
        db.execute('''INSERT INTO results(received_utc,project,station,worker_id,msg_id,fingerprint,barcode,
            barcode_status,logo,flame,standard_revision,standard_barcode,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (utc_now(),'packaging',1,'OLD001','old-id',fingerprint(m),'001-AbC','MATCH','OK','NG',1,'001-AbC',json.dumps(m)))
        db.commit()
        if version==2:
            c=CatalogMixin();c.db=db;c.migrate_catalog()
        return m
    finally:db.close()


class GroupMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'station-results.sqlite3'

    def open(self):
        s=StationStore(self.path);self.addCleanup(s.close);return s

    def test_new_database_has_schema_3_without_backup(self):
        s=self.open();self.assertEqual(s.db.execute('PRAGMA user_version').fetchone()[0],3);self.assertIsNone(s.migration_backup)
        self.assertTrue({'group_id','detection_result','total_result'}<={r[1] for r in s.db.execute('PRAGMA table_info(results)')})

    def test_v2_upgrade_backup_preserves_raw_history(self):
        m=old_database(self.path)
        with sqlite3.connect(self.path) as db:before=db.execute('SELECT raw_json,fingerprint,received_utc FROM results').fetchone()
        s=self.open();self.assertTrue(s.migration_backup.is_file());r=s.latest('packaging',1)
        self.assertEqual((r['raw_json'],r['fingerprint'],r['received_utc']),before)
        self.assertEqual(r['worker_id'],'OLD001');self.assertIsNone(r['group_id']);self.assertIsNone(r['total_result'])
        self.assertEqual(s.catalog()['selections'],{'1':'LEGACY','2':'LEGACY'})
        with sqlite3.connect(s.migration_backup) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertNotIn('group_id',{r[1] for r in db.execute('PRAGMA table_info(results)')})
        self.assertTrue(s.record(m,utc_now())[1])

    def test_v1_upgrade_backup_receipts_and_standards(self):
        old_database(self.path,1);s=self.open()
        with sqlite3.connect(s.migration_backup) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        self.assertEqual(s.db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertTrue(s.set_standard('001-AbC',0,'a'*32,'admin')['duplicate'])
        self.assertEqual(s.latest('packaging',1)['barcode_status'],'MATCH')

    def test_backup_failure_stops_upgrade(self):
        old_database(self.path)
        with patch.object(StationStore,'backup',side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):self.open()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertNotIn('group_id',{r[1] for r in db.execute('PRAGMA table_info(results)')})
        self.open()  # Owner lock and connection were released even on failure.

    def test_ddl_failure_rolls_back_added_columns(self):
        old_database(self.path)
        with sqlite3.connect(self.path) as db:db.execute('ALTER TABLE results ADD COLUMN detection_result TEXT')
        with self.assertRaises(sqlite3.Error):self.open()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertNotIn('group_id',{r[1] for r in db.execute('PRAGMA table_info(results)')})
            self.assertEqual(db.execute('SELECT worker_id FROM results').fetchone()[0],'OLD001')

    def test_reopen_current_schema_no_second_migration(self):
        old_database(self.path);s=self.open();backup=s.migration_backup;s.close();s=self.open()
        self.assertIsNone(s.migration_backup);self.assertEqual(list(backup.parent.glob('*.sqlite3')),[backup])

    def test_future_database_rejected_unchanged(self):
        old_database(self.path)
        with sqlite3.connect(self.path) as db:db.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError):self.open()
        with sqlite3.connect(self.path) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],999)

    def test_schema3_with_missing_columns_is_rejected(self):
        old_database(self.path)
        with sqlite3.connect(self.path) as db:db.execute('PRAGMA user_version=3')
        with self.assertRaises(ValueError):self.open()


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=StationStore(Path(self.tmp.name)/'station-results.sqlite3');self.addCleanup(self.store.close)

    def put(self,wire):return self.store.record(decode_text(wire.encode()),utc_now())[0]

    def test_count_not_used_to_recompute_verdict(self):
        for wire,count,verdict in [('screw,1,G1,4,NG,end',4,'NG'),('screw,2,G2,0,OK,end',0,'OK')]:
            r=self.put(wire);self.assertEqual((r['screw_count'],r['detection_result']),(count,verdict))

    def test_total_not_overridden_by_logo_flame_or_barcode(self):
        self.store.set_standard('EXPECTED',0,uuid.uuid4().hex,'admin')
        r=self.put('packaging,1,G1,OTHER,NG,NG,OK,end')
        self.assertEqual((r['barcode_status'],r['logo'],r['flame'],r['total_result']),('MISMATCH','NG','NG','OK'))

    def test_empty_barcode_not_substituted_by_total(self):
        r=self.put('packaging,1,001,,OK,OK,OK,end');self.assertEqual((r['barcode'],r['barcode_status'],r['total_result']),('','UNREAD','OK'))

    def test_group_cannot_substitute_or_choose_station_standard(self):
        self.store.set_catalog([{'id':'G1','name':'box','standard_barcode':'ABC'}],0,uuid.uuid4().hex,'admin')
        r=self.put('packaging,1,G1,ABC,OK,OK,OK,end');self.assertEqual(r['barcode_status'],'UNSELECTED')

    def test_no_fake_worker_id_in_new_rows(self):
        r=self.put('screw,1,001,4,OK,end');self.assertEqual(r['worker_id'],'');self.assertEqual(r['group_id'],'001')
        self.assertNotIn('worker_id',json.loads(r['raw_json']))

    def test_new_fields_are_immutable(self):
        self.put('screw,1,G1,4,NG,end')
        for sql in ["UPDATE results SET group_id='G2'","UPDATE results SET detection_result='OK'",'DELETE FROM results']:
            with self.assertRaises(sqlite3.IntegrityError),self.store.transaction() as db:db.execute(sql)

    def test_new_group_without_result_rejected_without_write(self):
        m=decode_text(b'screw,1,G1,4,OK,end');m.pop('detection_result')
        with self.assertRaises(ProtocolError):self.store.record(m,utc_now())
        self.assertEqual(self.store.records('screw'),[])

    def test_restart_and_backup_keep_new_fields(self):
        self.put('packaging,2,002,ABC,OK,NG,OK,end');backup=self.store.backup();path=self.store.path;self.store.close()
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT group_id,total_result FROM results').fetchone(),('002','OK'))
        s=StationStore(path)
        try:self.assertEqual(s.public(s.latest('packaging',2))['total_result'],'OK')
        finally:s.close()

    def test_history_legacy_worker_is_not_group(self):
        m={'v':2,'type':'result','msg_id':'old','project':'screw','station':1,'worker_id':'G1','screw_count':4}
        r,_=self.store.record(m,utc_now());public=self.store.public(r)
        self.assertEqual(public['worker_id'],'G1');self.assertIsNone(public['group_id']);self.assertIsNone(public['detection_result'])

    def test_invalid_new_fields_without_group_not_ignored(self):
        m={'v':2,'type':'result','msg_id':'bad','project':'screw','station':1,'worker_id':'D1','screw_count':4,'detection_result':'OK'}
        with self.assertRaises(ProtocolError):self.store.record(m,utc_now())


if __name__=='__main__':unittest.main()
