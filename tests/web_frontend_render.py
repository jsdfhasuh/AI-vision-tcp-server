"""Offline fixture rendering only, NOT a browser-to-server integration test.

No browser network access is used. Useful where enterprise policy prohibits
browser access to local test servers. API/protocol tests are separate.
"""
from __future__ import annotations
import argparse, json, re, shutil, sys, tempfile, time, uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from competition.headless import Runtime
from competition.web_config import WebConfig
from competition import protocol
from test_system import FakePeer
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True);args=p.parse_args()
    out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
    report={'mode':'OFFLINE FIXTURE RENDER ONLY','network_access':False,'errors':[],'viewports':[]}
    with tempfile.TemporaryDirectory() as td:
        runtime=Runtime(WebConfig(data_dir=Path(td)/'data',tcp_port=0,auto_backup=False))
        try:
            e=runtime.engine;s=e.new_session('演示队伍 · 非正式比赛','vision-01','packaging',mode='PRACTICE')
            peer=FakePeer()
            e.receive(peer,protocol.encode({'v':1,'type':'hello','msg_id':'render-hello','client_id':'vision-01','access_code':s['access_code']}))
            e.receive(peer,protocol.encode({'v':1,'type':'target_ack','msg_id':'render-target','session_id':s['id'],'target_revision':1}))
            for i in range(6):
                expected='OK' if i%2==0 else 'NG'
                rid=e.start_round('合格品' if i%2==0 else '正面LOGO破损',expected)
                e.receive(peer,protocol.encode({'v':1,'type':'result','msg_id':uuid.uuid4().hex,'session_id':s['id'],'round_id':rid,'target_revision':1,'verdict':expected}))
            runtime.cache.refresh()
            state=runtime.state();history=runtime.store.search_sessions();detail=runtime.history(s['id']);db=runtime.store.health()
            fixture={'state':state,'history':history,'detail':detail,'public':runtime.cache.read(),'health':db}
        finally:runtime.close()
        with sync_playwright() as pw:
            b=pw.chromium.launch(executable_path=shutil.which('chromium') or None,headless=True,args=['--no-sandbox'])
            page=b.new_page(viewport={'width':1440,'height':1050},locale='zh-CN');page.on('pageerror',lambda e:report['errors'].append(str(e)))
            html=(ROOT/'competition/admin/index.html').read_text();html=re.sub(r'<link[^>]*>|<script[^>]*></script>','',html)
            page.set_content(html);page.add_style_tag(content=(ROOT/'competition/admin/admin.css').read_text())
            page.evaluate('window.__fixture = '+json.dumps(fixture,ensure_ascii=True))
            page.evaluate("""() => {
              let authenticated=false;
              window.fetch=async (path,opts={})=>{
                let d={},status=200;const f=window.__fixture;
                if(path==='/api/auth/me'&&!authenticated){status=401;d={error:{code:'LOGIN_REQUIRED',message:'fixture login'}};}
                else if(path==='/api/auth/me'||path==='/api/auth/login'){authenticated=true;d={username:'admin',csrf:'OFFLINE_FIXTURE_ONLY',version:'1.3.0-web'};}
                else if(path==='/api/admin/state'){d=structuredClone(f.state);d.generated_utc=new Date().toISOString();}
                else if(path.startsWith('/api/admin/sessions?'))d=f.history;
                else if(path.startsWith('/api/admin/sessions/'))d=f.detail;
                else if(path==='/api/admin/jobs')d={jobs:[{id:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',kind:'check',status:'DONE',created_utc:new Date().toISOString(),result:f.health,download_ready:false},{id:'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',kind:'backup',status:'DONE',created_utc:new Date().toISOString(),automatic:true,download_ready:false}]};
                else {status=404;d={error:{message:'offline rendering; mutation not simulated'}};}
                return new Response(JSON.stringify(d),{status,headers:{'Content-Type':'application/json'}});
              };
            }""")
            page.add_script_tag(content=(ROOT/'competition/admin/admin.js').read_text());page.wait_for_timeout(100)
            page.screenshot(path=str(out/'web_login.png'),full_page=True)
            page.locator('#login-form [name=password]').fill('Offline-Render-Only')
            page.locator('#login-form button').click();page.locator('#app-screen').wait_for(state='visible');page.wait_for_timeout(150)
            page.screenshot(path=str(out/'web_referee.png'),full_page=True)
            for tab in ['history','database','settings']:
                page.locator('[data-tab='+tab+']').click();page.wait_for_timeout(100)
                if tab=='history':page.locator('#history-rows button').click();page.wait_for_timeout(100)
                page.screenshot(path=str(out/f'web_{tab}.png'),full_page=True)
            page.locator('[data-tab=live]').click()
            for w,h in [(1920,1080),(1366,768),(1024,768),(390,844)]:
                page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(60)
                width=page.evaluate('document.documentElement.scrollWidth');fit=width<=w
                report['viewports'].append({'width':w,'height':h,'body_scroll_width':width,'fits':fit})
                assert fit,(w,width)
                if w==390:page.screenshot(path=str(out/'web_mobile.png'),full_page=True)
            assert page.locator('#login-form [name=password]').input_value()==''
            board=b.new_page(viewport={'width':1920,'height':1080},locale='zh-CN')
            board.on('pageerror',lambda e:report['errors'].append(str(e)))
            html=(ROOT/'competition/web/index.html').read_text();html=re.sub(r'<link[^>]*>|<script[^>]*></script>','',html)
            board.set_content(html);board.add_style_tag(content=(ROOT/'competition/web/style.css').read_text())
            board.evaluate('window.__snapshot = '+json.dumps(fixture['public'],ensure_ascii=True))
            board.evaluate("""() => {window.fetch=async()=>{const d=structuredClone(window.__snapshot);d.age_ms=0;d.data.generated_utc=new Date().toISOString();return new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}})}}""")
            board.add_script_tag(content=(ROOT/'competition/web/app.js').read_text());board.wait_for_timeout(150)
            board.screenshot(path=str(out/'web_board.png'))
            assert '正面LOGO破损' not in board.locator('body').inner_text()
            b.close()
    assert not report['errors'],report
    report['passed']=True;(out/'render_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
