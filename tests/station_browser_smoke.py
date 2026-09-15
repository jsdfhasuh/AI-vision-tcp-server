"""Real Chromium + HTTP + four TCP sockets. Optional Playwright dependency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from competition.station_protocol import encode, decode
from competition.station_webapp import create_app
from competition.web_config import WebConfig
from competition.web_auth import credential_record


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    import uvicorn
    from playwright.sync_api import sync_playwright, expect
    peers=[];server=None;thread=None
    report={'passed':False,'checks':[]}
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);password='Browser-test-only-123456'
        credentials=root/'admin.json';credentials.write_text(json.dumps(credential_record('admin',password)))
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        base=f'http://localhost:{port}'
        config=WebConfig(data_dir=root/'data',credentials=credentials,public_url=base,port=port,tcp_port=0,auto_backup=False)
        app=create_app(config)
        server=uvicorn.Server(uvicorn.Config(app,log_level='warning',ws='none',proxy_headers=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        for _ in range(150):
            if server.started:break
            time.sleep(.05)
        if not server.started:raise RuntimeError('HTTP server did not start')
        def send(index, project, station, **fields):
            sock,stream=peers[index]
            message={'v':2,'type':'result','msg_id':uuid.uuid4().hex,'project':project,'station':station,'worker_id':f'D70{516+index}',**fields}
            sock.sendall(encode(message));reply=decode(stream.readline()[:-1]);assert reply['recorded']
        try:
            with sync_playwright() as pw:
                options={'headless':True,'args':['--no-sandbox']}
                chromium=shutil.which('chromium')
                if chromium:options['executable_path']=chromium
                browser=pw.chromium.launch(**options)
                context=browser.new_context(viewport={'width':1440,'height':1100},locale='zh-CN')
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/admin/');page.locator('[name=password]').fill(password)
                page.locator('#login-form button').click();expect(page.locator('#app-screen')).to_be_visible()
                expect(page.locator('#web-state')).to_have_text('实时连接')
                for project in ('screw','packaging'):
                    for station in (1,2):
                        sock=socket.create_connection(app.state.runtime.tcp.address,timeout=5);stream=sock.makefile('rb');peers.append((sock,stream))
                        sock.sendall(encode({'v':2,'type':'hello','msg_id':uuid.uuid4().hex,'project':project,'station':station}))
                        assert decode(stream.readline()[:-1])['type']=='hello_ok'
                send(0,'screw',1,screw_count=4);send(1,'screw',2,screw_count=3)
                expect(page.locator('.count').first).to_contain_text('4')
                expect(page.locator('.count').nth(1)).to_contain_text('3')
                expect(page.locator('#stations')).to_contain_text('D70516')
                page.screenshot(path=str(args.output_dir/'screw-page.png'),full_page=True)
                report['checks'].append('screw page shows live counts and operator IDs from real TCP')
                page.locator('[data-project=packaging]').click()
                expect(page.locator('#save-standard')).to_be_enabled()
                page.locator('#standard-barcode').fill('001234-AbC')
                with page.expect_response(lambda r:r.url.endswith('/api/admin/standard-barcode') and r.request.method=='POST') as saved:
                    page.locator('#save-standard').click()
                assert saved.value.status==200
                send(2,'packaging',1,barcode='001234-AbC',logo='OK',flame='NG')
                send(3,'packaging',2,barcode='001234-OTHER',logo='NG',flame='OK')
                expect(page.locator('#stations [data-station="1"]')).to_contain_text('匹配')
                expect(page.locator('#stations [data-station="2"]')).to_contain_text('不匹配')
                expect(page.locator('#records-body tr')).to_have_count(2)
                assert page.locator('#records-body tr').first.locator('td').all_text_contents()[-2:]==['NG','OK']
                page.screenshot(path=str(args.output_dir/'packaging-page.png'),full_page=True)
                report['checks'].append('barcode save, exact validation, independent LOGO and flame columns')
                page.locator('#standard-barcode').fill('UNSAVED-000')
                page.wait_for_timeout(1800)
                expect(page.locator('#standard-barcode')).to_have_value('UNSAVED-000')
                me=page.request.get(base+'/api/auth/me').json()
                response=page.request.post(base+'/api/admin/standard-barcode',data={'barcode':'NEW-STANDARD','revision':1},headers={'Origin':base,'X-CSRF-Token':me['csrf'],'X-Request-ID':uuid.uuid4().hex})
                assert response.status==200
                with page.expect_response(lambda r:r.url.endswith('/api/admin/standard-barcode') and r.request.method=='POST') as conflict:
                    page.locator('#save-standard').click()
                assert conflict.value.status==409
                report['checks'].append('polling preserves unsaved input; stale configuration rejected')
                hostile='<img src=x onerror=alert(1)>'
                send(3,'packaging',2,barcode=hostile,logo='OK',flame='NG')
                expect(page.locator('#records-body')).to_contain_text(hostile)
                assert page.locator('#records-body img').count()==0
                report['checks'].append('untrusted barcode rendered as text without HTML execution')
                for _ in range(3):
                    page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
                expect(page.locator('#records-head')).to_contain_text('火焰标识')
                expect(page.locator('#records-body')).to_contain_text(hostile)
                report['checks'].append('rapid tab switching does not mix project data')
                with page.expect_download() as downloaded:page.locator('#export-csv').click()
                assert downloaded.value.suggested_filename=='packaging-records.csv'
                report['checks'].append('CSV download through authenticated HTTP')
                public=browser.new_context(viewport={'width':1440,'height':1100});board=public.new_page();board.goto(base+'/board/')
                expect(board.locator('#app-screen')).to_be_visible();board.locator('[data-project=packaging]').click()
                expect(board.locator('#barcode-config')).to_be_hidden();expect(board.locator('#logs-panel')).to_be_hidden()
                data=board.request.get(base+'/api/display?project=packaging').json()
                assert 'standard' not in data and 'logs' not in data
                assert public.request.get(base+'/api/admin/state').status==401
                report['checks'].append('public board cannot read standard, logs or authenticated API')
                page.locator('[data-project=screw]').click();peers[1][1].close();peers[1][0].close()
                expect(page.locator('[data-station="2"] .station-title')).to_contain_text('未连接')
                expect(page.locator('[data-station="2"] .count')).to_contain_text('3')
                report['checks'].append('disconnect retains last count but clears live connection/operator')
                assert not errors,errors
                report['checks'].append('no browser JavaScript errors')
                report['passed']=True
                public.close();context.close();browser.close()
        finally:
            for sock,stream in peers:stream.close();sock.close()
            server.should_exit=True;thread.join(10);listener.close()
            (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
