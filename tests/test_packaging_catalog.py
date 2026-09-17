"""Batch standards: strict import, atomic migration, exact per-station checks and HTTP/TCP."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from competition.standard_json import (EXAMPLE_CATALOG, EXAMPLE_STANDARD, MAX_CATALOG_BYTES,
    IMPORT_BODY_BYTES, parse_catalog_json, parse_standard_json, validate_boxes)
from competition.station_store import StationStore, APPLICATION_ID, DDL, SCHEMA_VERSION
from competition.station_protocol import encode, decode, ProtocolError
from competition.station_webapp import create_app
from competition.storage import utc_now
from competition.web_auth import credential_record
from competition.web_config import WebConfig


def mid():
    return uuid.uuid4().hex


def observation(station=1, barcode='001234-AbC', **values):
    return {'v': 2, 'type': 'result', 'msg_id': mid(), 'project': 'packaging',
            'station': station, 'worker_id': '001234', 'barcode': barcode,
            'logo': 'OK', 'flame': 'NG', **values}


def old_database(path):
    """Exact original v1 DDL, with an existing accepted observation and standard receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        for statement in DDL:
            db.execute(statement)
        db.execute('INSERT INTO standards VALUES(0,?,?,?)', ('', utc_now(), 'system'))
        db.execute('INSERT INTO standards VALUES(1,?,?,?)', ('001234-AbC', utc_now(), 'admin'))
        from competition.station_protocol import fingerprint
        digest = fingerprint({'barcode': '001234-AbC', 'revision': 0, 'actor': 'admin'})
        db.execute('INSERT INTO standard_updates VALUES(?,?,?)', ('a'*32, digest, 1))
        data=observation(msg_id='historical')
        db.execute('''INSERT INTO results(received_utc,project,station,worker_id,msg_id,fingerprint,
            barcode,barcode_status,logo,flame,standard_revision,standard_barcode,raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (utc_now(), 'packaging', 1, '001234', 'historical', fingerprint(data),
             data['barcode'], 'MATCH', 'OK', 'NG', 1, '001234-AbC', json.dumps(data)))
        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
        db.execute('PRAGMA user_version=1')
        db.commit()
    finally:
        db.close()


class CatalogJsonTests(unittest.TestCase):
    def test_batch_example(self):
        value=parse_catalog_json(json.dumps(EXAMPLE_CATALOG))
        self.assertFalse(value['legacy']); self.assertEqual(len(value['boxes']),3)

    def test_single_compatibility(self):
        value=parse_catalog_json(json.dumps(EXAMPLE_STANDARD))
        self.assertTrue(value['legacy']); self.assertEqual(value['boxes'][0]['id'],'LEGACY')
        self.assertEqual(parse_standard_json(json.dumps(EXAMPLE_STANDARD)), '001234-AbC')

    def test_bom_crlf_and_exact_text(self):
        boxes=deepcopy(EXAMPLE_CATALOG['boxes']); boxes[0]['standard_barcode']=' 0001-AbC '
        content='\ufeff'+json.dumps({'boxes':boxes},ensure_ascii=False,indent=2).replace('\n','\r\n')
        self.assertEqual(parse_catalog_json(content)['boxes'][0]['standard_barcode'],' 0001-AbC ')

    def test_no_printed_model_logo_or_employee_fields(self):
        for value in ({'boxes':[], 'model':'X'}, {'boxes':[], 'logo':'OK'}, {'boxes':[], 'worker_id':'1'}):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_catalog_json(json.dumps(value))

    def test_bad_top_levels(self):
        for value in ([], None, 123, True, '', {'boxes':None}, {'boxes':{}}, {'boxes':'a'}):
            with self.subTest(value=value),self.assertRaises(ValueError):parse_catalog_json(json.dumps(value))

    def test_required_item_fields(self):
        for value in (None, [], {}, {'id':'A'}, {'id':'A','name':'a','standard_barcode':'01','extra':1}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_boxes([value])

    def test_numbers_and_bools_are_not_barcodes(self):
        for code in (1, 0, True, None, [], {}):
            with self.subTest(code=code),self.assertRaises(ValueError):validate_boxes([{'id':'B','name':'b','standard_barcode':code}])

    def test_barcode_controls_and_unicode(self):
        for code in ('', 'a\n', 'a\0', 'a\x7f', '\ud800', 'a'*513):
            with self.subTest(code=repr(code)),self.assertRaises(ValueError):validate_boxes([{'id':'B','name':'b','standard_barcode':code}])
        code='机型-000-😀'; self.assertEqual(validate_boxes([{'id':'B','name':'b','standard_barcode':code}])[0]['standard_barcode'],code)

    def test_id_restrictions(self):
        for bid in ('', ' b', '../B', '中文', True, 5, 'a'*65):
            with self.subTest(bid=bid),self.assertRaises(ValueError):validate_boxes([{'id':bid,'name':'b','standard_barcode':'x'}])

    def test_name_restrictions(self):
        for name in ('', '  ', 'x'*101, None, '\n', '\ud800'):
            with self.subTest(name=repr(name)),self.assertRaises(ValueError):validate_boxes([{'id':'A','name':name,'standard_barcode':'x'}])

    def test_duplicate_ids_rejected_but_repeated_barcodes_allowed(self):
        a={'id':'A','name':'a','standard_barcode':'000'}
        with self.assertRaises(ValueError):validate_boxes([a,a])
        self.assertEqual(len(validate_boxes([a,{**a,'id':'B'}])),2)

    def test_duplicate_json_keys_rejected(self):
        for content in ('{"boxes":[],"boxes":[]}',
            '{"boxes":[{"id":"A","id":"B","name":"a","standard_barcode":"x"}]}',
            r'{"boxes":[{"id":"A","name":"a","standard_barcode":"x","\u0069d":"B"}]}'):
            with self.subTest(content=content),self.assertRaises(ValueError):parse_catalog_json(content)

    def test_comments_trailing_comma_nonfinite_invalid_unicode(self):
        for content in ('//comment\n{}','{"boxes":[],}','{"boxes":NaN}','{"boxes":Infinity}', '\ud800', '{'*1500):
            with self.subTest(content=repr(content)),self.assertRaises(ValueError):parse_catalog_json(content)

    def test_1000_items_and_size_limit(self):
        boxes=[{'id':f'B{n}','name':f'箱{n}','standard_barcode':f'000-{n}'} for n in range(1000)]
        self.assertEqual(len(parse_catalog_json(json.dumps({'boxes':boxes},ensure_ascii=False))['boxes']),1000)
        with self.assertRaises(ValueError):validate_boxes(boxes+[{'id':'EXTRA','name':'x','standard_barcode':'1'}])
        content=json.dumps({'boxes':[]})
        self.assertEqual(parse_catalog_json(content.ljust(MAX_CATALOG_BYTES))['boxes'],[])
        with self.assertRaises(ValueError):parse_catalog_json(content.ljust(MAX_CATALOG_BYTES+1))

    def test_utf8_byte_limit_not_character_count(self):
        with self.assertRaises(ValueError):parse_catalog_json('界'*90000)
        huge=[{'id':f'B{n}','name':'a','standard_barcode':'界'*512} for n in range(200)]
        with self.assertRaises(ValueError):validate_boxes(huge)

    def test_explicit_empty_list_and_legacy_empty(self):
        self.assertEqual(parse_catalog_json('{"boxes":[]}'), {'boxes':[], 'legacy':False})
        self.assertEqual(parse_catalog_json('{"standard_barcode":""}'), {'boxes':[], 'legacy':True})

    def test_example_file_matches_template(self):
        path=Path(__file__).resolve().parents[1]/'examples'/'packaging-standards.example.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),EXAMPLE_CATALOG)


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'station-results.sqlite3';self.store=StationStore(self.path)
        self.addCleanup(lambda:self.store.close())

    def load(self, boxes=None):
        return self.store.set_catalog(deepcopy(EXAMPLE_CATALOG['boxes']) if boxes is None else boxes,
                                      self.store.catalog()['revision'],mid(),'admin')

    def select(self, station, bid):
        return self.store.select_standard(station,bid,self.store.catalog()['revision'],mid(),'admin')

    def test_new_import_requires_selection(self):
        self.load();self.assertEqual(self.store.catalog()['selections'],{'1':None,'2':None})
        r,_=self.store.record(observation(),utc_now());self.assertEqual(r['barcode_status'],'UNSELECTED')

    def test_independent_slots_not_whitelist(self):
        self.load();self.select(1,'BOX01');self.select(2,'BOX02')
        a,_=self.store.record(observation(1),utc_now());b,_=self.store.record(observation(2),utc_now())
        self.assertEqual((a['barcode_status'],b['barcode_status']),('MATCH','MISMATCH'))
        self.assertEqual((b['logo'],b['flame']),('OK','NG'))
        self.assertEqual(b['standard_box_id'],'BOX02')

    def test_same_box_both_stations_allowed(self):
        self.load();self.select(1,'BOX01');self.select(2,'BOX01')
        for n in (1,2):self.assertEqual(self.store.record(observation(n),utc_now())[0]['barcode_status'],'MATCH')

    def test_worker_id_never_selects_box(self):
        self.load();self.select(1,'BOX01')
        r,_=self.store.record(observation(worker_id='BOX02'),utc_now());self.assertEqual(r['standard_box_id'],'BOX01')

    def test_count_record_has_no_box(self):
        self.load();self.select(1,'BOX01')
        r,_=self.store.record({'project':'screw','station':1,'msg_id':mid(),'worker_id':'000','screw_count':0},utc_now())
        self.assertEqual(r['screw_count'],0);self.assertIsNone(r['standard_box_id']);self.assertIsNone(r['barcode_status'])

    def test_exact_comparison_no_normalization(self):
        self.load();self.select(1,'BOX01')
        for code in ('1234-AbC','001234-abc','001234-AbC ',' 001234-AbC','prefix001234-AbC'):
            self.assertEqual(self.store.record(observation(barcode=code),utc_now())[0]['barcode_status'],'MISMATCH')

    def test_unread_and_unconfigured_are_not_fake_ng(self):
        self.assertEqual(self.store.record(observation(),utc_now())[0]['barcode_status'],'UNCONFIGURED')
        self.load()
        self.assertEqual(self.store.record(observation(barcode=''),utc_now())[0]['barcode_status'],'UNREAD')

    def test_unknown_id_or_bad_station_has_no_effect(self):
        self.load();before=self.store.catalog()
        for station,bid in ((3,'BOX01'),(True,'BOX01'),(1,'missing'),(1,''),(1,2)):
            with self.subTest(station=station,bid=bid),self.assertRaises(ValueError):self.select(station,bid)
        self.assertEqual(self.store.catalog(),before)

    def test_switch_does_not_rejudge_history(self):
        self.load();self.select(1,'BOX01');a,_=self.store.record(observation(),utc_now())
        self.select(1,'BOX02');b,_=self.store.record(observation(),utc_now())
        self.assertEqual(self.store.records('packaging')[1],a)
        self.assertEqual((a['barcode_status'],b['barcode_status']),('MATCH','MISMATCH'))

    def test_retry_after_switch_keeps_old_snapshot(self):
        self.load();self.select(1,'BOX01');msg=observation();a,_=self.store.record(msg,utc_now())
        self.select(1,'BOX02');b,duplicate=self.store.record(msg,utc_now())
        self.assertTrue(duplicate);self.assertEqual(a,b)
        with self.assertRaises(ProtocolError):self.store.record({**msg,'barcode':'different'},utc_now())

    def test_reimport_unchanged_id_barcode_retains_selection(self):
        self.load();self.select(1,'BOX01');self.select(2,'BOX02')
        boxes=deepcopy(EXAMPLE_CATALOG['boxes']);boxes[0]['name']='新名称';self.load(list(reversed(boxes)))
        self.assertEqual(self.store.catalog()['selections'],{'1':'BOX01','2':'BOX02'})

    def test_removed_and_changed_barcode_clear_selection(self):
        self.load();self.select(1,'BOX01');self.select(2,'BOX02')
        boxes=deepcopy(EXAMPLE_CATALOG['boxes']);boxes[0]['standard_barcode']='new';self.load([boxes[0],boxes[2]])
        self.assertEqual(self.store.catalog()['selections'],{'1':None,'2':None})

    def test_empty_catalog_clears_both_without_erasing_history(self):
        self.load();self.select(1,'BOX01');self.store.record(observation(),utc_now());self.load([])
        self.assertEqual(self.store.catalog()['boxes'],[]);self.assertEqual(len(self.store.records('packaging')),1)

    def test_explicit_deselect_affects_only_one_slot(self):
        self.load();self.select(1,'BOX01');self.select(2,'BOX02');self.select(1,None)
        self.assertEqual(self.store.catalog()['selections'],{'1':None,'2':'BOX02'})

    def test_failed_batch_is_atomic(self):
        self.load();before=self.store.catalog();boxes=deepcopy(EXAMPLE_CATALOG['boxes']);boxes[-1]['standard_barcode']=1
        with self.assertRaises(ValueError):self.load(boxes)
        self.assertEqual(self.store.catalog(),before)

    def test_request_dedup_and_cross_operation_conflict(self):
        rid=mid();self.store.set_catalog(EXAMPLE_CATALOG['boxes'],0,rid,'admin')
        self.assertTrue(self.store.set_catalog(EXAMPLE_CATALOG['boxes'],0,rid,'admin')['duplicate'])
        with self.assertRaises(ProtocolError):self.store.select_standard(1,'BOX01',1,rid,'admin')
        self.assertEqual(self.store.catalog()['revision'],1)

    def test_selection_request_dedup(self):
        self.load();rid=mid();a=self.store.select_standard(1,'BOX01',1,rid,'admin')
        self.assertTrue(self.store.select_standard(1,'BOX01',1,rid,'admin')['duplicate'])
        self.assertEqual(self.store.catalog()['revision'],a['revision'])

    def test_stale_import_and_select_rejected(self):
        self.load();self.select(1,'BOX01')
        for action in (lambda:self.store.set_catalog([],1,mid(),'admin'),lambda:self.store.select_standard(2,'BOX02',1,mid(),'admin')):
            with self.assertRaises(ProtocolError) as caught:action()
            self.assertEqual(caught.exception.code,'STATE_CHANGED')
        self.assertEqual(self.store.catalog()['selections'],{'1':'BOX01','2':None})

    def test_concurrent_writes_one_winner(self):
        self.load()
        def select(n):
            try:self.store.select_standard(n,'BOX01',1,mid(),'admin');return 'ok'
            except ProtocolError as e:return e.code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(select,(1,2)))
        self.assertCountEqual(results,['ok','STATE_CHANGED']);self.assertEqual(self.store.catalog()['revision'],2)

    def test_restart_persists_catalog_and_selections(self):
        self.load();self.select(2,'BOX03');before=self.store.catalog();self.store.close();self.store=StationStore(self.path)
        self.assertEqual(self.store.catalog(),before);self.assertIsNone(self.store.migration_backup)

    def test_backup_contains_catalog_and_snapshots(self):
        self.load();self.select(2,'BOX02');self.store.record(observation(2),utc_now());path=self.store.backup()
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
            self.assertEqual(json.loads(db.execute('SELECT selections_json FROM standards ORDER BY revision DESC LIMIT 1').fetchone()[0])['2'],'BOX02')
            self.assertEqual(db.execute('SELECT standard_box_name FROM results').fetchone()[0],'2号包装箱')
        finally:db.close()

    def test_v1_migration_preserves_standard_history_and_receipts(self):
        self.store.close();self.path=Path(self.temp.name)/'v1.sqlite3';old_database(self.path)
        self.store=StationStore(self.path);self.assertTrue(self.store.migration_backup.is_file())
        self.assertEqual(self.store.catalog()['selections'],{'1':'LEGACY','2':'LEGACY'})
        self.assertEqual(self.store.catalog()['boxes'][0]['standard_barcode'],'001234-AbC')
        self.assertEqual(self.store.records('packaging')[0]['barcode_status'],'MATCH')
        self.assertIsNone(self.store.records('packaging')[0]['standard_box_id'])
        self.assertTrue(self.store.set_standard('001234-AbC',0,'a'*32,'admin')['duplicate'])
        before=sqlite3.connect(self.store.migration_backup)
        try:self.assertEqual(before.execute('PRAGMA user_version').fetchone()[0],1)
        finally:before.close()

    def test_migration_backup_failure_leaves_schema_at_v1(self):
        path=Path(self.temp.name)/'v1.sqlite3';old_database(path)
        with patch.object(StationStore,'backup',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):StationStore(path)
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],1)
        finally:db.close()
        repaired=StationStore(path);repaired.close()

    def test_migration_ddl_failure_rolls_back(self):
        path=Path(self.temp.name)/'broken.sqlite3';old_database(path)
        db=sqlite3.connect(path);db.execute('ALTER TABLE results ADD COLUMN standard_box_id TEXT');db.commit();db.close()
        with self.assertRaises(sqlite3.Error):StationStore(path)
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertNotIn('boxes_json',{r[1] for r in db.execute('PRAGMA table_info(standards)')})
        finally:db.close()

    def test_legacy_single_import_applies_to_both(self):
        self.load();self.store.set_standard('000-Old',1,mid(),'admin')
        self.assertEqual(self.store.catalog()['selections'],{'1':'LEGACY','2':'LEGACY'})
        self.assertEqual(len(self.store.catalog()['boxes']),1)
        self.assertEqual(self.store.standard()['barcode'],'000-Old')

    def test_public_projection_excludes_target(self):
        self.load();self.select(1,'BOX01');r,_=self.store.record(observation(),utc_now())
        for key in ('standard_box_id','standard_box_name','standard_barcode','standard_revision'):
            self.assertNotIn(key,self.store.public(r))
        self.assertEqual(self.store.admin_row(r)['standard_box_id'],'BOX01')


class CatalogApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password='Test-only-catalog-password';cls.credentials=credential_record('admin',cls.password)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);creds=self.root/'admin.json';creds.write_text(json.dumps(self.credentials))
        self.origin='http://localhost:9080'
        config=WebConfig(data_dir=self.root/'data',credentials=creds,public_url=self.origin,tcp_port=0,auto_backup=False)
        self.app=create_app(config);self.client=TestClient(self.app,base_url=self.origin);self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        answer=self.client.post('/api/auth/login',json={'username':'admin','password':self.password},headers={'Origin':self.origin})
        self.assertEqual(answer.status_code,200);self.headers={'Origin':self.origin,'X-CSRF-Token':answer.json()['csrf']}
        self.store=self.app.state.runtime.store

    def post(self,path,payload,request_id=None):
        return self.client.post('/api/admin/standard-barcode/'+path,json=payload,
                                headers={**self.headers,'X-Request-ID':request_id or mid()})

    def load(self,data=EXAMPLE_CATALOG,revision=0):
        return self.post('import',{'content':json.dumps(data,ensure_ascii=False),'revision':revision})

    def choose(self,n,bid,revision):
        return self.post('select',{'station':n,'box_id':bid,'revision':revision})

    def test_import_and_two_independent_selections(self):
        self.assertEqual(self.load().status_code,200)
        self.assertEqual(self.choose(1,'BOX01',1).status_code,200)
        self.assertEqual(self.choose(2,'BOX03',2).status_code,200)
        state=self.client.get('/api/admin/state?project=packaging').json()
        self.assertEqual(state['catalog']['selections'],{'1':'BOX01','2':'BOX03'})

    def test_large_real_json_import_exceeds_old_16k_limit(self):
        data={'boxes':[{'id':f'B{n}','name':f'包装箱{n}','standard_barcode':f'000-{n}'} for n in range(1000)]}
        self.assertGreater(len(json.dumps(data).encode()),16384)
        self.assertEqual(self.load(data).status_code,200);self.assertEqual(len(self.store.catalog()['boxes']),1000)

    def test_size_budgets_for_import_and_other_endpoints(self):
        self.assertEqual(self.post('import',{'content':' '*(MAX_CATALOG_BYTES+1),'revision':0}).status_code,400)
        self.assertEqual(self.post('import',{'content':' '*IMPORT_BODY_BYTES,'revision':0}).status_code,413)
        self.assertEqual(self.post('select',{'padding':' '*20000}).status_code,413)
        self.assertEqual(self.client.post('/api/auth/login',json={'padding':' '*20000},headers=self.headers).status_code,413)

    def test_conflict_returns_409_and_does_not_change_standard(self):
        self.load();self.choose(1,'BOX01',1)
        self.assertEqual(self.choose(2,'BOX02',1).status_code,409)
        self.assertEqual(self.load({'boxes':[]},1).status_code,409)
        self.assertEqual(self.store.catalog()['selections']['1'],'BOX01')

    def test_auth_and_csrf_for_import_and_selection(self):
        for endpoint,payload in [('select',{'station':1,'box_id':None,'revision':0}),('import',{'content':'{"boxes":[]}','revision':0})]:
            self.assertEqual(self.client.post('/api/admin/standard-barcode/'+endpoint,json=payload,headers={'Origin':self.origin}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.choose(1,None,0).status_code,401)
        self.assertEqual(self.load().status_code,401)
        self.assertEqual(self.client.get('/api/admin/standard-barcode/template?format=catalog').status_code,401)

    def test_invalid_fields_no_partial_effect(self):
        for content in ('{"boxes":[],"boxes":[]}','{"boxes":[{"id":"A","name":"a","standard_barcode":12}]}'):
            self.assertEqual(self.post('import',{'content':content,'revision':0}).status_code,400)
        self.assertEqual(self.post('select',{'station':1,'box_id':None,'revision':0,'worker_id':'1'}).status_code,400)
        self.assertEqual(self.choose(True,'A',0).status_code,400)
        self.assertEqual(self.store.catalog()['revision'],0)

    def test_template_is_fixed_example_not_current_answers(self):
        self.load({'standard_barcode':'PRIVATE-ANSWER'})
        r=self.client.get('/api/admin/standard-barcode/template?format=catalog')
        self.assertEqual(r.json(),EXAMPLE_CATALOG);self.assertNotIn('PRIVATE-ANSWER',r.text)
        self.assertIn('attachment;',r.headers['content-disposition'])
        self.assertEqual(self.client.get('/api/admin/standard-barcode/template').json(),EXAMPLE_STANDARD)

    def test_legacy_import_is_compatible(self):
        self.assertEqual(self.load(EXAMPLE_STANDARD).status_code,200)
        self.assertEqual(self.store.standard()['barcode'],'001234-AbC')
        self.assertEqual(self.store.catalog()['selections'],{'1':'LEGACY','2':'LEGACY'})

    def test_jsonl_csv_keep_selected_snapshot(self):
        self.load();self.choose(1,'BOX01',1)
        self.store.record(observation(),utc_now());self.choose(1,'BOX02',2)
        data=self.client.get('/api/admin/export?project=packaging&format=jsonl').text
        r=json.loads(data);self.assertEqual(r['standard_box_id'],'BOX01');self.assertEqual(r['standard_barcode'],'001234-AbC')
        text=self.client.get('/api/admin/export?project=packaging&format=csv').text
        self.assertIn('standard_box_name',text);self.assertIn('1号包装箱',text)

    def test_public_and_tcp_never_receive_catalog(self):
        self.load();self.choose(1,'BOX01',1)
        self.store.record(observation(),utc_now())
        public=self.client.get('/api/display?project=packaging').json()
        self.assertNotIn('catalog',public);self.assertNotIn('standard',public)
        self.assertNotIn('standard_box_name',public['records'][0]);self.assertNotIn('standard_box_id',public['stations'][0]['latest'])

    def test_four_tcp_connections_survive_import_and_selection(self):
        peers=[]
        try:
            for project in ('screw','packaging'):
                for n in (1,2):
                    sock=socket.create_connection(self.app.state.runtime.tcp.address,timeout=3);stream=sock.makefile('rb');peers.append((sock,stream,project,n))
                    sock.sendall(encode({'v':2,'type':'hello','msg_id':mid(),'project':project,'station':n}))
                    self.assertEqual(decode(stream.readline()[:-1])['type'],'hello_ok')
            self.load();self.choose(1,'BOX01',1);self.choose(2,'BOX02',2)
            for sock,stream,project,n in peers:
                data=observation(n) if project=='packaging' else {'v':2,'type':'result','project':'screw','station':n,'msg_id':mid(),'worker_id':'000001','screw_count':4}
                sock.sendall(encode(data));ack=decode(stream.readline()[:-1])
                self.assertEqual(ack['type'],'result_ack');self.assertEqual(set(ack),{'v','type','reply_to','record_id','recorded','duplicate'})
                sock.sendall(encode(data));self.assertTrue(decode(stream.readline()[:-1])['duplicate'])
            self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'MATCH')
            self.assertEqual(self.store.latest('packaging',2)['barcode_status'],'MISMATCH')
            self.assertEqual(self.store.latest('packaging',2)['flame'],'NG')
            self.assertEqual(self.store.latest('screw',1)['screw_count'],4)
            for project in ('screw','packaging'):self.assertTrue(all(s['connected'] for s in self.app.state.runtime.engine.snapshot(project)['stations']))
        finally:
            for sock,stream,*_ in peers:stream.close();sock.close()


if __name__=='__main__':
    unittest.main()
