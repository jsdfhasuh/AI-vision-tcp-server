"""Plain-text framing, real TCP, persistence and HTTP regression tests.

All data and credentials are synthetic and temporary. No fixture replaces TCP
or HTTP in integration tests. Existing JSON v2 remains a compatibility path.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from competition import station_protocol as p
from competition.station_text import FrameDecoder, decode_text, encode_result, encode_reply
from competition.station_runtime import StationRuntime
from competition.station_store import StationStore
from competition.web_config import WebConfig
from station_client import send_text_result, send_result


def message(project='screw', station=1, **values):
    m = dict(v=2, type='result', msg_id=uuid.uuid4().hex, project=project, station=station, worker_id='001234')
    m.update(dict(screw_count=4) if project == 'screw' else dict(barcode='001-AbC', logo='OK', flame='NG'))
    m.update(values)
    return m


class TextClient:
    def __init__(self, address):
        self.sock = socket.create_connection(address, timeout=3)
        self.buffer = bytearray()

    def read(self):
        while b',end' not in self.buffer:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError('closed before full text response')
            self.buffer.extend(chunk)
        length = self.buffer.index(b',end') + 4
        result = bytes(self.buffer[:length]); del self.buffer[:length]
        return result

    def send(self, raw):
        self.sock.sendall(raw.encode('utf-8') if isinstance(raw, str) else raw)
        return self.read()

    def close(self):
        self.sock.close()


class TextCodecTests(unittest.TestCase):
    def test_screw_count_and_worker(self):
        m = decode_text(b'screw,1,000012,4,end')
        self.assertEqual((m['project'], m['station'], m['worker_id'], m['screw_count']), ('screw', 1, '000012', 4))
        self.assertNotIn('verdict', m)

    def test_packaging_exact_text(self):
        for code in ['001-AbC', ' 001-AbC ', 'end', 'friend', '001end002', '型号end😀', '']:
            with self.subTest(code=code):
                m = decode_text(f'packaging,2,D70516,{code},OK,NG,end'.encode())
                self.assertEqual((m['barcode'], m['logo'], m['flame']), (code, 'OK', 'NG'))

    def test_identical_text_has_fresh_internal_ids(self):
        raw = b'screw,1,D70516,4,end'
        self.assertNotEqual(decode_text(raw)['msg_id'], decode_text(raw)['msg_id'])

    def test_control_frames(self):
        self.assertEqual(decode_text(b'hello,packaging,2,end')['type'], 'hello')
        self.assertEqual(decode_text(b'ping,end')['type'], 'ping')

    def test_zero_and_integer_limit(self):
        for count in ('0', '004', '2147483647'):
            self.assertEqual(decode_text(f'screw,1,D,{count},end'.encode())['screw_count'], int(count))

    def test_invalid_counts(self):
        for count in ('', '-1', '+1', '4.0', '4e0', 'true', ' 4', '٤', '2147483648', '1'*100):
            with self.subTest(count=count), self.assertRaises(p.ProtocolError):
                decode_text(f'screw,1,D,{count},end'.encode())

    def test_only_stations_one_two(self):
        for n in ('0', '3', '01', ' 1', 'true'):
            with self.subTest(n=n), self.assertRaises(p.ProtocolError):
                decode_text(f'screw,{n},D,4,end'.encode())

    def test_invalid_logo_flame_or_missing_fields(self):
        for raw in (b'packaging,1,D,001,ok,NG,end', b'packaging,1,D,001,OK,false,end',
                    b'packaging,1,D,001,OK,end', b'packaging,1,D,001,OK,NG,reason,end'):
            with self.subTest(raw=raw), self.assertRaises(p.ProtocolError): decode_text(raw)

    def test_invalid_fields_encoding_and_delimiters(self):
        for raw in (b'screw,1,,4,end', b'screw,1, D,4,end', b'screw,1,D\nX,4,end',
                    b'screw,1,D\xff,4,end', b'screw,1,D,4,END', b'screw,1,D,4,end\n',
                    b'\xef\xbb\xbfscrew,1,D,4,end', b'other,1,D,4,end'):
            with self.subTest(raw=raw), self.assertRaises(p.ProtocolError): decode_text(raw)

    def test_field_lengths(self):
        for value in (message(worker_id='D'*65), message('packaging', barcode='x'*513)):
            with self.assertRaises(p.ProtocolError): encode_result(value)

    def test_encoder_and_reply_no_json_no_newline(self):
        self.assertEqual(encode_result(message()), b'screw,1,001234,4,end')
        self.assertEqual(encode_result(message('packaging')), b'packaging,1,001234,001-AbC,OK,NG,end')
        self.assertEqual(encode_reply({'type':'result_ack', 'recorded':True, 'record_id':12}), b'ACK,end')
        self.assertEqual(encode_reply({'type':'hello_ok'}), b'HELLO,end')
        self.assertEqual(encode_reply({'type':'pong'}), b'PONG,end')
        self.assertEqual(encode_reply({'type':'error', 'code':'INVALID_VERDICT'}), b'ERR,FORMAT,end')

    def test_encoder_rejects_commas(self):
        for m in (message(worker_id='D,123'), message('packaging', barcode='ABC,end')):
            with self.assertRaises(ValueError): encode_result(m)


class TextFramingTests(unittest.TestCase):
    def test_every_possible_byte_split(self):
        raw = 'packaging,1,工号01,前end😀后,OK,NG,end'.encode()
        for split in range(1, len(raw)):
            f = FrameDecoder(); b = bytearray(raw[:split])
            self.assertIsNone(f.pop(b), split)
            b.extend(raw[split:]); self.assertEqual(f.pop(b), raw); self.assertFalse(b)

    def test_bytewise_input_with_split_terminator(self):
        f = FrameDecoder(); b = bytearray(); raw = b'screw,1,D,4,end'
        for byte in raw[:-1]:
            b.append(byte); self.assertIsNone(f.pop(b))
        b.append(raw[-1]); self.assertEqual(f.pop(b), raw)

    def test_coalesced_frames_without_newlines(self):
        raw = b'screw,1,D,4,end'; b = bytearray(raw*3); f = FrameDecoder()
        self.assertEqual([f.pop(b) for _ in range(3)], [raw]*3); self.assertIsNone(f.pop(b))

    def test_optional_crlf_outside_frames_only(self):
        raw = b'packaging,1,D, 001-AbC ,OK,NG,end'; f = FrameDecoder()
        b = bytearray(b'\r\n'+raw+b'\r\n'+raw+b'\r\n')
        self.assertEqual(f.pop(b), raw); self.assertEqual(f.pop(b), raw)
        self.assertIsNone(f.pop(b)); self.assertFalse(b)

    def test_barcode_equal_to_end_is_not_boundary(self):
        f = FrameDecoder(); b = bytearray(b'packaging,1,D,end')
        self.assertIsNone(f.pop(b)); b.extend(b',OK,NG,end')
        self.assertEqual(decode_text(f.pop(b))['barcode'], 'end')

    def test_wrong_terminal_is_not_scanned_for_later_end(self):
        for raw in (b'screw,1,D,4,extra,end', b'packaging,1,D,a,b,OK,NG,end'):
            with self.assertRaises(p.ProtocolError): FrameDecoder().pop(bytearray(raw))

    def test_oversized_complete_frame_rejected(self):
        with self.assertRaises(p.ProtocolError):
            FrameDecoder().pop(bytearray(b'screw,1,'+b'D'*p.MAX_FRAME+b',4,end'))

    def test_json_mode_is_pinned_and_unchanged(self):
        f = FrameDecoder(); a = p.encode(message()); b = bytearray(a+a)
        self.assertEqual(f.pop(b), a); self.assertEqual(f.mode, 'json'); self.assertEqual(f.pop(b), a)
        b.extend(b'screw,1,D,4,end'); self.assertIsNone(f.pop(b)); self.assertEqual(f.mode, 'json')

    def test_json_leading_whitespace_compatibility(self):
        f = FrameDecoder(); b = bytearray(b' \t')
        self.assertIsNone(f.pop(b))
        raw = p.encode(message()); b.extend(raw)
        self.assertEqual(f.pop(b), b' \t'+raw)
        self.assertEqual(f.mode, 'json')

    def test_text_mode_does_not_switch_to_json(self):
        f = FrameDecoder(); b = bytearray(b'ping,end'); f.pop(b); b.extend(p.encode(message()))
        with self.assertRaises(p.ProtocolError): f.pop(b)


class TextTCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = WebConfig(data_dir=Path(self.temp.name), tcp_port=0, auto_backup=False)
        self.runtime = StationRuntime(self.config); self.addCleanup(lambda: self.runtime.close())
        self.store = self.runtime.store

    def client(self):
        c = TextClient(self.runtime.tcp.address); self.addCleanup(c.close); return c

    def test_four_independent_connections_and_states(self):
        pairs = [(p,n) for p in ('screw','packaging') for n in (1,2)]
        clients = [self.client() for _ in pairs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda item: item[0].send(encode_result(message(*item[1]))), zip(clients, pairs)))
        self.assertEqual(replies, [b'ACK,end']*4)
        for project in ('screw','packaging'):
            s = self.runtime.engine.snapshot(project)
            self.assertTrue(all(x['connected'] for x in s['stations']))
            self.assertEqual(len(s['records']), 2)

    def test_no_newline_needed_and_ack_after_commit(self):
        self.assertEqual(self.client().send('screw,1,D70516,4,end'), b'ACK,end')
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT screw_count,worker_id FROM results').fetchone(), (4,'D70516'))

    def test_partial_result_does_not_show_before_end(self):
        c = self.client(); c.sock.sendall(b'screw,1,D70516,4,en'); time.sleep(.03)
        self.assertEqual(self.store.records('screw'), [])
        c.sock.sendall(b'd'); self.assertEqual(c.read(), b'ACK,end')

    def test_many_reports_in_one_write_all_count(self):
        c = self.client(); c.sock.sendall(b'screw,1,D70516,4,end'*10)
        self.assertEqual([c.read() for _ in range(10)], [b'ACK,end']*10)
        self.assertEqual(len(self.store.records('screw')), 10)

    def test_optional_newline_repeated_deliveries(self):
        c = self.client()
        for ending in (b'\r\n', b'\n', b''):
            self.assertEqual(c.send(b'screw,1,D70516,0,end'+ending), b'ACK,end')
        self.assertEqual(len(self.store.records('screw')), 3)

    def test_wrong_format_never_fabricates_ng(self):
        c = self.client()
        self.assertEqual(c.send('packaging,1,D,001,ok,NG,end'), b'ERR,FORMAT,end')
        self.assertEqual(self.store.records('packaging'), [])
        self.assertEqual(c.send('packaging,1,D,001,OK,NG,end'), b'ACK,end')

    def test_unknown_or_ambiguous_framing_closes_connection(self):
        c = self.client(); self.assertEqual(c.send('screw,1,D,4,extra,end'), b'ERR,FORMAT,end')
        self.assertEqual(c.sock.recv(64), b''); self.assertEqual(self.store.records('screw'), [])

    def test_station_ownership_and_route_mismatch(self):
        a,b = self.client(),self.client(); a.send('screw,1,D,4,end')
        self.assertEqual(b.send('screw,1,OTHER,3,end'), b'ERR,STATION_BUSY,end')
        self.assertEqual(a.send('screw,2,D,4,end'), b'ERR,STATION_MISMATCH,end')
        self.assertEqual(a.send('packaging,1,D,ABC,OK,NG,end'), b'ERR,STATION_MISMATCH,end')
        self.assertEqual(len(self.store.records('screw')), 1)

    def test_hello_and_ping_are_optional_not_observations(self):
        c = self.client(); self.assertEqual(c.send('ping,end'), b'ERR,IDENTIFY_FIRST,end')
        self.assertEqual(c.send('hello,packaging,1,end'), b'HELLO,end')
        self.assertEqual(c.send('ping,end'), b'PONG,end')
        self.assertEqual(self.store.records('packaging'), [])
        self.assertEqual(c.send('packaging,1,D,ABC,OK,NG,end'), b'ACK,end')

    def test_bad_frame_error_limit_closes(self):
        c = self.client()
        for _ in range(5): self.assertEqual(c.send('screw,1,D,-1,end'), b'ERR,FORMAT,end')
        self.assertEqual(c.sock.recv(64), b''); self.assertEqual(self.store.records('screw'), [])

    def test_unterminated_oversize_is_not_recorded(self):
        c = self.client(); c.sock.sendall(b'screw,1,'+b'X'*18000)
        self.assertEqual(c.sock.recv(64), b''); self.assertEqual(self.store.records('screw'), [])

    def test_unterminated_frame_deadline(self):
        c = self.client(); c.send('hello,screw,1,end'); c.sock.settimeout(7)
        c.sock.sendall(b'screw,1,D,4,en')
        self.assertEqual(c.sock.recv(64), b''); self.assertEqual(self.store.records('screw'), [])

    def test_count_change_updates_operator_without_sessions(self):
        c = self.client(); c.send('screw,1,0001,4,end'); c.send('screw,1,0002,3,end')
        s = self.runtime.engine.snapshot('screw')['stations'][0]
        self.assertEqual((s['worker_id'], s['latest']['screw_count']), ('0002',3))

    def test_raw_logs_are_original_text(self):
        c = self.client(); c.send('packaging,1,0001,AbCend001,OK,NG,end')
        logs = self.store.logs('packaging')
        self.assertTrue(any(x['raw']=='packaging,1,0001,AbCend001,OK,NG,end' and x['direction']=='RX' for x in logs))
        self.assertTrue(any(x['raw']=='ACK,end' and x['direction']=='TX' for x in logs))

    def test_catalog_exact_per_station_and_no_downlink(self):
        code = ' end001-AbC '
        boxes = [{'id':'A','name':'箱1','standard_barcode':code}, {'id':'B','name':'箱2','standard_barcode':'OTHER'}]
        self.store.set_catalog(boxes,0,uuid.uuid4().hex,'test')
        self.store.select_standard(1,'A',1,uuid.uuid4().hex,'test')
        self.store.select_standard(2,'B',2,uuid.uuid4().hex,'test')
        for n in (1,2):
            c = self.client(); self.assertEqual(c.send(f'packaging,{n},D,{code},OK,NG,end'), b'ACK,end')
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'], 'MATCH')
        self.assertEqual(self.store.latest('packaging',2)['barcode_status'], 'MISMATCH')
        self.assertEqual(self.store.latest('packaging',1)['flame'], 'NG')
        public = self.runtime.engine.snapshot('packaging',public=True)
        self.assertNotIn('catalog',public); self.assertNotIn('OTHER',json.dumps(public))

    def test_blank_unconfigured_unselected(self):
        c = self.client(); c.send('packaging,1,D,ABC,OK,NG,end')
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'UNCONFIGURED')
        self.store.set_catalog([{'id':'A','name':'箱1','standard_barcode':'ABC'}],0,uuid.uuid4().hex,'test')
        c.send('packaging,1,D,ABC,OK,NG,end')
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'UNSELECTED')
        c.send('packaging,1,D,,OK,NG,end')
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'UNREAD')

    def test_history_and_repeated_text_after_standard_change(self):
        self.store.set_standard('ABC',0,uuid.uuid4().hex,'test')
        c = self.client(); c.send('packaging,1,D,ABC,OK,NG,end')
        self.store.set_standard('OTHER',1,uuid.uuid4().hex,'test')
        c.sock.settimeout(.1)
        with self.assertRaises(socket.timeout): c.sock.recv(1)  # No unsolicited standard.
        c.sock.settimeout(3); c.send('packaging,1,D,ABC,OK,NG,end')
        rows = self.store.records('packaging')
        self.assertEqual([r['barcode_status'] for r in rows],['MISMATCH','MATCH'])
        self.assertEqual(rows[1]['standard_barcode'],'ABC')

    def test_storage_failure_does_not_ack(self):
        c = self.client()
        with patch.object(self.store,'record',side_effect=sqlite3.OperationalError('synthetic fault')):
            with self.assertLogs('competition.station_runtime',level='ERROR'):
                c.sock.sendall(b'screw,1,D,4,end'); self.assertEqual(c.sock.recv(64), b'')
        self.assertFalse(self.runtime.healthy); self.assertEqual(self.store.records('screw'), [])

    def test_json_v2_compatibility_keeps_dedup(self):
        m = message(station=2)
        with socket.create_connection(self.runtime.tcp.address,timeout=3) as sock, sock.makefile('rb') as stream:
            for duplicate in (False,True):
                sock.sendall(p.encode(m)); reply = p.decode(stream.readline()[:-1])
                self.assertEqual(reply['duplicate'],duplicate)
            sock.sendall(p.encode({**m,'screw_count':3}))
            self.assertEqual(p.decode(stream.readline()[:-1])['code'],'MSG_ID_CONFLICT')
            self.assertEqual(self.client().send('screw,1,D,4,end'), b'ACK,end')
        self.assertEqual(len(self.store.records('screw')), 2)

    def test_json_station_cannot_be_taken_by_text(self):
        with socket.create_connection(self.runtime.tcp.address,timeout=3) as sock, sock.makefile('rb') as stream:
            sock.sendall(p.encode(message())); p.decode(stream.readline()[:-1])
            self.assertEqual(self.client().send('screw,1,D,4,end'), b'ERR,STATION_BUSY,end')

    def test_restart_retains_catalog_and_text_not_deduplicated(self):
        c = self.client(); self.store.set_standard('ABC',0,uuid.uuid4().hex,'test')
        c.send('packaging,1,D,ABC,OK,NG,end'); c.close(); self.runtime.close()
        self.runtime = StationRuntime(self.config); self.store = self.runtime.store
        self.assertFalse(self.runtime.engine.snapshot('packaging')['stations'][0]['connected'])
        self.client().send('packaging,1,D,ABC,OK,NG,end')
        self.assertEqual(len(self.store.records('packaging')), 2)
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'], 'MATCH')

    def test_default_cli_sends_text(self):
        host,port = self.runtime.tcp.address
        root = Path(__file__).resolve().parents[1]
        run = subprocess.run([sys.executable,str(root/'station_client.py'),'--host',host,'--port',str(port),
                              '--project','screw','--station','1','--worker-id','D70516','--count','4'],
                             capture_output=True,text=True,timeout=8)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('screw,1,D70516,4,end',run.stdout); self.assertIn('ACK,end',run.stdout)
        self.assertNotIn('"v"',run.stdout)

    def test_text_and_legacy_client_helpers(self):
        host,port = self.runtime.tcp.address
        self.assertEqual(send_text_result(host,port,message('packaging',1)), 'ACK,end')
        self.assertTrue(send_result(host,port,message('packaging',2))['recorded'])


class TextClientTests(unittest.TestCase):
    @contextmanager
    def responder(self, parts):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
        address=listener.getsockname();received=[]
        def run():
            try:
                with listener.accept()[0] as conn:
                    conn.settimeout(3);received.append(conn.recv(4096))
                    for part in parts:
                        conn.sendall(part);time.sleep(.01)
            finally:listener.close()
        thread=threading.Thread(target=run,daemon=True);thread.start()
        try:yield address,received
        finally:thread.join(4)

    def test_split_ack_without_newline(self):
        with self.responder([b'A',b'CK,',b'e',b'nd']) as (address,received):
            self.assertEqual(send_text_result(*address,message()),'ACK,end')
        self.assertEqual(received,[b'screw,1,001234,4,end'])

    def test_error_reply_does_not_retry(self):
        with self.responder([b'ERR,STATION_BUSY,end']) as (address,received):
            with self.assertRaises(ValueError):send_text_result(*address,message())
        self.assertEqual(len(received),1)

    def test_closed_connection_unknown_outcome_no_retry(self):
        with self.responder([]) as (address,received):
            with self.assertRaises(ConnectionError):send_text_result(*address,message())
        self.assertEqual(len(received),1)

    def test_cli_does_not_silently_discard_retry_id(self):
        root=Path(__file__).resolve().parents[1]
        run=subprocess.run([sys.executable,str(root/'station_client.py'),'--project','screw','--station','1',
                            '--worker-id','D','--count','4','--msg-id','retry-id'],capture_output=True,timeout=5)
        self.assertEqual(run.returncode,2)


class TextHTTPIntegrationTests(unittest.TestCase):
    def test_real_http_import_selection_tcp_display_export_and_backup(self):
        import uvicorn
        from competition.station_webapp import create_app
        from competition.web_auth import credential_record
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);credentials=root/'admin.json';password='Only-synthetic-test-123456'
            credentials.write_text(json.dumps(credential_record('admin',password)),encoding='utf-8')
            listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            base=f'http://localhost:{port}'
            config=WebConfig(data_dir=root/'data',credentials=credentials,public_url=base,port=port,tcp_port=0,auto_backup=False)
            app=create_app(config)
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',ws='none',proxy_headers=False))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
            cookie='';csrf='';clients=[]
            import http.client as http_client
            def request(method,path,payload=None):
                connection=http_client.HTTPConnection('127.0.0.1',port,timeout=5)
                headers={'Host':f'localhost:{port}','Origin':base,'Cookie':cookie,'X-CSRF-Token':csrf,'X-Request-ID':uuid.uuid4().hex}
                data=None
                if payload is not None:data=json.dumps(payload);headers['Content-Type']='application/json'
                try:
                    connection.request(method,path,body=data,headers=headers)
                    response=connection.getresponse();return response.status,dict(response.getheaders()),response.read()
                finally:connection.close()
            try:
                for _ in range(150):
                    if server.started:break
                    time.sleep(.02)
                self.assertTrue(server.started)
                status,headers,body=request('POST','/api/auth/login',{'username':'admin','password':password})
                self.assertEqual(status,200);cookie=headers['set-cookie'].split(';')[0];csrf=json.loads(body)['csrf']
                boxes=[{'id':'A','name':'箱1','standard_barcode':'001-AbC'},{'id':'B','name':'箱2','standard_barcode':'OTHER'}]
                status,_,body=request('POST','/api/admin/standard-barcode/import',{'content':json.dumps({'boxes':boxes}),'revision':0})
                self.assertEqual(status,200,body)
                for n,bid in [(1,'A'),(2,'B')]:
                    status,_,body=request('POST','/api/admin/standard-barcode/select',{'station':n,'box_id':bid,'revision':n})
                    self.assertEqual(status,200,body)
                for project in ('screw','packaging'):
                    for n in (1,2):
                        c=TextClient(app.state.runtime.tcp.address);clients.append(c)
                        for _ in range(10):self.assertEqual(c.send(encode_result(message(project,n))),b'ACK,end')
                for project in ('screw','packaging'):
                    status,_,body=request('GET','/api/admin/state?project='+project);self.assertEqual(status,200)
                    state=json.loads(body);self.assertEqual(len(state['records']),20)
                    self.assertTrue(all(s['connected'] for s in state['stations']))
                    if project=='packaging':
                        self.assertEqual([s['latest']['barcode_status'] for s in state['stations']],['MATCH','MISMATCH'])
                        self.assertEqual([s['latest']['flame'] for s in state['stations']],['NG','NG'])
                _,_,export=request('GET','/api/admin/export?project=packaging&format=jsonl')
                rows=[json.loads(line) for line in export.decode().splitlines()]
                self.assertEqual(len(rows),20);self.assertEqual(rows[0]['barcode'],'001-AbC')
                self.assertEqual(rows[0]['standard_box_id'],'A')
                status,_,body=request('POST','/api/admin/backup',{});self.assertEqual(status,200)
                backup=json.loads(body);status,_,data=request('GET',backup['download_url']);self.assertEqual(status,200)
                self.assertTrue(data.startswith(b'SQLite format 3'))
                cookie='';csrf=''
                _,_,body=request('GET','/api/display?project=packaging')
                public=json.loads(body);self.assertNotIn('catalog',public);self.assertNotIn('OTHER',body.decode())
                self.assertEqual(request('GET','/api/admin/state')[0],401)
            finally:
                for c in clients:c.close()
                server.should_exit=True;thread.join(10);listener.close()


if __name__ == '__main__':
    unittest.main()
