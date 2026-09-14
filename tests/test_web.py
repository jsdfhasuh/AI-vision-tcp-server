"""Web v1.3 tests; desktop-only environments skip these optional-dependency tests."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from competition.web_auth import Auth, COOKIE, LoginLimited, credential_record
from competition.web_config import WebConfig
from competition.headless import Runtime, ControlError
from competition.dbtools import restore_backup, reader
from competition import protocol
from test_system import FakePeer
from vision_client import VisionClient

try:
    from fastapi.testclient import TestClient
    from competition.webapp import create_app
    WEB = True
except ImportError:
    WEB = False

ORIGIN = 'http://localhost:9080'
PASSWORD = 'Test-only-Password-12345'
CREDENTIAL = credential_record('admin',PASSWORD)


class WebConfigTests(unittest.TestCase):
    def test_nonlocal_http_requires_opt_in(self):
        with self.assertRaises(ValueError):
            WebConfig(public_url='http://example.com')

    def test_https_configuration(self):
        c=WebConfig(public_url='https://vision.example.com/')
        self.assertEqual(c.origin,'https://vision.example.com')
        self.assertTrue(c.secure_cookie)

    def test_bad_origins_rejected(self):
        for u in ['https://x/admin','https://a:b@x','https://x?y=1','https://x#f','https://x:bad','file:///a']:
            with self.subTest(u=u), self.assertRaises(ValueError): WebConfig(public_url=u)

    def test_private_http_explicit(self):
        self.assertFalse(WebConfig(public_url='http://192.168.1.2:9080',allow_insecure_http=True).secure_cookie)

    def test_missing_credentials_fails_closed(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError): Auth(Path(d)/'missing.json')

    def test_hash_salts_differ(self):
        other=credential_record('admin',PASSWORD)
        self.assertNotEqual(other['salt'],CREDENTIAL['salt'])
        self.assertNotEqual(other['password_hash'],CREDENTIAL['password_hash'])
        self.assertNotIn(PASSWORD,json.dumps(other))

    def test_default_short_password_rejected(self):
        with self.assertRaises(ValueError): credential_record('admin','admin')

    def test_expired_browser_session(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'auth.json';p.write_text(json.dumps(CREDENTIAL));a=Auth(p,lifetime=0)
            token,_=a.login('admin',PASSWORD,'test');self.assertIsNone(a.get(token))


class HeadlessLifecycleTests(unittest.TestCase):
    def test_global_login_limit_does_not_allocate_unbounded_sources(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'admin.json'
            path.write_text(json.dumps(CREDENTIAL))
            auth = Auth(path)
            for n in range(20):
                auth._limit(f'source-{n}')
            for n in range(200):
                with self.assertRaises(LoginLimited):
                    auth._limit(f'blocked-{n}')
            self.assertEqual(len(auth.attempts), 20)

    def test_shutdown_releases_database_after_session_write_failure(self):
        from competition.storage import Store
        with tempfile.TemporaryDirectory() as d:
            config = WebConfig(data_dir=Path(d) / 'data', tcp_port=0, auto_backup=False)
            runtime = Runtime(config)
            with patch.object(runtime.engine, 'close_session', side_effect=sqlite3.OperationalError('test disk failure')):
                with self.assertLogs('competition.headless', level='ERROR'):
                    runtime.close()
            self.assertTrue(runtime.jobs.closed)
            self.assertFalse(runtime.cache.thread.is_alive())
            # Also checks the process owner lock was released, not just sockets.
            store = Store(config.data_dir / 'competition.sqlite3')
            store.close()
            runtime.close()  # shutdown is idempotent


@unittest.skipUnless(WEB,'Install requirements-web.txt and httpx for Web tests')
class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.credential=self.root/'admin.json';self.credential.write_text(json.dumps(CREDENTIAL))
        self.config=WebConfig(data_dir=self.root/'data',credentials=self.credential,public_url=ORIGIN,tcp_port=0,auto_backup=False)
        self.app=create_app(self.config)
        self.client=TestClient(self.app,base_url=ORIGIN)
        self.client.__enter__()
        self.runtime=self.app.state.runtime
        self.csrf=''

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def login(self):
        r=self.client.post('/api/auth/login',json={'username':'admin','password':PASSWORD},headers={'Origin':ORIGIN})
        self.assertEqual(r.status_code,200,r.text);self.csrf=r.json()['csrf'];return r

    def state(self):
        r=self.client.get('/api/admin/state');self.assertEqual(r.status_code,200,r.text);return r.json()

    def cmd(self,name,d=None,*,token=None,request_id=None):
        payload=dict(d or {})
        if name not in {'backup','export','check'}:
            payload['state_token']=token if token is not None else self.state()['state_token']
        return self.client.post('/api/admin/commands/'+name,json=payload,headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf,'X-Request-ID':request_id or uuid.uuid4().hex})

    def session(self,competition='packaging',mode='PRACTICE'):
        r=self.cmd('new_session',{'team':'网页测试队','client_id':'vision-01','competition':competition,'mode':mode})
        self.assertEqual(r.status_code,200,r.text);return r.json()['session_id']

    def peer(self):
        p=FakePeer();e=self.runtime.engine
        e.receive(p,protocol.encode({'v':1,'type':'hello','msg_id':uuid.uuid4().hex,'client_id':'vision-01','access_code':e.session['access_code']}))
        e.receive(p,protocol.encode({'v':1,'type':'target_ack','msg_id':uuid.uuid4().hex,'session_id':e.session_id,'target_revision':e.session['target_revision']}))
        return p

    def start(self,**extra):
        r=self.cmd('start_round',{'case_name':'合格品','expected':'OK','timeout_ms':0,'action':'arm',**extra})
        self.assertEqual(r.status_code,200,r.text);return r.json()['round_id']

    def wait_job(self,jid):
        end=time.monotonic()+6
        while time.monotonic()<end:
            r=self.client.get('/api/admin/jobs/'+jid).json()
            if r['status'] in {'DONE','FAILED'}:
                self.assertEqual(r['status'],'DONE',r);return r
            time.sleep(.02)
        self.fail('job timeout')

    def test_static_board_and_login_work_without_auth(self):
        for path in ['/admin/','/admin/admin.css','/admin/admin.js','/board/','/app.js','/style.css','/favicon.svg','/api/display','/healthz']:
            self.assertEqual(self.client.get(path).status_code,200,path)

    def test_private_routes_need_auth(self):
        for path in ['/api/admin/state','/api/admin/access-code','/api/admin/sessions','/api/admin/jobs','/api/auth/me']:
            self.assertEqual(self.client.get(path).status_code,401,path)

    def test_login_cookie_flags(self):
        cookie=self.login().headers['set-cookie']
        self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=strict',cookie)
        self.assertNotIn('Secure;',cookie)

    def test_secure_cookie_when_https(self):
        c=replace(self.config,public_url='https://vision.example.com',data_dir=self.root/'https-data')
        with TestClient(create_app(c),base_url=c.origin) as other:
            r=other.post('/api/auth/login',json={'username':'admin','password':PASSWORD},headers={'Origin':c.origin})
            self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['set-cookie'])
            self.assertEqual(other.get('/api/admin/state').status_code,200)
            self.assertEqual(other.get('/admin',follow_redirects=False).headers['location'],'/admin/')

    def test_wrong_password_and_attempt_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':'incorrect'},headers={'Origin':ORIGIN}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':PASSWORD},headers={'Origin':ORIGIN}).status_code,429)

    def test_logout_revokes_cookie(self):
        self.login();cookie=self.client.cookies.get(COOKIE)
        r=self.client.post('/api/auth/logout',json={},headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf})
        self.assertEqual(r.status_code,200)
        self.client.cookies.set(COOKIE,cookie)
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)

    def test_forged_cookie_rejected(self):
        self.client.cookies.set(COOKIE,'anything')
        self.assertEqual(self.client.get('/api/admin/state').status_code,401)

    def test_csrf_required_for_mutation(self):
        self.login()
        r=self.client.post('/api/admin/commands/backup',json={},headers={'Origin':ORIGIN})
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.runtime.jobs.list(),[])

    def test_origin_required_for_login(self):
        for origin in [None,'https://evil.example','null']:
            h={} if origin is None else {'Origin':origin}
            self.assertEqual(self.client.post('/api/auth/login',json={'username':'admin','password':PASSWORD},headers=h).status_code,403)

    def test_host_header_rejected(self):
        self.assertEqual(self.client.get('/admin/',headers={'Host':'evil.example'}).status_code,400)

    def test_json_required(self):
        self.assertEqual(self.client.post('/api/auth/login',data={'username':'admin','password':PASSWORD},headers={'Origin':ORIGIN}).status_code,415)

    def test_request_body_limit(self):
        self.assertEqual(self.client.post('/api/auth/login',content=b'x'*17000,headers={'Origin':ORIGIN,'Content-Type':'application/json'}).status_code,413)

    def test_bad_json_does_not_echo_password(self):
        raw='{"password":"'+PASSWORD+'",'
        r=self.client.post('/api/auth/login',content=raw,headers={'Origin':ORIGIN,'Content-Type':'application/json'})
        self.assertEqual(r.status_code,400);self.assertNotIn(PASSWORD,r.text)

    def test_security_headers(self):
        for path in ['/admin/','/api/display','/missing']:
            r=self.client.get(path)
            self.assertEqual(r.headers['x-frame-options'],'DENY')
            self.assertIn("script-src 'self'",r.headers['content-security-policy'])
            self.assertIn('no-store',r.headers['cache-control'])

    def test_static_paths_cannot_read_database_or_credentials(self):
        for path in ['/data/competition.sqlite3','/secrets/admin.json','/admin/../storage.py','/openapi.json','/docs']:
            r=self.client.get(path);self.assertEqual(r.status_code,404,path)

    def test_public_route_is_read_only(self):
        self.login()
        self.assertEqual(self.client.post('/api/display',json={},headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf}).status_code,405)
        self.assertEqual(self.client.delete('/api/display').status_code,405)

    def test_requires_request_id(self):
        self.login()
        r=self.client.post('/api/admin/commands/backup',json={},headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf})
        self.assertEqual(r.status_code,400)

    def test_new_official_session_persists(self):
        self.login();sid=self.session(mode='OFFICIAL')
        self.assertEqual(self.state()['session']['mode'],'OFFICIAL')
        r=self.client.get('/api/admin/sessions?mode=OFFICIAL').json()
        self.assertEqual(r['total'],1);self.assertEqual(r['sessions'][0]['id'],sid)

    def test_access_code_is_explicit_private_read(self):
        self.login();self.session()
        self.assertNotIn('access_code',json.dumps(self.state()))
        code=self.client.get('/api/admin/access-code').json()['access_code']
        self.assertEqual(code,self.runtime.engine.session['access_code'])

    def test_new_session_idempotent_replay(self):
        self.login();mid=uuid.uuid4().hex;token=self.state()['state_token']
        d={'team':'幂等队','client_id':'vision-01','competition':'screw','mode':'PRACTICE'}
        a=self.cmd('new_session',d,token=token,request_id=mid)
        b=self.cmd('new_session',d,token=token,request_id=mid)
        self.assertEqual(a.json()['session_id'],b.json()['session_id']);self.assertTrue(b.json()['replayed'])
        self.assertEqual(self.runtime.store.search_sessions()['total'],1)

    def test_same_request_id_different_content_rejected(self):
        self.login();mid=uuid.uuid4().hex;token=self.state()['state_token']
        d={'team':'队伍A','client_id':'vision-01','competition':'screw','mode':'PRACTICE'}
        self.assertEqual(self.cmd('new_session',d,token=token,request_id=mid).status_code,200)
        d['team']='队伍B'
        self.assertEqual(self.cmd('new_session',d,token=token,request_id=mid).json()['error']['code'],'REQUEST_CONFLICT')

    def test_stale_tab_cannot_create_or_cancel(self):
        self.login();old=self.state()['state_token'];self.session()
        r=self.cmd('new_session',{'team':'旧标签页','client_id':'vision-01','competition':'screw','mode':'PRACTICE'},token=old)
        self.assertEqual(r.json()['error']['code'],'STATE_CHANGED')
        self.peer();self.start();old=self.state()['state_token']
        self.assertEqual(self.cmd('cancel_round',{'reason':'第一次'}).status_code,200)
        next_rid=self.start()
        r=self.cmd('cancel_round',{'reason':'旧页面取消'},token=old)
        self.assertEqual(r.status_code,409);self.assertEqual(self.runtime.engine.active_id,next_rid)

    def test_start_without_client_rejected(self):
        self.login();self.session()
        r=self.cmd('start_round',{'case_name':'合格','expected':'OK'})
        self.assertEqual(r.status_code,400);self.assertIsNone(self.runtime.engine.active_id)

    def test_target_requires_reack(self):
        self.login();self.session();self.peer()
        self.assertTrue(self.state()['ready'])
        self.assertEqual(self.cmd('target',{'box_type':'BOX_B'}).status_code,200)
        self.assertFalse(self.state()['ready']);self.assertEqual(self.state()['session']['target_revision'],2)

    def test_active_round_blocks_session_end(self):
        self.login();self.session();self.peer();self.start()
        self.assertEqual(self.cmd('end_session').status_code,409)
        self.assertIsNotNone(self.runtime.engine.active_id)

    def test_result_ng_can_be_correct(self):
        self.login();sid=self.session('screw');p=self.peer();rid=self.start(case_name='2号漏打',expected='NG')
        e=self.runtime.engine
        e.receive(p,protocol.encode({'v':1,'type':'result','msg_id':uuid.uuid4().hex,'session_id':sid,'round_id':rid,'target_revision':1,'verdict':'NG'}))
        row=self.state()['rows'][0]
        self.assertEqual(row['actual'],'NG');self.assertEqual(row['matched'],1)
        self.assertNotIn('expected',json.dumps(p.sent[-1]));self.assertTrue(p.sent[-1]['recorded'])

    def test_public_snapshot_never_leaks_private_fields(self):
        self.login();self.session();self.peer();self.start(case_name='PRIVATE_CASE',notes='PRIVATE_NOTE')
        code=self.runtime.engine.session['access_code'];self.runtime.cache.refresh()
        raw=self.client.get('/api/display').text
        for v in ['access_code','expected','matched','PRIVATE_CASE','PRIVATE_NOTE',code,PASSWORD]:
            self.assertNotIn(v,raw)

    def test_cancel_is_not_ng_and_end_disconnects(self):
        self.login();self.session();p=self.peer();self.start()
        self.assertEqual(self.cmd('cancel_round',{'reason':'样件需要重新放置'}).status_code,200)
        self.assertEqual(self.state()['rows'][0]['status'],'CANCELLED');self.assertIsNone(self.state()['rows'][0]['actual'])
        self.assertEqual(self.cmd('end_session').status_code,200)
        self.assertFalse(p.alive);self.assertIsNone(self.state()['session'])

    def test_timeout_is_not_ng(self):
        self.login();self.session();self.peer();self.start(timeout_ms=1)
        time.sleep(.03);self.runtime.engine.tick()
        self.assertEqual(self.state()['rows'][0]['status'],'TIMEOUT');self.assertIsNone(self.state()['rows'][0]['actual'])

    def test_unknown_fields_and_wrong_types(self):
        self.login()
        self.assertEqual(self.cmd('new_session',{'team':{},'client_id':'x','competition':'screw','mode':'PRACTICE'}).status_code,400)
        self.assertEqual(self.cmd('backup',{'path':'/etc/passwd'}).status_code,400)
        self.assertEqual(self.cmd('restore',{'path':'/data'}).status_code,400)

    def test_surrogate_input_is_bad_request(self):
        self.login()
        data={'team':'\ud800','client_id':'x','competition':'screw','mode':'PRACTICE','state_token':self.state()['state_token']}
        r=self.client.post('/api/admin/commands/new_session',content=json.dumps(data,ensure_ascii=True),headers={'Origin':ORIGIN,'Content-Type':'application/json','X-CSRF-Token':self.csrf,'X-Request-ID':uuid.uuid4().hex})
        self.assertEqual(r.status_code,400)

    def test_history_keeps_private_mode_and_readonly(self):
        self.login();sid=self.session();self.peer();self.start();self.cmd('cancel_round',{'reason':'测试'})
        self.session('screw',mode='OFFICIAL')
        d=self.client.get('/api/admin/sessions/'+sid).json()
        self.assertEqual(d['session']['mode'],'PRACTICE');self.assertEqual(d['session']['state'],'CLOSED')
        self.assertNotIn('access_code',d['session']);self.assertEqual(d['stats']['total'],1)
        self.assertEqual(self.client.get('/api/admin/sessions?mode=OFFICIAL').json()['total'],1)

    def test_history_invalid_id_and_page(self):
        self.login();sid=self.session()
        for path in ['/api/admin/sessions/nonsense','/api/admin/sessions?limit=10000','/api/admin/sessions?date_from=nope',f'/api/admin/sessions/{sid}?offset=-1']:
            self.assertEqual(self.client.get(path).status_code,400,path)

    def test_branding_persists(self):
        self.login();r=self.cmd('branding',{'title':'工业视觉联调','subtitle':'Web部署测试'})
        self.assertEqual(r.status_code,200);self.runtime.cache.refresh()
        self.assertEqual(self.client.get('/api/display').json()['data']['title'],'工业视觉联调')
        self.assertEqual(self.runtime.store.load_settings()['display_title'],'工业视觉联调')

    def test_backup_download_requires_login_and_restores(self):
        self.login();self.session();p=self.peer();rid=self.start()
        self.cmd('cancel_round',{'reason':'用于备份测试'})
        j=self.cmd('backup').json()['job'];done=self.wait_job(j['id'])
        res=self.client.get(f"/api/admin/jobs/{j['id']}/download")
        self.assertEqual(res.status_code,200);self.assertIn('attachment',res.headers['content-disposition'])
        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
            self.assertEqual(set(z.namelist()),{'database.sqlite3','manifest.json'})
            self.assertNotIn(PASSWORD,z.read('manifest.json').decode())
            folder=self.root/'restore-source';z.extractall(folder)
        target=restore_backup(folder,self.root/'restored');self.assertTrue(target.is_file())
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f"/api/admin/jobs/{j['id']}/download").status_code,401)

    def test_export_and_integrity_jobs(self):
        self.login();sid=self.session()
        j=self.cmd('export',{'session_id':sid}).json()['job'];self.wait_job(j['id'])
        with zipfile.ZipFile(io.BytesIO(self.client.get(f"/api/admin/jobs/{j['id']}/download").content)) as z:
            self.assertEqual(set(z.namelist()),{'rounds.csv','summary.json','audit.jsonl','SHA256.json'})
        j=self.cmd('check').json()['job'];d=self.wait_job(j['id']);self.assertTrue(d['result']['integrity_ok'])

    def test_missing_job_and_missing_session(self):
        self.login()
        self.assertEqual(self.client.get('/api/admin/jobs/'+uuid.uuid4().hex).status_code,404)
        self.assertEqual(self.cmd('export',{'session_id':uuid.uuid4().hex}).status_code,404)

    def test_slow_export_does_not_block_result(self):
        self.login();sid=self.session();p=self.peer();rid=self.start()
        entered=threading.Event();release=threading.Event();original=self.runtime.store.export
        def slow(*args,**kw):
            entered.set();release.wait(4);return original(*args,**kw)
        with patch.object(self.runtime.store,'export',slow):
            j=self.cmd('export',{'session_id':sid}).json()['job']
            self.assertTrue(entered.wait(2))
            self.runtime.engine.receive(p,protocol.encode({'v':1,'type':'result','msg_id':uuid.uuid4().hex,'session_id':sid,'round_id':rid,'target_revision':1,'verdict':'OK'}))
            self.assertEqual(self.state()['rows'][0]['actual'],'OK')
            self.assertFalse(release.is_set());release.set();self.wait_job(j['id'])

    def test_listener_control_and_engine_process_lock(self):
        self.login()
        self.assertEqual(self.cmd('stop_listener').status_code,200);self.assertEqual(self.state()['listening'],'未监听')
        self.assertEqual(self.cmd('start_listener').status_code,200)
        with self.assertRaises(RuntimeError): Runtime(self.config)

    def test_no_password_or_cookie_in_database(self):
        r=self.login();cookie=self.client.cookies.get(COOKIE);self.session()
        with reader(self.runtime.store.path) as db:
            logs=' '.join(row[0] for row in db.execute('SELECT text FROM audit'))
        for v in [PASSWORD,cookie,self.csrf,CREDENTIAL['password_hash']]:self.assertNotIn(v,logs)

    def test_real_tcp_two_competitions_ten_rounds(self):
        self.login()
        for competition in ('packaging','screw'):
            sid=self.session(competition)
            code=self.runtime.engine.session['access_code'];host,port=self.runtime.tcp.address
            with VisionClient(host,port,'vision-01',code) as client:
                hello=client.connect();client.acknowledge_target(hello['target_revision'])
                deadline=time.monotonic()+2
                while not self.state()['ready'] and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(self.state()['ready'])
                for i in range(10):
                    value='OK' if i%2==0 else 'NG';rid=self.start(expected=value)
                    frame=client.receive(2)
                    while frame and frame['type']!='round':frame=client.receive(2)
                    self.assertIsNotNone(frame);self.assertEqual(frame['round_id'],rid)
                    client.send(client.make_result(frame,value));ack=client.receive(2)
                    self.assertEqual(ack['type'],'result_ack');self.assertTrue(ack['recorded'])
                self.assertEqual(self.state()['stats']['matched'],10)
                self.assertEqual(self.state()['stats']['received'],10)
            self.assertEqual(self.cmd('end_session').status_code,200)
