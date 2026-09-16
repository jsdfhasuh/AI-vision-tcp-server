"""Local JSON import regression tests. Uses a real temporary station store/TCP listener."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import unittest
import uuid

from competition.standard_json import EXAMPLE_STANDARD, MAX_JSON_BYTES, parse_standard_json
from competition.station_protocol import encode, decode
from competition.station_store import StationStore
from competition.storage import utc_now

try:
    from fastapi.testclient import TestClient
    from competition.station_webapp import create_app
    from competition.web_auth import credential_record
    from competition.web_config import WebConfig
    WEB = True
except ImportError:
    WEB = False


class StandardJsonParserTests(unittest.TestCase):
    def test_template_is_exact_string(self):
        self.assertEqual(parse_standard_json(json.dumps(EXAMPLE_STANDARD)), '001234-AbC')

    def test_leading_zeros_case_and_spaces_are_preserved(self):
        self.assertEqual(parse_standard_json('{"standard_barcode":" 001-AbC "}'), ' 001-AbC ')

    def test_bom_and_windows_line_endings(self):
        self.assertEqual(parse_standard_json('\ufeff{\r\n "standard_barcode": "001"\r\n}\r\n'), '001')

    def test_unicode_is_preserved(self):
        self.assertEqual(parse_standard_json(json.dumps({'standard_barcode': '型号-001-😀'})), '型号-001-😀')

    def test_explicit_empty_string_clears_standard(self):
        self.assertEqual(parse_standard_json('{"standard_barcode":""}'), '')

    def test_numbers_booleans_arrays_and_null_rejected(self):
        for value in (1234, True, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_standard_json(json.dumps({'standard_barcode': value}))

    def test_wrong_top_level_rejected(self):
        for value in ([], None, 12, '001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_standard_json(json.dumps(value))

    def test_unknown_or_missing_field_rejected(self):
        for value in ({}, {'barcode': '001'}, {'standard_barcode': '001', 'logo': 'OK'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_standard_json(json.dumps(value))

    def test_duplicate_keys_rejected_even_with_escapes(self):
        for content in ('{"standard_barcode":"001","standard_barcode":"002"}',
                        r'{"standard_barcode":"001","\u0073tandard_barcode":"002"}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_standard_json(content)

    def test_malformed_and_nonstandard_json_rejected(self):
        for content in ('', ' ', '\ufeff', '{"standard_barcode":"x",}', '//comment\n{}',
                        '{"standard_barcode":NaN}', '{"standard_barcode":Infinity}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_standard_json(content)

    def test_non_text_and_invalid_unicode_rejected(self):
        for content in (None, {}, b'{}', '\ud800', r'{"standard_barcode":"\ud800"}'):
            with self.subTest(content=repr(content)), self.assertRaises(ValueError):
                parse_standard_json(content)

    def test_control_characters_rejected(self):
        for char in ('\n', '\t', '\0', '\x7f'):
            with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                parse_standard_json(json.dumps({'standard_barcode': 'abc'+char}))

    def test_size_limit_is_in_utf8_bytes(self):
        content=json.dumps({'standard_barcode':'001'})
        self.assertEqual(parse_standard_json(content.ljust(MAX_JSON_BYTES)), '001')
        with self.assertRaises(ValueError): parse_standard_json(content.ljust(MAX_JSON_BYTES+1))
        with self.assertRaises(ValueError): parse_standard_json('界'*1400)

    def test_barcode_length_limit(self):
        self.assertEqual(len(parse_standard_json(json.dumps({'standard_barcode': 'x'*512}))), 512)
        with self.assertRaises(ValueError): parse_standard_json(json.dumps({'standard_barcode':'x'*513}))

    def test_shipped_example_matches_template(self):
        path=Path(__file__).resolve().parents[1]/'examples'/'standard-barcode.example.json'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), EXAMPLE_STANDARD)


@unittest.skipUnless(WEB, 'Install Web test dependencies')
class StandardJsonApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password='Json-test-only-123456'
        cls.credentials=credential_record('admin',cls.password)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);creds=root/'admin.json';creds.write_text(json.dumps(self.credentials))
        self.origin='http://localhost:9080'
        self.config=WebConfig(data_dir=root/'data',credentials=creds,public_url=self.origin,tcp_port=0,auto_backup=False)
        self.app=create_app(self.config)
        self.client=TestClient(self.app,base_url=self.origin);self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        reply=self.client.post('/api/auth/login',json={'username':'admin','password':self.password},headers={'Origin':self.origin})
        self.assertEqual(reply.status_code,200)
        self.headers={'Origin':self.origin,'X-CSRF-Token':reply.json()['csrf']}
        self.store=self.app.state.runtime.store

    def post(self, content, revision=0, request_id=None):
        return self.client.post('/api/admin/standard-barcode/import',json={'content':content,'revision':revision},
                                headers={**self.headers,'X-Request-ID':request_id or uuid.uuid4().hex})

    def test_import_persists_exact_standard(self):
        response=self.post('{"standard_barcode":"001-AbC"}')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.store.standard()['barcode'],'001-AbC')
        with self.store.reader() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM standards').fetchone()[0],2)

    def test_invalid_import_never_changes_existing_standard(self):
        self.post(json.dumps(EXAMPLE_STANDARD))
        for content in ('{}','{"standard_barcode":12}','{"standard_barcode":"a","standard_barcode":"b"}'):
            with self.subTest(content=content):
                self.assertEqual(self.post(content,1).status_code,400)
                self.assertEqual(self.store.standard()['barcode'],'001234-AbC')
                self.assertEqual(self.store.standard()['revision'],1)

    def test_bom_import_and_explicit_clear(self):
        self.assertEqual(self.post('\ufeff'+json.dumps(EXAMPLE_STANDARD)).status_code,200)
        self.assertEqual(self.post('{"standard_barcode":""}',1).status_code,200)
        self.assertEqual(self.store.standard()['barcode'],'')

    def test_stale_revision_rejected(self):
        self.post(json.dumps(EXAMPLE_STANDARD))
        self.assertEqual(self.post('{"standard_barcode":"other"}',0).status_code,409)
        self.assertEqual(self.store.standard()['barcode'],'001234-AbC')

    def test_duplicate_request_is_idempotent(self):
        rid=uuid.uuid4().hex;content=json.dumps(EXAMPLE_STANDARD)
        first=self.post(content,request_id=rid);second=self.post(content,request_id=rid)
        self.assertEqual(first.json()['revision'],second.json()['revision'])
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(self.post('{"standard_barcode":"other"}',request_id=rid).status_code,409)

    def test_invalid_revision_and_request_id(self):
        for revision in (-1,True,'0',None):
            with self.subTest(revision=revision):
                self.assertEqual(self.post(json.dumps(EXAMPLE_STANDARD),revision).status_code,400)
        self.assertEqual(self.post(json.dumps(EXAMPLE_STANDARD),request_id='short').status_code,400)
        self.assertEqual(self.store.standard()['revision'],0)

    def test_authentication_and_csrf(self):
        payload={'content':json.dumps(EXAMPLE_STANDARD),'revision':0}
        self.assertEqual(self.client.post('/api/admin/standard-barcode/import',json=payload,
                                         headers={'Origin':self.origin}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.post('/api/admin/standard-barcode/import',json=payload,
                                         headers=self.headers).status_code,401)
        self.assertEqual(self.client.get('/api/admin/standard-barcode/template').status_code,401)

    def test_wrong_origin_and_oversize(self):
        payload={'content':json.dumps(EXAMPLE_STANDARD),'revision':0}
        self.assertEqual(self.client.post('/api/admin/standard-barcode/import',json=payload,
                            headers={**self.headers,'Origin':'https://invalid.example'}).status_code,403)
        self.assertEqual(self.post(' '*4097).status_code,400)
        # Import now has a larger, route-specific budget for batch JSON.
        self.assertEqual(self.post(' '*20000).status_code,400)
        self.assertEqual(self.post(' '*(2*1024*1024)).status_code,413)

    def test_unknown_payload_keys_rejected(self):
        self.assertEqual(self.client.post('/api/admin/standard-barcode/import',
            json={'content':json.dumps(EXAMPLE_STANDARD),'revision':0,'path':'/etc/passwd'},
            headers=self.headers).status_code,400)
        self.assertEqual(self.store.standard()['revision'],0)

    def test_template_contains_example_not_current_standard(self):
        self.post('{"standard_barcode":"PRIVATE-STANDARD"}')
        response=self.client.get('/api/admin/standard-barcode/template')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),EXAMPLE_STANDARD)
        self.assertIn('attachment;',response.headers['content-disposition'])
        self.assertNotIn('PRIVATE-STANDARD',response.text)

    def test_history_snapshot_not_rejudged_and_restarts_persist(self):
        self.post(json.dumps(EXAMPLE_STANDARD))
        row,_=self.store.record({'v':2,'type':'result','msg_id':'sample1','project':'packaging',
            'station':1,'worker_id':'001','barcode':'001234-AbC','logo':'OK','flame':'NG'},utc_now())
        self.post('{"standard_barcode":"changed"}',1)
        self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'MATCH')
        self.assertEqual(self.store.latest('packaging',1)['standard_barcode'],row['standard_barcode'])
        self.app.state.runtime.close()
        restored=StationStore(self.config.data_dir/'station-results.sqlite3')
        try: self.assertEqual(restored.standard()['barcode'],'changed')
        finally: restored.close()

    def test_import_changes_only_barcode_and_never_pushes_to_tcp(self):
        # Four live sockets remain connected; standard changes produce no unsolicited frames.
        peers=[]
        try:
            for project in ('screw','packaging'):
                for number in (1,2):
                    sock=socket.create_connection(self.app.state.runtime.tcp.address,timeout=2)
                    stream=sock.makefile('rb');peers.append((sock,stream))
                    sock.sendall(encode({'v':2,'type':'hello','msg_id':uuid.uuid4().hex,'project':project,'station':number}))
                    self.assertEqual(decode(stream.readline()[:-1])['type'],'hello_ok')
            self.post(json.dumps(EXAMPLE_STANDARD))
            for i,(sock,stream) in enumerate(peers):
                project='screw' if i<2 else 'packaging'
                fields={'screw_count':4} if i<2 else {'barcode':'001234-AbC','logo':'OK','flame':'NG'}
                sock.sendall(encode({'v':2,'type':'result','msg_id':uuid.uuid4().hex,'project':project,'station':i%2+1,'worker_id':'00123',**fields}))
                ack=decode(stream.readline()[:-1]);self.assertEqual(ack['type'],'result_ack')
                self.assertNotIn('standard_barcode',ack)
            self.assertEqual(self.store.latest('packaging',1)['barcode_status'],'MATCH')
            self.assertEqual(self.store.latest('packaging',1)['flame'],'NG')
            self.assertEqual(self.store.latest('screw',2)['screw_count'],4)
            public=self.client.get('/api/display?project=packaging').json()
            self.assertNotIn('standard',public)
        finally:
            for sock,stream in peers: stream.close();sock.close()

    def test_legacy_save_endpoint_remains_compatible(self):
        response=self.client.post('/api/admin/standard-barcode',json={'barcode':'direct','revision':0},
                                 headers={**self.headers,'X-Request-ID':uuid.uuid4().hex})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.post(json.dumps(EXAMPLE_STANDARD),1).status_code,200)


if __name__ == '__main__':
    unittest.main()
