"""Real HTTP/TCP process integration and crash/restart verification.

Temporary synthetic PRACTICE data only; does not require a GUI or a browser.
Run: python tests/web_http_smoke.py --output-dir /tmp/web-http-smoke
"""
from __future__ import annotations
import argparse, io, json, os, signal, socket, subprocess, sys, tempfile, time, uuid, zipfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from competition.web_auth import credential_record
ROOT=Path(__file__).resolve().parents[1]


def freeport():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True);args=p.parse_args()
    out=args.output_dir;out.mkdir(parents=True,exist_ok=True);report={'transport':'native HTTP and independent TCP mock subprocesses','rounds':{}}
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);credentials=root/'admin.json';password='HTTP-Smoke-Only-12345'
        credentials.write_text(json.dumps(credential_record('admin',password)))
        wp,tp=freeport(),freeport();origin=f'http://127.0.0.1:{wp}'
        log=(out/'server.log').open('w');mlog=(out/'mock.log').open('w')
        proc=None;mock=None
        def boot():
            process=subprocess.Popen([sys.executable,'web_server.py','--data-dir',str(root/'data'),'--credentials',str(credentials),'--public-url',origin,'--host','127.0.0.1','--port',str(wp),'--tcp-host','127.0.0.1','--tcp-port',str(tp)],cwd=ROOT,env={**os.environ,'AUTO_BACKUP':'0'},stdout=log,stderr=log)
            for _ in range(80):
                try:
                    if httpx.get(origin+'/healthz',timeout=.3).status_code==200:return process
                except httpx.HTTPError:pass
                if process.poll() is not None:raise RuntimeError('process exited')
                time.sleep(.1)
            raise RuntimeError('startup timed out')
        try:
            proc=boot()
            with httpx.Client(base_url=origin,timeout=5,headers={'Origin':origin}) as c:
                def login():
                    r=c.post('/api/auth/login',json={'username':'admin','password':password});r.raise_for_status();c.headers['X-CSRF-Token']=r.json()['csrf']
                def state():
                    r=c.get('/api/admin/state');r.raise_for_status();return r.json()
                def cmd(name,d=None):
                    data=dict(d or {})
                    if name not in ('backup','export','check'):data['state_token']=state()['state_token']
                    r=c.post('/api/admin/commands/'+name,json=data,headers={'X-Request-ID':uuid.uuid4().hex});r.raise_for_status();return r.json()
                def await_state(fn):
                    until=time.monotonic()+6
                    while time.monotonic()<until:
                        s=state()
                        if fn(s):return s
                        time.sleep(.03)
                    raise RuntimeError('state timeout')
                login()
                for competition in ('packaging','screw'):
                    sid=cmd('new_session',{'team':'HTTP联调演示队','client_id':'vision-01','competition':competition,'mode':'PRACTICE'})['session_id']
                    code=c.get('/api/admin/access-code').json()['access_code']
                    mock=subprocess.Popen([sys.executable,'client_example.py','--host','127.0.0.1','--port',str(tp),'--client-id','vision-01','--access-code',code,'--delay','0.02'],cwd=ROOT,stdout=mlog,stderr=mlog)
                    await_state(lambda s:s['ready'])
                    for i in range(10):
                        cmd('start_round',{'case_name':'合格品' if i%2==0 else '缺陷品','expected':'OK' if i%2==0 else 'NG','action':'trigger','timeout_ms':0})
                        s=await_state(lambda s:s['stats']['received']==i+1)
                    assert s['stats']['matched']==10
                    report['rounds'][competition]={'received':s['stats']['received'],'matched':s['stats']['matched']}
                    time.sleep(.4);public=c.get('/api/display').text
                    for private in ('"expected"','"matched"','"access_code"',code):assert private not in public
                    mock.terminate();mock.wait(5);mock=None
                    cmd('end_session')
                job=cmd('backup')['job']['id']
                for _ in range(200):
                    j=c.get('/api/admin/jobs/'+job).json()
                    if j['status']=='DONE':break
                    if j['status']=='FAILED':raise RuntimeError(j)
                    time.sleep(.03)
                else:raise RuntimeError('backup timeout')
                archive=c.get('/api/admin/jobs/'+job+'/download');archive.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(archive.content)) as z:assert set(z.namelist())=={'database.sqlite3','manifest.json'}
                report['private_backup_download']=True
                sid=cmd('new_session',{'team':'异常退出联调','client_id':'vision-01','competition':'screw','mode':'PRACTICE'})['session_id']
                code=c.get('/api/admin/access-code').json()['access_code']
                mock=subprocess.Popen([sys.executable,'client_example.py','--host','127.0.0.1','--port',str(tp),'--client-id','vision-01','--access-code',code,'--delay','30'],cwd=ROOT,stdout=mlog,stderr=mlog)
                await_state(lambda s:s['ready']);cmd('start_round',{'case_name':'异常退出验证','expected':'OK'})
                proc.kill();proc.wait(5);mock.terminate();mock.wait(5);mock=None
                proc=boot()
                assert c.get('/api/admin/state').status_code==401
                login();assert state()['session'] is None
                h=c.get('/api/admin/sessions/'+sid).json()
                assert h['session']['state']=='INTERRUPTED' and h['rows'][0]['status']=='INTERRUPTED' and h['rows'][0]['actual'] is None
                report['crash_recovery_no_timer_resume']=True;report['old_cookie_revoked_after_restart']=True
                proc.terminate();proc.wait(20)
                assert proc.returncode in (0,-signal.SIGTERM)
                report['shutdown_exit_code']=proc.returncode
            report['passed']=True
        finally:
            if mock and mock.poll() is None:mock.terminate();mock.wait(5)
            if proc and proc.poll() is None:proc.terminate();proc.wait(20)
            log.close();mlog.close()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
