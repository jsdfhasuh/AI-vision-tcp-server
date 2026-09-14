"""Native Uvicorn + Chromium + separate TCP mock process; temporary PRACTICE data.

Run from repo root: python tests/web_browser_smoke.py --output-dir /tmp/web-smoke
Needs httpx and Playwright with Chromium. Does not touch real competition data.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from competition.web_auth import credential_record
from playwright.sync_api import sync_playwright
import httpx

ROOT=Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    arg=argparse.ArgumentParser();arg.add_argument('--output-dir',type=Path,required=True);args=arg.parse_args()
    out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    findings={'demo_data':True,'http_direct':False,'browser_errors':[],'viewports':[]}
    with tempfile.TemporaryDirectory() as td:
        temp=Path(td);creds=temp/'admin.json';password='Browser-Smoke-Only-12345'
        creds.write_text(json.dumps(credential_record('admin',password)))
        wp,tp=port(),port();origin=f'http://127.0.0.1:{wp}'
        log=(out/'native_server.log').open('w');mocklog=(out/'mock_client.log').open('w')
        env={**os.environ,'AUTO_BACKUP':'0'}
        proc=subprocess.Popen([sys.executable,'web_server.py','--data-dir',str(temp/'data'),
                               '--credentials',str(creds),'--host','127.0.0.1','--port',str(wp),
                               '--tcp-host','127.0.0.1','--tcp-port',str(tp),'--public-url',origin],cwd=ROOT,env=env,stdout=log,stderr=log)
        mock=None
        try:
            for _ in range(100):
                try:
                    if httpx.get(origin+'/healthz',timeout=1).status_code==200:break
                except httpx.HTTPError:pass
                if proc.poll() is not None:raise RuntimeError('Web server exited; check native_server.log')
                time.sleep(.1)
            else:raise RuntimeError('Web startup timeout')
            findings['http_direct']=True
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=shutil.which('chromium') or None,headless=True,args=['--no-sandbox'])
                context=browser.new_context(viewport={'width':1440,'height':1050},locale='zh-CN',accept_downloads=True)
                page=context.new_page();page.on('pageerror',lambda e:findings['browser_errors'].append(str(e)))
                page.on('dialog',lambda d:d.accept('联调取消'))
                page.goto(origin+'/admin/',wait_until='networkidle')
                page.screenshot(path=str(out/'web_login.png'),full_page=True)
                page.locator('#login-form [name=username]').fill('admin')
                page.locator('#login-form [name=password]').fill(password)
                page.locator('#login-form button').click()
                page.locator('#app-screen').wait_for(state='visible')
                page.locator('#session-form [name=team]').fill('演示队伍 · 非正式比赛')
                page.locator('#new-session').click()
                page.wait_for_function("document.getElementById('current-team').textContent.includes('演示队伍')")
                page.locator('#reveal-code').click();page.wait_for_function("!document.getElementById('access-code').textContent.includes('•')")
                code=page.locator('#access-code').inner_text();page.locator('#reveal-code').click()
                mock=subprocess.Popen([sys.executable,'client_example.py','--host','127.0.0.1','--port',str(tp),'--client-id','vision-01','--access-code',code,'--delay','0.05'],cwd=ROOT,stdout=mocklog,stderr=mocklog)
                page.wait_for_function("document.getElementById('client-state').textContent==='参赛端已就绪'")
                for i,expected in enumerate(['OK','NG'],1):
                    page.locator('#round-form [name=expected]').select_option(expected)
                    page.locator('#round-form [name=case_name]').fill('合格品' if expected=='OK' else '正面LOGO破损')
                    page.locator('#start-round').click()
                    page.wait_for_function(f"document.getElementById('s-received').textContent==='{i}'")
                assert page.locator('#s-matched').inner_text()=='2'
                page.screenshot(path=str(out/'web_referee.png'),full_page=True)
                findings['browser_rounds']=2
                board=context.new_page();board.goto(origin+'/board/',wait_until='networkidle')
                board.wait_for_function("document.getElementById('result-value').textContent==='NG'")
                assert '正面LOGO破损' not in board.locator('body').inner_text()
                assert code not in board.locator('body').inner_text()
                board.set_viewport_size({'width':1920,'height':1080});board.screenshot(path=str(out/'web_board.png'))
                page.locator('[data-tab=history]').click()
                page.wait_for_selector('#history-rows button');page.locator('#history-rows button').first.click()
                page.wait_for_selector('#history-detail-rows tr');assert page.locator('#history-detail-rows tr').count()==2
                page.screenshot(path=str(out/'web_history.png'),full_page=True)
                page.locator('[data-tab=database]').click();page.locator('#db-check').click()
                page.wait_for_selector('#db-info-card:not([hidden])')
                page.locator('#db-backup').click();page.wait_for_selector('#jobs-list a.download')
                with page.expect_download() as info:page.locator('#jobs-list a.download').first.click()
                download=info.value;assert download.suggested_filename.endswith('.zip')
                findings['backup_browser_download']=True
                page.screenshot(path=str(out/'web_database.png'),full_page=True)
                page.locator('[data-tab=settings]').click();page.locator('#branding-form [name=title]').fill('工业视觉技能竞赛 · 联调')
                page.locator('#save-branding').click()
                board.wait_for_function("document.getElementById('event-title').textContent.includes('联调')")
                page.locator('[data-tab=live]').click()
                for w,h in [(1920,1080),(1366,768),(1024,768),(390,844)]:
                    page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(100)
                    width=page.evaluate('document.documentElement.scrollWidth')
                    findings['viewports'].append({'width':w,'height':h,'body_scroll_width':width,'fits':width<=w})
                    assert width<=w,(w,width)
                    if w==390:page.screenshot(path=str(out/'web_mobile.png'),full_page=True)
                findings['html_password_not_stored']=page.locator('#login-form [name=password]').input_value()==''
                # Graceful shutdown: a browser must visibly go offline, not fabricate NG.
                proc.terminate();proc.wait(timeout=20)
                page.wait_for_selector('#offline-alert:not([hidden])',timeout=15000)
                assert page.locator('#start-round').is_disabled()
                findings['offline_controls_disabled']=True
                findings['graceful_exit']=proc.returncode in (0, -15)
                context.close();browser.close()
            findings['passed']=not findings['browser_errors'] and findings['graceful_exit']
            assert findings['passed'], findings
        finally:
            if mock and mock.poll() is None:mock.terminate();mock.wait(timeout=5)
            if proc.poll() is None:proc.terminate();proc.wait(timeout=20)
            log.close();mocklog.close()
            (out/'browser_findings.json').write_text(json.dumps(findings,ensure_ascii=False,indent=2))
    print(json.dumps(findings,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
