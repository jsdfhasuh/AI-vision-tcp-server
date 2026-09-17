"""Station + group text regression: exact frames, real TCP, persistence and HTTP.

Synthetic fixtures only; integration tests use real sockets, not mocked transports.
Legacy JSON v2 is tested separately and never labels an old worker ID as a group.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import csv
import http.client
import io
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
from competition.web_config import WebConfig
from station_client import send_text_result, send_result


def message(project='screw', station=1, **values):
    m = dict(v=2, type='result', msg_id=uuid.uuid4().hex, project=project, station=station, group_id='001234')
    m.update(dict(screw_count=4, detection_result='NG') if project == 'screw' else
             dict(barcode='001-AbC', logo='OK', flame='NG', total_result='OK'))
    m.update(values)
    return m


def legacy(project='screw', station=1):
    m = message(project, station)
    m.pop('group_id');m.pop('detection_result' if project == 'screw' else 'total_result')
    m['worker_id'] = 'OLD001'
    return m


class TextClient:
    def __init__(self, address):
        self.sock = socket.create_connection(address, timeout=3)
        self.buffer = bytearray()

    def read(self):
        while b',end' not in self.buffer:
            chunk = self.sock.recv(1024)
            if not chunk: raise ConnectionError('closed before full text response')
            self.buffer.extend(chunk)
        length = self.buffer.index(b',end') + 4
        result = bytes(self.buffer[:length]);del self.buffer[:length]
        return result

    def send(self, raw):
        self.sock.sendall(raw.encode('utf-8') if isinstance(raw, str) else raw)
        return self.read()

    def close(self):
        self.sock.close()


class TextCodecTests(unittest.TestCase):
    def test_screw_exact_latest_layout(self):
        m=decode_text(b'screw,1,000012,4,NG,end')
        self.assertEqual((m['station'],m['group_id'],m['screw_count'],m['detection_result']),(1,'000012',4,'NG'))
        self.assertNotIn('worker_id',m);self.assertNotIn('total_result',m)

    def test_packaging_exact_latest_layout(self):
        m=decode_text(b'packaging,2,G9,001-AbC,OK,NG,OK,end')
        self.assertEqual((m['station'],m['group_id'],m['barcode'],m['logo'],m['flame'],m['total_result']),(2,'G9','001-AbC','OK','NG','OK'))
        self.assertNotIn('worker_id',m)

    def test_exact_barcode_and_end_as_data(self):
        for code in ['001-AbC',' 001-AbC ','end','friend','001end002','型号end😀','']:
            with self.subTest(code=code):
                self.assertEqual(decode_text(f'packaging,2,G1,{code},OK,NG,OK,end'.encode())['barcode'],code)

    def test_groups_are_text_not_station_numbers(self):
        for group in ['G100','001','第三组','end','g1']:
            m=decode_text(f'screw,1,{group},4,OK,end'.encode())
            self.assertEqual(m['group_id'],group);self.assertEqual(m['station'],1)

    def test_identical_text_has_fresh_ids(self):
        raw=b'screw,1,G1,4,OK,end';self.assertNotEqual(decode_text(raw)['msg_id'],decode_text(raw)['msg_id'])

    def test_control_frames(self):
        self.assertEqual(decode_text(b'hello,packaging,2,end')['type'],'hello')
        self.assertEqual(decode_text(b'ping,end')['type'],'ping')

    def test_zero_and_integer_limit(self):
        for count in ['0','004','2147483647']:
            self.assertEqual(decode_text(f'screw,1,G1,{count},OK,end'.encode())['screw_count'],int(count))

    def test_invalid_counts(self):
        for count in ['','-1','+1','4.0','4e0','true',' 4','٤','2147483648','1'*100]:
            with self.subTest(count=count),self.assertRaises(p.ProtocolError):decode_text(f'screw,1,G1,{count},OK,end'.encode())

    def test_only_station_one_two(self):
        for n in ['0','3','01',' 1','true']:
            with self.subTest(n=n),self.assertRaises(p.ProtocolError):decode_text(f'screw,{n},G1,4,OK,end'.encode())

    def test_all_reported_results_strict_ok_ng(self):
        for proj,key in [('screw','detection_result'),('packaging','logo'),('packaging','flame'),('packaging','total_result')]:
            for val in ['ok','ng','',False,1,None,'破损']:
                with self.subTest(proj=proj,key=key,val=val),self.assertRaises(p.ProtocolError):encode_result(message(proj,**{key:val}))

    def test_missing_result_fields_rejected(self):
        for proj,key in [('screw','detection_result'),('packaging','logo'),('packaging','flame'),('packaging','total_result')]:
            m=message(proj);m.pop(key)
            with self.subTest(key=key),self.assertRaises(p.ProtocolError):encode_result(m)

    def test_legacy_text_and_extra_employee_field_rejected(self):
        for raw in [b'screw,1,D001,4,end',b'packaging,1,D001,B,OK,NG,end',
                    b'screw,1,D001,G1,4,OK,end',b'packaging,1,D001,G1,B,OK,NG,NG,end',
                    b'screw,G1,4,OK,end',b'packaging,G1,B,OK,NG,NG,end']:
            with self.subTest(raw=raw),self.assertRaises(p.ProtocolError):decode_text(raw)

    def test_invalid_groups_encoding_and_delimiters(self):
        for raw in [b'screw,1,,4,OK,end',b'screw,1, G1,4,OK,end',b'screw,1,G\n1,4,OK,end',
                    b'screw,1,G\xff,4,OK,end',b'screw,1,G,4,OK,END',b'screw,1,G,4,OK,end\n',
                    b'\xef\xbb\xbfscrew,1,G,4,OK,end','screw,1，G1,4,OK，end'.encode()]:
            with self.subTest(raw=raw),self.assertRaises(p.ProtocolError):decode_text(raw)

    def test_field_lengths(self):
        for m in [message(group_id='G'*65),message('packaging',barcode='x'*513)]:
            with self.assertRaises(p.ProtocolError):encode_result(m)
        self.assertEqual(len(decode_text(encode_result(message(group_id='G'*64)))['group_id']),64)

    def test_encoder_and_reply_no_json_no_newline(self):
        self.assertEqual(encode_result(message()),b'screw,1,001234,4,NG,end')
        self.assertEqual(encode_result(message('packaging')),b'packaging,1,001234,001-AbC,OK,NG,OK,end')
        for m,reply in [({'type':'result_ack','recorded':True},b'ACK,end'),({'type':'hello_ok'},b'HELLO,end'),
                        ({'type':'pong'},b'PONG,end'),({'type':'error','code':'INVALID_VERDICT'},b'ERR,FORMAT,end')]:
            self.assertEqual(encode_reply(m),reply)

    def test_encoder_rejects_commas_and_employee_substitution(self):
        for m in [message(group_id='G,1'),message('packaging',barcode='ABC,end'),message(worker_id='D1')]:
            with self.assertRaises(ValueError):encode_result(m)
        with self.assertRaises(ValueError):encode_result(legacy())


class TextFramingTests(unittest.TestCase):
    def test_every_utf8_byte_split(self):
        raw='packaging,1,第三组,前end😀后,OK,NG,NG,end'.encode()
        for split in range(1,len(raw)):
            f=FrameDecoder();b=bytearray(raw[:split]);self.assertIsNone(f.pop(b),split)
            b.extend(raw[split:]);self.assertEqual(f.pop(b),raw);self.assertFalse(b)

    def test_bytewise_terminator(self):
        f=FrameDecoder();b=bytearray();raw=b'screw,1,G1,4,OK,end'
        for byte in raw[:-1]: b.append(byte);self.assertIsNone(f.pop(b))
        b.append(raw[-1]);self.assertEqual(f.pop(b),raw)

    def test_coalesced_without_newline(self):
        raw=b'screw,1,G1,4,OK,end';b=bytearray(raw*3);f=FrameDecoder()
        self.assertEqual([f.pop(b) for _ in range(3)],[raw]*3);self.assertIsNone(f.pop(b))

    def test_crlf_outside_frames(self):
        raw=b'packaging,1,G1, 001-AbC ,OK,NG,NG,end';f=FrameDecoder();b=bytearray(b'\r\n'+raw+b'\r\n'+raw+b'\n')
        self.assertEqual(f.pop(b),raw);self.assertEqual(f.pop(b),raw);self.assertIsNone(f.pop(b));self.assertFalse(b)

    def test_barcode_equal_end_is_not_boundary(self):
        f=FrameDecoder();b=bytearray(b'packaging,1,end,end');self.assertIsNone(f.pop(b));b.extend(b',OK,NG,NG,end')
        m=decode_text(f.pop(b));self.assertEqual((m['group_id'],m['barcode']),('end','end'))

    def test_wrong_terminator_no_resync(self):
        for raw in [b'screw,1,G,4,OK,extra,end',b'packaging,1,G,a,b,OK,NG,NG,end']:
            with self.assertRaises(p.ProtocolError):FrameDecoder().pop(bytearray(raw))

    def test_oversized_complete_frame(self):
        with self.assertRaises(p.ProtocolError):FrameDecoder().pop(bytearray(b'screw,1,'+b'G'*p.MAX_FRAME+b',4,OK,end'))

    def test_json_mode_fixed(self):
        f=FrameDecoder();a=p.encode(legacy());b=bytearray(a+a)
        self.assertEqual(f.pop(b),a);self.assertEqual(f.pop(b),a);self.assertEqual(f.mode,'json')
        b.extend(b'screw,1,G,4,OK,end');self.assertIsNone(f.pop(b))

    def test_json_leading_whitespace(self):
        f=FrameDecoder();b=bytearray(b' \t');self.assertIsNone(f.pop(b));raw=p.encode(legacy());b.extend(raw)
        self.assertEqual(f.pop(b),b' \t'+raw)

    def test_text_mode_fixed(self):
        f=FrameDecoder();b=bytearray(b'ping,end');f.pop(b);b.extend(p.encode(legacy()))
        with self.assertRaises(p.ProtocolError):f.pop(b)


class TextTCPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config=WebConfig(data_dir=Path(self.temp.name),tcp_port=0,auto_backup=False)
        self.runtime=StationRuntime(self.config);self.addCleanup(lambda:self.runtime.close());self.store=self.runtime.store

    def client(self):
        c=TextClient(self.runtime.tcp.address);self.addCleanup(c.close);return c

    def test_four_independent_slots_same_group(self):
        pairs=[(proj,n) for proj in ('screw','packaging') for n in (1,2)];clients=[self.client() for _ in pairs]
        with ThreadPoolExecutor(4) as pool:
            replies=list(pool.map(lambda x:x[0].send(encode_result(message(*x[1]))),zip(clients,pairs)))
        self.assertEqual(replies,[b'ACK,end']*4)
        for project in ('screw','packaging'):
            s=self.runtime.engine.snapshot(project);self.assertTrue(all(x['connected'] for x in s['stations']))
            self.assertEqual([x['group_id'] for x in s['stations']],['001234']*2)

    def test_ack_after_commit_no_newline(self):
        self.assertEqual(self.client().send('screw,1,G1,4,NG,end'),b'ACK,end')
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute('SELECT screw_count,group_id,detection_result,worker_id FROM results').fetchone(),(4,'G1','NG',''))

    def test_partial_not_visible(self):
        c=self.client();c.sock.sendall(b'screw,1,G1,4,OK,en');time.sleep(.03);self.assertEqual(self.store.records('screw'),[])
        c.sock.sendall(b'd');self.assertEqual(c.read(),b'ACK,end')

    def test_ten_identical_reports_are_ten_records(self):
        c=self.client();c.sock.sendall(b'screw,1,G1,4,OK,end'*10)
        self.assertEqual([c.read() for _ in range(10)],[b'ACK,end']*10);self.assertEqual(len(self.store.records('screw')),10)

    def test_optional_newlines(self):
        c=self.client()
        for ending in (b'\r\n',b'\n',b''):self.assertEqual(c.send(b'screw,1,G1,0,NG,end'+ending),b'ACK,end')
        self.assertEqual(len(self.store.records('screw')),3)

    def test_invalid_value_then_correct(self):
        c=self.client();self.assertEqual(c.send('packaging,1,G1,B,ok,NG,NG,end'),b'ERR,FORMAT,end')
        self.assertEqual(self.store.records('packaging'),[])
        self.assertEqual(c.send('packaging,1,G1,B,OK,NG,NG,end'),b'ACK,end')

    def test_ambiguous_boundary_closes(self):
        c=self.client();self.assertEqual(c.send('screw,1,G1,4,OK,extra,end'),b'ERR,FORMAT,end')
        self.assertEqual(c.sock.recv(64),b'');self.assertEqual(self.store.records('screw'),[])

    def test_slot_exclusive_and_cannot_switch_station(self):
        a,b=self.client(),self.client();a.send('screw,1,G1,4,OK,end')
        self.assertEqual(b.send('screw,1,G2,3,NG,end'),b'ERR,STATION_BUSY,end')
        self.assertEqual(a.send('screw,2,G1,4,OK,end'),b'ERR,STATION_MISMATCH,end')
        self.assertEqual(a.send('packaging,1,G1,B,OK,NG,NG,end'),b'ERR,STATION_MISMATCH,end')
        self.assertEqual(len(self.store.records('screw')),1)

    def test_controls_not_results(self):
        c=self.client();self.assertEqual(c.send('ping,end'),b'ERR,IDENTIFY_FIRST,end')
        self.assertEqual(c.send('hello,packaging,1,end'),b'HELLO,end');self.assertEqual(c.send('ping,end'),b'PONG,end')
        self.assertEqual(self.store.records('packaging'),[])

    def test_error_limit(self):
        c=self.client()
        for _ in range(5):self.assertEqual(c.send('screw,1,G1,-1,OK,end'),b'ERR,FORMAT,end')
        self.assertEqual(c.sock.recv(64),b'')

    def test_oversized_partial(self):
        c=self.client();c.sock.sendall(b'screw,1,'+b'X'*18000);self.assertEqual(c.sock.recv(64),b'')
        self.assertEqual(self.store.records('screw'),[])

    def test_partial_deadline(self):
        c=self.client();c.send('hello,screw,1,end');c.sock.settimeout(7);c.sock.sendall(b'screw,1,G,4,OK,en')
        self.assertEqual(c.sock.recv(64),b'');self.assertEqual(self.store.records('screw'),[])

    def test_group_change_same_slot_no_registration(self):
        c=self.client();c.send('screw,1,0001,4,NG,end');c.send('screw,1,0002,3,OK,end')
        s=self.runtime.engine.snapshot('screw')['stations'][0]
        self.assertEqual((s['group_id'],s['latest']['screw_count'],s['latest']['detection_result']),('0002',3,'OK'))
        self.assertIsNone(s['worker_id'])

    def test_original_text_logs(self):
        raw='packaging,1,G1,AbCend001,OK,NG,OK,end';self.client().send(raw);logs=self.store.logs('packaging')
        self.assertTrue(any(x['raw']==raw and x['direction']=='RX' for x in logs))
        self.assertTrue(any(x['raw']=='ACK,end' and x['direction']=='TX' for x in logs))

    def test_per_station_standard_and_independent_total(self):
        code=' end001-AbC ';self.store.set_catalog([{'id':'A','name':'箱1','standard_barcode':code},{'id':'B','name':'箱2','standard_barcode':'OTHER'}],0,uuid.uuid4().hex,'test')
        for n,bid in [(1,'A'),(2,'B')]:self.store.select_standard(n,bid,n,uuid.uuid4().hex,'test')
        for n in (1,2):self.assertEqual(self.client().send(f'packaging,{n},G9,{code},OK,NG,OK,end'),b'ACK,end')
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'MATCH')
        row=self.store.latest('packaging',2);self.assertEqual((row['barcode_status'],row['flame'],row['total_result']),('MISMATCH','NG','OK'))
        public=self.runtime.engine.snapshot('packaging',public=True);self.assertNotIn('catalog',public);self.assertNotIn('OTHER',json.dumps(public))

    def test_unconfigured_unselected_unread(self):
        c=self.client();c.send('packaging,1,G1,ABC,OK,NG,OK,end');self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'UNCONFIGURED')
        self.store.set_catalog([{'id':'A','name':'箱1','standard_barcode':'ABC'}],0,uuid.uuid4().hex,'test')
        c.send('packaging,1,G1,ABC,OK,NG,OK,end');self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'UNSELECTED')
        c.send('packaging,1,G1,,OK,NG,OK,end');r=self.store.latest('packaging',1);self.assertEqual((r['barcode_status'],r['total_result']),('UNREAD','OK'))

    def test_history_standard_change_no_downlink(self):
        self.store.set_standard('ABC',0,uuid.uuid4().hex,'test');c=self.client();raw='packaging,1,G1,ABC,OK,NG,NG,end';c.send(raw)
        self.store.set_standard('OTHER',1,uuid.uuid4().hex,'test');c.sock.settimeout(.1)
        with self.assertRaises(socket.timeout):c.sock.recv(1)
        c.sock.settimeout(3);c.send(raw);rows=self.store.records('packaging')
        self.assertEqual([r['barcode_status'] for r in rows],['MISMATCH','MATCH']);self.assertEqual(rows[1]['standard_barcode'],'ABC')

    def test_storage_failure_no_ack(self):
        c=self.client()
        with patch.object(self.store,'record',side_effect=sqlite3.OperationalError('synthetic fault')),self.assertLogs('competition.station_runtime',level='ERROR'):
            c.sock.sendall(b'screw,1,G1,4,OK,end');self.assertEqual(c.sock.recv(64),b'')
        self.assertFalse(self.runtime.healthy)

    def test_legacy_json_keeps_dedup_and_no_group_invention(self):
        m=legacy(station=2)
        with socket.create_connection(self.runtime.tcp.address,timeout=3) as sock,sock.makefile('rb') as f:
            for duplicate in (False,True):
                sock.sendall(p.encode(m));self.assertEqual(p.decode(f.readline()[:-1])['duplicate'],duplicate)
            sock.sendall(p.encode({**m,'screw_count':3}));self.assertEqual(p.decode(f.readline()[:-1])['code'],'MSG_ID_CONFLICT')
            self.assertEqual(self.client().send('screw,1,G1,4,OK,end'),b'ACK,end')
        row=self.store.latest('screw',2);self.assertEqual(row['worker_id'],'OLD001');self.assertIsNone(row['group_id']);self.assertIsNone(row['detection_result'])

    def test_legacy_slot_not_taken(self):
        with socket.create_connection(self.runtime.tcp.address,timeout=3) as sock,sock.makefile('rb') as f:
            sock.sendall(p.encode(legacy()));p.decode(f.readline()[:-1]);self.assertEqual(self.client().send('screw,1,G1,4,OK,end'),b'ERR,STATION_BUSY,end')

    def test_restart_preserves_values_and_stale_state(self):
        self.store.set_standard('ABC',0,uuid.uuid4().hex,'test');c=self.client();raw='packaging,1,G1,ABC,OK,NG,OK,end';c.send(raw);c.close();self.runtime.close()
        self.runtime=StationRuntime(self.config);self.store=self.runtime.store
        self.assertFalse(self.runtime.engine.snapshot('packaging')['stations'][0]['connected'])
        self.client().send(raw);self.assertEqual(len(self.store.records('packaging')),2);self.assertEqual(self.store.latest('packaging',1)['total_result'],'OK')

    def test_cli_default_group_and_result(self):
        host,port=self.runtime.tcp.address
        run=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'station_client.py'),'--host',host,'--port',str(port),
            '--project','screw','--station','1','--group-id','G7','--count','4','--result','NG'],capture_output=True,text=True,timeout=8)
        self.assertEqual(run.returncode,0,run.stderr);self.assertIn('screw,1,G7,4,NG,end',run.stdout);self.assertIn('ACK,end',run.stdout)

    def test_client_helpers(self):
        addr=self.runtime.tcp.address;self.assertEqual(send_text_result(*addr,message('packaging',1)),'ACK,end')
        self.assertTrue(send_result(*addr,legacy('packaging',2))['recorded'])

    def test_disconnect_keeps_history_not_live_group(self):
        c=self.client();c.send('screw,1,G1,4,OK,end');c.close()
        for _ in range(100):
            s=self.runtime.engine.snapshot('screw')['stations'][0]
            if not s['connected']:break
            time.sleep(.01)
        self.assertFalse(s['connected']);self.assertIsNone(s['group_id']);self.assertEqual(s['latest']['group_id'],'G1')


class TextClientTests(unittest.TestCase):
    @contextmanager
    def responder(self,parts):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3);address=listener.getsockname();received=[]
        def serve():
            try:
                with listener.accept()[0] as c:
                    c.settimeout(3);received.append(c.recv(4096))
                    for part in parts:c.sendall(part);time.sleep(.01)
            finally:listener.close()
        t=threading.Thread(target=serve,daemon=True);t.start()
        try:yield address,received
        finally:t.join(4)

    def test_split_ack(self):
        with self.responder([b'A',b'CK,',b'e',b'nd']) as (addr,received):self.assertEqual(send_text_result(*addr,message()),'ACK,end')
        self.assertEqual(received,[b'screw,1,001234,4,NG,end'])

    def test_error_no_retry(self):
        with self.responder([b'ERR,STATION_BUSY,end']) as (addr,received):
            with self.assertRaises(ValueError):send_text_result(*addr,message())
        self.assertEqual(len(received),1)

    def test_closed_unknown_no_retry(self):
        with self.responder([]) as (addr,received):
            with self.assertRaises(ConnectionError):send_text_result(*addr,message())
        self.assertEqual(len(received),1)

    def test_invalid_timeout(self):
        for t in [0,-1,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):send_text_result('127.0.0.1',1,message(),t)

    def test_cli_rejects_missing_result_or_employee_substitution(self):
        base=[sys.executable,str(Path(__file__).resolve().parents[1]/'station_client.py'),'--project','screw','--station','1','--count','4']
        for extra in [['--group-id','G1'],['--worker-id','D1','--result','OK'],['--group-id','G1','--result','OK','--msg-id','x']]:
            run=subprocess.run(base+extra,capture_output=True,timeout=5);self.assertEqual(run.returncode,2)


class TextHTTPIntegrationTests(unittest.TestCase):
    def test_real_http_four_tcp_display_export_backup(self):
        import uvicorn
        from competition.station_webapp import create_app
        from competition.web_auth import credential_record
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);credentials=root/'admin.json';password='Only-synthetic-test-123456'
            credentials.write_text(json.dumps(credential_record('admin',password)),encoding='utf-8')
            listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];base=f'http://localhost:{port}'
            app=create_app(WebConfig(data_dir=root/'data',credentials=credentials,public_url=base,port=port,tcp_port=0,auto_backup=False))
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',ws='none',proxy_headers=False));t=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);t.start()
            cookie='';csrf='';clients=[]
            def request(method,path,payload=None):
                c=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
                h={'Host':f'localhost:{port}','Origin':base,'Cookie':cookie,'X-CSRF-Token':csrf,'X-Request-ID':uuid.uuid4().hex};body=None
                if payload is not None:body=json.dumps(payload);h['Content-Type']='application/json'
                try:
                    c.request(method,path,body,h);r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
                finally:c.close()
            try:
                for _ in range(150):
                    if server.started:break
                    time.sleep(.02)
                self.assertTrue(server.started)
                status,h,b=request('POST','/api/auth/login',{'username':'admin','password':password});self.assertEqual(status,200)
                cookie=h['set-cookie'].split(';')[0];csrf=json.loads(b)['csrf']
                boxes=[{'id':'A','name':'箱1','standard_barcode':'001-AbC'},{'id':'B','name':'箱2','standard_barcode':'OTHER'}]
                self.assertEqual(request('POST','/api/admin/standard-barcode/import',{'content':json.dumps({'boxes':boxes}),'revision':0})[0],200)
                for n,bid in [(1,'A'),(2,'B')]:self.assertEqual(request('POST','/api/admin/standard-barcode/select',{'station':n,'box_id':bid,'revision':n})[0],200)
                for proj in ('screw','packaging'):
                    for n in (1,2):
                        c=TextClient(app.state.runtime.tcp.address);clients.append(c)
                        for _ in range(10):self.assertEqual(c.send(encode_result(message(proj,n))),b'ACK,end')
                for proj in ('screw','packaging'):
                    status,_,b=request('GET','/api/admin/state?project='+proj);self.assertEqual(status,200);s=json.loads(b)
                    self.assertEqual(len(s['records']),20);self.assertTrue(all(x['group_id']=='001234' and x['connected'] for x in s['stations']))
                    if proj=='packaging':
                        self.assertEqual([x['latest']['barcode_status'] for x in s['stations']],['MATCH','MISMATCH'])
                        self.assertTrue(all(x['latest']['total_result']=='OK' and x['latest']['flame']=='NG' for x in s['stations']))
                    else:self.assertTrue(all(x['latest']['detection_result']=='NG' and x['latest']['screw_count']==4 for x in s['stations']))
                    _,_,b=request('GET','/api/admin/export?project='+proj+'&format=csv');rows=list(csv.DictReader(io.StringIO(b.decode('utf-8-sig'))))
                    self.assertEqual(len(rows),20);self.assertEqual(rows[0]['group_id'],'001234');self.assertEqual(rows[0]['worker_id'],'')
                    self.assertEqual(rows[0]['total_result' if proj=='packaging' else 'detection_result'],'OK' if proj=='packaging' else 'NG')
                _,_,b=request('GET','/api/admin/export?project=packaging&format=jsonl');rows=[json.loads(x) for x in b.decode().splitlines()]
                self.assertEqual(rows[0]['group_id'],'001234');self.assertEqual(rows[0]['total_result'],'OK');self.assertEqual(rows[0]['standard_box_id'],'A')
                status,_,b=request('POST','/api/admin/backup',{});self.assertEqual(status,200)
                status,_,b=request('GET',json.loads(b)['download_url']);self.assertEqual(status,200);self.assertTrue(b.startswith(b'SQLite format 3'))
                cookie='';csrf='';_,_,b=request('GET','/api/display?project=packaging');s=json.loads(b)
                self.assertNotIn('catalog',s);self.assertNotIn('OTHER',b.decode());self.assertEqual(s['records'][0]['total_result'],'OK')
                self.assertEqual(request('GET','/api/admin/state')[0],401)
            finally:
                for c in clients:c.close()
                server.should_exit=True;t.join(10);listener.close()


if __name__=='__main__':unittest.main()
