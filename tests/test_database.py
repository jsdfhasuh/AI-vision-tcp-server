from __future__ import annotations
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from competition.core import Engine
from competition.storage import Store, DataLock, json_text, utc_now
from competition import protocol
from competition.dbtools import (APPLICATION_ID, backup_database, digest_file, inspect_database,
                                  reader, restore_backup, search_sessions)
from competition.schema import migrate_to_v2
from competition.display import public_snapshot
from test_system import FakePeer

ROOT = Path(__file__).resolve().parents[1]


def legacy_file(path: Path, *, open_session: bool = False) -> None:
    db = sqlite3.connect(path)
    db.executescript((ROOT / 'tests/fixtures/schema_v1.sql').read_text())
    with db:
        db.execute("INSERT INTO sessions VALUES('old','2026-09-01T10:00:00.000+00:00',NULL,?,'旧版队伍','vision-01','packaging','SECRET',1,'{}')", ('OPEN' if open_session else 'CLOSED',))
    db.close()


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'competition.sqlite3'
        self.store = Store(self.path)
        self.engine = Engine(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, mode='PRACTICE', team='数据库测试', competition='packaging'):
        session = self.engine.new_session(team, 'vision-01', competition, 'CODE', mode=mode)
        self.peer = FakePeer()
        self.engine.receive(self.peer, protocol.encode({'v':1,'type':'hello','msg_id':uuid.uuid4().hex,'client_id':'vision-01','access_code':'CODE'}))
        self.engine.receive(self.peer, protocol.encode({'v':1,'type':'target_ack','msg_id':uuid.uuid4().hex,'session_id':session['id'],'target_revision':1}))
        return session

    def result(self, rid, verdict='OK', mid=None):
        msg={'v':1,'type':'result','msg_id':mid or uuid.uuid4().hex, 'session_id':self.engine.session_id,'round_id':rid,'target_revision':1,'verdict':verdict}
        self.engine.receive(self.peer,protocol.encode(msg))
        return msg

    def test_new_schema_identity(self):
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
        self.assertIsNone(self.store.migration_backup)
        self.assertTrue(self.store.health()['integrity_ok'])

    def test_official_and_practice_are_separate(self):
        self.session()
        self.session('OFFICIAL')
        self.assertEqual(search_sessions(self.path,mode='PRACTICE')['total'],1)
        self.assertEqual(search_sessions(self.path,mode='OFFICIAL')['total'],1)
        self.assertEqual(search_sessions(self.path,mode='LEGACY')['total'],0)

    def test_mode_immutable_and_unknown_rejected(self):
        session=self.session()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("UPDATE sessions SET mode='OFFICIAL'")
        with self.assertRaises(ValueError):
            self.engine.new_session('x','x','screw',mode='LEGACY')
        self.assertEqual(self.engine.session_id, session['id'])
        self.assertEqual(search_sessions(self.path,state='OPEN')['total'],1)

    def test_target_revision_history(self):
        session=self.session()
        self.engine.set_target('B','M','L')
        self.engine.set_target('C')
        with reader(self.path) as db:
            rows=db.execute('SELECT revision,target_json FROM target_revisions WHERE session_id=? ORDER BY revision',(session['id'],)).fetchall()
        self.assertEqual([r[0] for r in rows],[1,2,3])
        self.assertEqual(json.loads(rows[1][1])['box_type'],'B')

    def test_target_update_is_atomic(self):
        session=self.session()
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_target BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.set_target('B')
        self.assertEqual(self.engine.session['target_revision'],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM target_revisions').fetchone()[0],1)

    def test_settings_import_once(self):
        old=Path(self.temp.name)/'settings.json'
        old.write_text(json.dumps({'team':'A','display_title':'测试标题','access_code':'DO_NOT_IMPORT'}))
        self.assertEqual(self.store.load_settings(old)['team'],'A')
        self.store.save_settings({'team':'B'})
        old.write_text('{"team":"C"}')
        result=self.store.load_settings(old)
        self.assertEqual(result['team'],'B')
        self.assertNotIn('access_code',result)

    def test_settings_invalid_legacy_is_tolerated(self):
        old=Path(self.temp.name)/'settings.json'
        old.write_text('broken')
        self.assertEqual(self.store.load_settings(old),{})

    def test_settings_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save_settings({'access_code':'no'})
        self.assertEqual(self.store.load_settings(),{})

    def test_literal_search_no_sql_injection(self):
        self.session(team='100%_中文队伍')
        self.session(team='其他队伍')
        self.assertEqual(search_sessions(self.path,team='%_')['total'],1)
        self.assertEqual(search_sessions(self.path,team="' OR 1=1 --")['total'],0)
        self.assertEqual(search_sessions(self.path)['total'],2)

    def test_filters_and_pagination(self):
        for i in range(7):
            self.session(team=str(i),competition='screw' if i%2 else 'packaging')
        a=search_sessions(self.path,limit=3)
        b=search_sessions(self.path,limit=3,offset=3)
        self.assertEqual(a['total'],7)
        self.assertFalse({r['id'] for r in a['sessions']} & {r['id'] for r in b['sessions']})
        self.assertEqual(search_sessions(self.path,competition='screw')['total'],3)
        self.assertNotIn('access_code',a['sessions'][0])
        self.assertEqual(search_sessions(self.path,date_from='2000-01-01',date_to='2099-01-01')['total'],7)
        self.assertEqual(search_sessions(self.path,date_from='2000-01-01',date_to='2001-01-01')['total'],0)

    def test_invalid_query_arguments(self):
        for kwargs in ({'limit':0},{'offset':-1},{'mode':'BAD'},{'date_from':'garbage'}, {'date_from':'2026-10-01','date_to':'2026-01-01'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                search_sessions(self.path,**kwargs)

    def test_final_result_cannot_be_edited(self):
        self.session()
        rid=self.engine.start_round('完整','OK')
        self.result(rid)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("UPDATE rounds SET actual='NG' WHERE id=?",(rid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_round(rid,status='CANCELLED')
        self.store.increment(rid,'retries')
        self.assertEqual(self.store.get_round(rid)['actual'],'OK')

    def test_receipt_failure_rolls_back_result(self):
        self.session()
        rid=self.engine.start_round('完整','OK')
        with self.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.result(rid)
        self.assertIsNone(self.store.get_round(rid)['actual'])
        self.assertIsNotNone(self.engine.fatal)
        self.assertFalse(any(m['type']=='result_ack' for m in self.peer.sent))

    def test_independent_reader_snapshot(self):
        self.session()
        with reader(self.path) as db:
            count=db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
            self.session()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],count)
        self.assertEqual(search_sessions(self.path)['total'],2)

    def test_slow_export_does_not_hold_result_lock(self):
        self.session()
        rid=self.engine.start_round('完整','OK',2000)
        entered, release=threading.Event(),threading.Event()
        exports,errors=[],[]
        import csv
        original=csv.writer
        def slow(*args,**kwargs):
            entered.set()
            release.wait(5)
            return original(*args,**kwargs)
        def export():
            try:
                exports.append(self.engine.export(self.engine.session_id,Path(self.temp.name)/'exports'))
            except Exception as exc:
                errors.append(exc)
        with patch('competition.storage.csv.writer',slow):
            thread=threading.Thread(target=export)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.result(rid)
                self.assertEqual(self.store.get_round(rid)['status'],'RECEIVED')
                self.assertTrue(thread.is_alive())
                self.assertIsNotNone(public_snapshot(self.engine,'标题','说明'))
            finally:
                release.set();thread.join(5)
        self.assertFalse(errors)
        summary=json.loads((exports[0]/'summary.json').read_text())
        self.assertEqual(summary['statistics']['received'],0) # snapshot before receipt
        self.assertIn('snapshot_audit_max_id',summary)
        self.assertEqual(self.store.stats(self.engine.session_id)['received'],1)

    def test_online_backup_includes_committed_results_and_settings(self):
        self.session('OFFICIAL')
        self.store.save_settings({'display_title':'持久化标题'})
        rid=self.engine.start_round('完整','OK')
        self.result(rid)
        folder=self.store.backup()
        info=inspect_database(folder/'database.sqlite3')
        manifest=json.loads((folder/'manifest.json').read_text())
        self.assertTrue(info['integrity_ok'])
        self.assertEqual(info['counts']['receipts'],1)
        self.assertEqual(digest_file(folder/'database.sqlite3'),manifest['sha256'])
        self.assertEqual(info['journal_mode'],'delete')
        self.assertEqual(search_sessions(self.path,state='OPEN')['total'],1) # no accidental recovery

    def test_backup_restore_new_directory_recovery(self):
        self.session()
        received=self.engine.start_round('完整','OK')
        self.result(received)
        waiting=self.engine.start_round('待检测','NG')
        backup=self.store.backup()
        restored=restore_backup(backup,Path(self.temp.name)/'restored')
        second=Store(restored)
        try:
            self.assertEqual(second.get_round(received)['actual'],'OK')
            self.assertEqual(second.get_round(waiting)['status'],'INTERRUPTED')
            self.assertEqual(self.store.get_round(waiting)['status'],'WAITING')
            self.assertEqual(second.sessions()[0]['state'],'INTERRUPTED')
        finally:
            second.close()

    def test_backup_hash_change_refuses_restore(self):
        self.session()
        backup=self.store.backup()
        with (backup/'database.sqlite3').open('ab') as f:
            f.write(b'tampered')
        dest=Path(self.temp.name)/'invalid_restore'
        with self.assertRaisesRegex(ValueError,'SHA256'):
            restore_backup(backup,dest)
        self.assertFalse((dest/'competition.sqlite3').exists())
        self.assertTrue(self.store.health()['integrity_ok'])

    def test_restore_never_overwrites_existing_directory(self):
        self.session()
        backup=self.store.backup()
        before=self.store.stats(self.engine.session_id)
        with self.assertRaisesRegex(ValueError,'覆盖'):
            restore_backup(backup,Path(self.temp.name))
        self.assertEqual(before,self.store.stats(self.engine.session_id))

    def test_backup_failure_keeps_source(self):
        self.session()
        with self.assertRaises(TimeoutError):
            backup_database(self.path,Path(self.temp.name)/'failed',timeout_seconds=1e-12)
        self.assertFalse(list((Path(self.temp.name)/'failed').iterdir()))
        self.assertTrue(self.store.health()['integrity_ok'])

    def test_check_cli_does_not_recover_running_session(self):
        self.session()
        result=subprocess.run([sys.executable,str(ROOT/'database_tools.py'),'--data-dir',self.temp.name,'check'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['integrity_ok'])
        self.assertEqual(search_sessions(self.path,state='OPEN')['total'],1)

    def test_missing_read_does_not_create_database(self):
        missing=Path(self.temp.name)/'none.sqlite3'
        with self.assertRaises(FileNotFoundError):
            inspect_database(missing)
        self.assertFalse(missing.exists())

    def test_public_data_whitelist_keeps_modes_not_secrets(self):
        self.session('OFFICIAL',team='正式队伍')
        rid=self.engine.start_round('PRIVATE_CASE','NG',action='arm',notes='PRIVATE_NOTE')
        data=public_snapshot(self.engine,'标题','说明')
        self.assertEqual(data['session']['mode'],'OFFICIAL')
        self.assertEqual(data['latest']['action'],'arm')
        text=json.dumps(data)
        for private in ('PRIVATE_CASE','PRIVATE_NOTE','expected','access_code','CODE','matched',rid):
            self.assertNotIn(private,text)

    def test_cancelled_rounds_are_not_received(self):
        self.session()
        for _ in range(10):
            self.engine.start_round('取消','OK')
            self.engine.cancel_round('测试')
        data=public_snapshot(self.engine,'标题','说明')
        self.assertEqual(data['counts']['total'],10)
        self.assertEqual(data['counts']['received'],0)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'competition.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def test_v1_upgrade_backs_up_and_marks_legacy(self):
        legacy_file(self.path)
        store=Store(self.path)
        try:
            self.assertIsNotNone(store.migration_backup)
            self.assertEqual(inspect_database(store.migration_backup/'database.sqlite3')['schema_version'],1)
            self.assertEqual(store.sessions()[0]['mode'],'LEGACY')
            self.assertEqual(store.sessions()[0]['team'],'旧版队伍')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM target_revisions').fetchone()[0],1)
        finally:
            store.close()
        second=Store(self.path)
        try:
            self.assertIsNone(second.migration_backup)
        finally:
            second.close()

    def test_failure_rolls_back_schema_and_version(self):
        legacy_file(self.path)
        class Fail(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if sql.startswith('CREATE INDEX rounds_status'):
                    raise sqlite3.OperationalError('injected DDL failure')
                return super().execute(sql,*args,**kwargs)
        db=sqlite3.connect(self.path,factory=Fail)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                migrate_to_v2(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertNotIn('mode',[r[1] for r in db.execute('PRAGMA table_info(sessions)')])
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='settings'").fetchall())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],1)
        finally:
            db.close()

    def test_future_version_rejected_without_downgrade(self):
        legacy_file(self.path)
        db=sqlite3.connect(self.path)
        db.execute('PRAGMA user_version=999');db.close()
        before=digest_file(self.path)
        with self.assertRaisesRegex(ValueError,'999'):
            Store(self.path)
        self.assertEqual(digest_file(self.path),before)
        lock=DataLock(self.path.with_suffix('.lock'));lock.close()

    def test_unknown_database_not_silently_adopted(self):
        db=sqlite3.connect(self.path);db.execute('CREATE TABLE unrelated(x)');db.close()
        with self.assertRaises(ValueError):
            Store(self.path)
        lock=DataLock(self.path.with_suffix('.lock'));lock.close()

    def test_corrupt_file_rejected(self):
        self.path.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.path)
        lock=DataLock(self.path.with_suffix('.lock'));lock.close()

    def test_old_open_session_not_resumed(self):
        legacy_file(self.path,open_session=True)
        store=Store(self.path)
        try:
            self.assertEqual(store.sessions()[0]['state'],'INTERRUPTED')
            self.assertEqual(store.sessions()[0]['mode'],'LEGACY')
            self.assertEqual(inspect_database(store.migration_backup/'database.sqlite3')['counts']['sessions'],1)
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
