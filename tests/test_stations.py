"""TCP v2 / two-project monitor regression tests; no real samples or credentials."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from competition import station_protocol as p
from competition.station_store import StationStore, APPLICATION_ID
from competition.station_runtime import StationRuntime
from competition.web_config import WebConfig
from competition.web_auth import credential_record
from station_client import send_result


def result(project='screw', station=1, **kwargs):
    data = {'v': 2, 'type': 'result', 'msg_id': uuid.uuid4().hex, 'project': project,
            'station': station, 'worker_id': '001234'}
    if project == 'screw':
        data.update(screw_count=4)
    else:
        data.update(barcode='001234-AbC', logo='OK', flame='NG')
    data.update(kwargs)
    return data


class Client:
    def __init__(self, address):
        self.sock = socket.create_connection(address, timeout=3)
        self.sock.settimeout(3)
        self.stream = self.sock.makefile('rb')

    def read(self):
        raw = self.stream.readline()
        if not raw:
            raise ConnectionError('closed')
        return p.decode(raw[:-1])

    def send(self, data):
        self.sock.sendall(p.encode(data)); return self.read()

    def close(self):
        self.stream.close(); self.sock.close()


class ProtocolTests(unittest.TestCase):
    def test_valid_screw_and_packaging(self):
        for proj in p.PROJECTS:
            self.assertEqual(p.validate(result(proj))['project'], proj)

    def test_no_legacy_verdict_or_round_fields(self):
        for fields in ({'v': 1}, {'verdict': 'NG'}, {'round_id': 'x'}, {'session_id': 'x'}):
            with self.subTest(fields=fields), self.assertRaises(p.ProtocolError):
                p.validate(result(**fields))

    def test_count_integer_only(self):
        for count in [True, False, -1, 4.0, '4', None, 2**32]:
            with self.subTest(count=count), self.assertRaises(p.ProtocolError):
                p.validate(result(screw_count=count))

    def test_zero_is_valid_observation(self):
        self.assertEqual(p.validate(result(screw_count=0))['screw_count'], 0)

    def test_worker_id_is_text_and_keeps_zeros(self):
        self.assertEqual(p.validate(result(worker_id='000012'))['worker_id'], '000012')
        for worker in [12, '', ' A ', '\n', 'A'*65]:
            with self.subTest(worker=worker), self.assertRaises(p.ProtocolError): p.validate(result(worker_id=worker))

    def test_only_two_stations_per_project(self):
        for proj, station in [('screw', 3), ('packaging', 0), ('x', 1), ('screw', True), ('screw', '1')]:
            with self.subTest(project=proj,station=station), self.assertRaises(p.ProtocolError): p.validate(result(proj,station))

    def test_logo_and_flame_are_both_required(self):
        for key in ('logo', 'flame'):
            data=result('packaging'); data.pop(key)
            with self.assertRaises(p.ProtocolError): p.validate(data)

    def test_no_logo_or_flame_subcategories(self):
        for key in ('logo', 'flame'):
            for value in ('ok', 'ng', '破损', False, {}, None):
                with self.subTest(key=key,value=value), self.assertRaises(p.ProtocolError): p.validate(result('packaging', **{key:value}))

    def test_empty_barcode_is_explicit_unread(self):
        self.assertEqual(p.validate(result('packaging', barcode=''))['barcode'],'')

    def test_barcode_is_not_trimmed_or_coerced(self):
        code=' 001abc '
        self.assertEqual(p.validate(result('packaging',barcode=code))['barcode'],code)
        for code in (123, None, 'A\nB', 'A'*513):
            with self.subTest(code=code), self.assertRaises(p.ProtocolError): p.validate(result('packaging',barcode=code))

    def test_wire_bad_json_duplicate_keys_and_nonfinite(self):
        for raw in (b'{"v":2,"v":2}', b'{"x":NaN}', b'[]', b'\xff', b'\xef\xbb\xbf{}'):
            with self.subTest(raw=raw), self.assertRaises(p.ProtocolError): p.decode(raw)

    def test_bad_unicode_or_unknown_fields(self):
        for values in ({'worker_id':'\ud800'}, {'logo_reason':'stain'}, {'model':'abc'}):
            with self.assertRaises(p.ProtocolError): p.validate(result('packaging',**values))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=StationStore(self.root/'station-results.sqlite3')

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def configure(self, barcode):
        return self.store.set_standard(barcode,self.store.standard()['revision'],uuid.uuid4().hex,'tester')

    def record(self, message):
        return self.store.record(p.validate(message),'2026-09-15T10:00:00.000+00:00')

    def test_three_packaging_fields_are_independent(self):
        self.configure('001234-AbC');row,dup=self.record(result('packaging'))
        self.assertEqual((row['barcode_status'],row['logo'],row['flame']),('MATCH','OK','NG'))
        self.assertFalse(dup)
        self.assertNotIn('score',row)

    def test_exact_full_comparison(self):
        self.configure('001234-AbC')
        for code in ['1234-AbC','001234-abc','001234-AbC ',' 001234-AbC','001234-AbC-extra']:
            row,_=self.record(result('packaging',barcode=code));self.assertEqual(row['barcode_status'],'MISMATCH')

    def test_no_standard_is_not_mismatch(self):
        row,_=self.record(result('packaging'));self.assertEqual(row['barcode_status'],'UNCONFIGURED')

    def test_empty_read_is_not_replaced(self):
        row,_=self.record(result('packaging',barcode=''));self.assertEqual(row['barcode'],'');self.assertEqual(row['barcode_status'],'UNREAD')

    def test_only_count_for_screw(self):
        row,_=self.record(result(screw_count=0))
        self.assertEqual(row['screw_count'],0)
        for key in ['barcode','logo','flame','barcode_status','standard_barcode']: self.assertIsNone(row[key])

    def test_history_keeps_original_standard(self):
        self.configure('001234-AbC');one,_=self.record(result('packaging'))
        self.configure('OTHER');two,_=self.record(result('packaging'))
        self.assertEqual((one['barcode_status'],two['barcode_status']),('MATCH','MISMATCH'))
        rows=self.store.records('packaging');self.assertEqual(rows[1]['standard_barcode'],'001234-AbC')

    def test_retry_after_standard_change_keeps_old_receipt(self):
        self.configure('001234-AbC');data=result('packaging');a,_=self.record(data)
        self.configure('OTHER');b,dup=self.record(data)
        self.assertTrue(dup);self.assertEqual(a['id'],b['id']);self.assertEqual(b['barcode_status'],'MATCH')
        self.assertEqual(len(self.store.records('packaging')),1)

    def test_same_id_changed_content_rejected(self):
        data=result();self.record(data)
        with self.assertRaises(p.ProtocolError):self.record({**data,'screw_count':3})
        self.assertEqual(self.store.records('screw')[0]['screw_count'],4)

    def test_same_count_with_new_id_is_a_new_detection(self):
        self.record(result());self.record(result());self.assertEqual(len(self.store.records('screw')),2)

    def test_same_id_different_station_is_independent(self):
        data=result();self.record(data);self.record({**data,'station':2})
        self.assertEqual(len(self.store.records('screw')),2)

    def test_settings_revision_and_idempotence(self):
        mid=uuid.uuid4().hex
        self.store.set_standard('0001',0,mid,'admin')
        retry=self.store.set_standard('0001',0,mid,'admin');self.assertTrue(retry['duplicate'])
        with self.assertRaises(p.ProtocolError):self.store.set_standard('0002',0,uuid.uuid4().hex,'admin')
        with self.assertRaises(p.ProtocolError):self.store.set_standard('0002',0,mid,'admin')
        self.assertEqual(self.store.standard()['barcode'],'0001')

    def test_standard_and_results_survive_restart(self):
        self.configure('0001');self.record(result());self.store.close()
        self.store=StationStore(self.root/'station-results.sqlite3')
        self.assertEqual(self.store.standard()['barcode'],'0001');self.assertEqual(len(self.store.records('screw')),1)

    def test_public_projection_never_contains_standard(self):
        self.configure('secret-standard');row,_=self.record(result('packaging'))
        public=self.store.public(row)
        self.assertNotIn('standard_barcode',public);self.assertNotIn('standard_revision',public);self.assertNotIn('raw_json',public)

    def test_durable_backup_is_standalone(self):
        self.configure('0001');self.record(result());path=self.store.backup()
        db=sqlite3.connect(path)
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM results').fetchone()[0],1);self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        finally:db.close()
        self.assertFalse(list(path.parent.glob('*.partial')))

    def test_backup_failure_does_not_delete_live_data(self):
        self.record(result())
        with patch('competition.station_store.os.fsync',side_effect=OSError('test fault')):
            with self.assertRaises(OSError):self.store.backup()
        self.assertEqual(len(self.store.records('screw')),1);self.assertFalse(list((self.root/'station-backups').glob('*.partial')))

    def test_process_owner_lock(self):
        with self.assertRaises(RuntimeError):StationStore(self.store.path)

    def test_rejects_legacy_or_foreign_database(self):
        path=self.root/'foreign.sqlite3';db=sqlite3.connect(path);db.execute('CREATE TABLE other(x)');db.commit();db.close()
        with self.assertRaises(ValueError):StationStore(path)
        db=sqlite3.connect(path)
        try:self.assertEqual(db.execute('PRAGMA application_id').fetchone()[0],0)
        finally:db.close()

    def test_future_version_rejected(self):
        path=self.root/'future.sqlite3';db=sqlite3.connect(path);db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute('PRAGMA user_version=999');db.close()
        with self.assertRaises(ValueError):StationStore(path)

    def test_observations_are_immutable(self):
        self.record(result())
        for sql in ('UPDATE results SET screw_count=5','DELETE FROM results'):
            with self.assertRaises(sqlite3.IntegrityError),self.store.transaction() as db:db.execute(sql)

    def test_cursor_pagination(self):
        for _ in range(7):self.record(result())
        first=self.store.records('screw',limit=3);second=self.store.records('screw',before=first[-1]['id'],limit=3)
        self.assertFalse({r['id'] for r in first}&{r['id'] for r in second})

    def test_no_auto_ng_after_invalid_message(self):
        before=self.store.records('screw')
        with self.assertRaises(p.ProtocolError):self.record(result(screw_count=True))
        self.assertEqual(self.store.records('screw'),before)


class TCPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.config=WebConfig(data_dir=Path(self.temp.name),tcp_port=0,auto_backup=False)
        self.runtime=StationRuntime(self.config);self.clients=[]

    def tearDown(self):
        for c in self.clients:c.close()
        self.runtime.close();self.temp.cleanup()

    def client(self):
        c=Client(self.runtime.tcp.address);self.clients.append(c);return c

    def test_four_simultaneous_stations_without_rounds(self):
        pairs=[(proj,n) for proj in p.PROJECTS for n in (1,2)];clients=[self.client() for _ in pairs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies=list(pool.map(lambda pair:pair[0].send(result(*pair[1])),zip(clients,pairs)))
        self.assertTrue(all(r['recorded'] for r in replies))
        for proj in p.PROJECTS:
            state=self.runtime.engine.snapshot(proj);self.assertTrue(all(s['connected'] for s in state['stations']))
            self.assertEqual(len(state['records']),2)

    def test_handshake_is_optional_first_result_binds_station(self):
        c=self.client();self.assertTrue(c.send(result())['recorded'])

    def test_hello_and_ping_does_not_create_results(self):
        c=self.client();hello={'v':2,'type':'hello','msg_id':'h','project':'packaging','station':2}
        reply=c.send(hello);self.assertEqual(reply['type'],'hello_ok');self.assertNotIn('standard',reply)
        self.assertEqual(c.send({'v':2,'type':'ping','msg_id':'p'})['type'],'pong')
        self.assertEqual(self.runtime.store.records('packaging'),[])

    def test_busy_station_does_not_replace_owner(self):
        a,b=self.client(),self.client();a.send(result());reply=b.send(result())
        self.assertEqual(reply['code'],'STATION_BUSY');self.assertTrue(a.send(result())['recorded'])

    def test_one_connection_cannot_switch_station(self):
        c=self.client();c.send(result());self.assertEqual(c.send(result(station=2))['code'],'STATION_MISMATCH')
        self.assertIsNone(self.runtime.store.latest('screw',2))

    def test_worker_change_is_metadata_not_new_session(self):
        c=self.client();c.send(result(worker_id='0001'));c.send(result(worker_id='D70516',screw_count=3))
        self.assertEqual(self.runtime.engine.snapshot('screw')['stations'][0]['worker_id'],'D70516')
        self.assertEqual([r['worker_id'] for r in self.runtime.store.records('screw')],['D70516','0001'])

    def test_duplicate_does_not_change_current_operator(self):
        c=self.client();old=result(worker_id='old');c.send(old);c.send(result(worker_id='new'));c.send(old)
        self.assertEqual(self.runtime.engine.snapshot('screw')['stations'][0]['worker_id'],'new')

    def test_chunked_and_coalesced_frames(self):
        c=self.client();a=result();wire=p.encode(a);c.sock.sendall(wire[:9]);time.sleep(.01);c.sock.sendall(wire[9:]);self.assertTrue(c.read()['recorded'])
        c.sock.sendall(p.encode(result())+p.encode(result()));self.assertTrue(c.read()['recorded']);self.assertTrue(c.read()['recorded'])
        self.assertEqual(len(self.runtime.store.records('screw')),3)

    def test_crlf_supported(self):
        c=self.client();c.sock.sendall(p.encode(result())[:-1]+b'\r\n');self.assertTrue(c.read()['recorded'])

    def test_conflict_does_not_overwrite_result(self):
        c=self.client();data=result();c.send(data)
        self.assertTrue(c.send(data)['duplicate'])
        self.assertEqual(c.send({**data,'screw_count':3})['code'],'MSG_ID_CONFLICT')
        self.assertEqual(len(self.runtime.store.records('screw')),1)

    def test_ack_follows_commit(self):
        c=self.client();reply=c.send(result());db=sqlite3.connect(self.runtime.store.path)
        try:self.assertEqual(db.execute('SELECT id FROM results').fetchone()[0],reply['record_id'])
        finally:db.close()

    def test_no_standard_or_matching_feedback_in_tcp(self):
        self.runtime.store.set_standard('001234-AbC',0,uuid.uuid4().hex,'admin')
        reply=self.client().send(result('packaging'));self.assertEqual(set(reply),{'v','reply_to','type','recorded','duplicate','record_id'})

    def test_bad_fields_do_not_consume_result_id(self):
        c=self.client();bad=result('packaging',flame='UNKNOWN');self.assertEqual(c.send(bad)['code'],'INVALID_VERDICT')
        self.assertTrue(c.send({**bad,'flame':'NG'})['recorded'])

    def test_disconnect_retains_history_but_not_live_state(self):
        c=self.client();c.send(result());c.close()
        for _ in range(100):
            state=self.runtime.engine.snapshot('screw')['stations'][0]
            if not state['connected']:break
            time.sleep(.01)
        self.assertFalse(state['connected']);self.assertIsNone(state['worker_id']);self.assertEqual(state['latest']['screw_count'],4)

    def test_disk_failure_closes_and_no_ack(self):
        c=self.client()
        with patch.object(self.runtime.store,'record',side_effect=sqlite3.OperationalError('test disk fault')):
            with self.assertLogs('competition.station_runtime',level='ERROR'):
                c.sock.sendall(p.encode(result()));self.assertEqual(c.stream.readline(),b'')
        self.assertFalse(self.runtime.healthy);self.assertEqual(self.runtime.store.records('screw'),[])

    def test_reject_oversize_partial(self):
        c=self.client();c.sock.sendall(b'x'*18000);self.assertEqual(c.stream.readline(),b'')
        self.assertEqual(self.runtime.store.records('screw'),[])

    def test_invalid_json_not_stored_as_ng(self):
        c=self.client();c.sock.sendall(b'{bad}\n');self.assertEqual(c.read()['code'],'INVALID_JSON')
        self.assertEqual(self.runtime.store.records('packaging'),[])

    def test_sample_client_uses_real_tcp(self):
        host,port=self.runtime.tcp.address;self.assertTrue(send_result(host,port,result('packaging'))['recorded'])

    def test_restart_keeps_dedup_and_results(self):
        data=result();c=self.client();c.send(data);c.close();self.runtime.close()
        self.runtime=StationRuntime(self.config)
        self.assertFalse(self.runtime.engine.snapshot('screw')['stations'][0]['connected'])
        c=self.client();self.assertTrue(c.send(data)['duplicate'])
        self.assertEqual(len(self.runtime.store.records('screw')),1)


try:
    from fastapi.testclient import TestClient
    from competition.station_webapp import create_app
    WEB=True
except ImportError:
    WEB=False


@unittest.skipUnless(WEB,'optional Web dependencies required')
class StationWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password='Local-test-only-123456'
        cls.credential=credential_record('admin',cls.password)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        credentials=root/'admin.json';credentials.write_text(json.dumps(self.credential))
        self.origin='http://localhost:9080';self.config=WebConfig(data_dir=root/'data',credentials=credentials,tcp_port=0,auto_backup=False)
        self.app=create_app(self.config);self.client=TestClient(self.app,base_url=self.origin);self.client.__enter__();self.csrf=''

    def tearDown(self):
        self.client.__exit__(None,None,None);self.temp.cleanup()

    def login(self):
        reply=self.client.post('/api/auth/login',json={'username':'admin','password':self.password},headers={'Origin':self.origin})
        self.assertEqual(reply.status_code,200,reply.text);self.csrf=reply.json()['csrf'];return reply

    def post(self,url,data):
        return self.client.post(url,json=data,headers={'Origin':self.origin,'X-CSRF-Token':self.csrf,'X-Request-ID':uuid.uuid4().hex})

    def test_private_endpoints_require_login(self):
        for url in ['/api/admin/state','/api/admin/records?project=screw','/api/admin/export?project=screw']:
            self.assertEqual(self.client.get(url).status_code,401)

    def test_login_cookie_and_logout(self):
        self.assertIn('HttpOnly',self.login().headers['set-cookie'])
        self.assertEqual(self.post('/api/auth/logout',{}).status_code,200)
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)

    def test_csrf_and_origin_enforced(self):
        self.login();data={'barcode':'secret','revision':0}
        self.assertEqual(self.client.post('/api/admin/standard-barcode',json=data,headers={'Origin':self.origin}).status_code,403)
        self.assertEqual(self.client.post('/api/admin/standard-barcode',json=data,headers={'Origin':'http://wrong','X-CSRF-Token':self.csrf}).status_code,403)

    def test_standard_exact_text_and_conflict(self):
        self.login();self.assertEqual(self.post('/api/admin/standard-barcode',{'barcode':' 000AbC ','revision':0}).status_code,200)
        self.assertEqual(self.client.get('/api/admin/state?project=packaging').json()['standard']['barcode'],' 000AbC ')
        self.assertEqual(self.post('/api/admin/standard-barcode',{'barcode':'OTHER','revision':0}).status_code,409)

    def test_http_state_driven_by_real_tcp(self):
        self.login();host,port=self.app.state.runtime.tcp.address;send_result(host,port,result('packaging'))
        state=self.client.get('/api/admin/state?project=packaging').json()
        self.assertEqual(state['records'][0]['flame'],'NG');self.assertEqual(state['records'][0]['barcode_status'],'UNCONFIGURED')
        self.assertEqual(len(state['stations']),2)

    def test_public_never_leaks_standard_or_private_logs(self):
        self.login();self.post('/api/admin/standard-barcode',{'barcode':'SERVER-SECRET','revision':0})
        host,port=self.app.state.runtime.tcp.address;send_result(host,port,result('packaging'))
        data=self.client.get('/api/display?project=packaging').json()
        self.assertNotIn('standard',data);self.assertNotIn('logs',data);self.assertNotIn('SERVER-SECRET',json.dumps(data))
        self.assertTrue(all(s['address'] is None for s in data['stations']))

    def test_export_jsonl_preserves_leading_zeroes_and_raw_payload(self):
        self.login();host,port=self.app.state.runtime.tcp.address;send_result(host,port,result('packaging'))
        reply=self.client.get('/api/admin/export?project=packaging&format=jsonl')
        self.assertEqual(reply.status_code,200);row=json.loads(reply.text)
        self.assertEqual(row['barcode'],'001234-AbC');self.assertEqual(row['worker_id'],'001234')

    def test_csv_export_defuses_formulas(self):
        self.login();host,port=self.app.state.runtime.tcp.address;send_result(host,port,result('packaging',barcode='=1+1'))
        reply=self.client.get('/api/admin/export?project=packaging')
        self.assertIn("'=1+1",reply.text)

    def test_backup_download_authenticated(self):
        self.login();reply=self.post('/api/admin/backup',{});self.assertEqual(reply.status_code,200,reply.text)
        url=reply.json()['download_url'];self.assertEqual(self.client.get(url).content[:16],b'SQLite format 3\x00')
        self.post('/api/auth/logout',{});self.assertEqual(self.client.get(url).status_code,401)

    def test_bad_filters_host_and_routes(self):
        self.login()
        self.assertEqual(self.client.get('/api/admin/state?project=other').status_code,400)
        self.assertEqual(self.client.get('/api/admin/records?project=screw&station=9').status_code,400)
        self.assertEqual(self.client.get('/api/admin/state',headers={'Host':'evil'}).status_code,400)
        self.assertEqual(self.post('/api/admin/commands/start_round',{}).status_code,404)

    def test_default_assets_have_new_fields_not_old_commands(self):
        page=self.client.get('/admin/').text;script=self.client.get('/monitor/app.js').text
        self.assertIn('standard-barcode',page);self.assertIn('火焰标识',script)
        self.assertNotIn('innerHTML',script);self.assertNotIn('start_round',script)
        self.assertEqual(self.client.get('/healthz').json(),{'ok':True})

    def test_ui_scoped_ng_filter_not_scoring(self):
        script=self.client.get('/monitor/app.js').text
        self.assertIn("r.flame==='NG'",script);self.assertNotIn('screw_count<',script)


if __name__=='__main__':
    unittest.main()
