"""Offline DOM tests only: synthetic fetch responses, all browser networking disabled.
This is NOT a browser-to-live-server end-to-end test.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright, expect
    root=Path(__file__).resolve().parents[1]/'competition'/'monitor'
    html=re.sub(r'<link[^>]*>|<script[^>]*>.*?</script>','', (root/'index.html').read_text(),flags=re.S)
    common={'received_utc':'2026-09-15T10:32:18+00:00','worker_id':'D70516','id':1,'station':1}
    screw=[{**common,'project':'screw','screw_count':4},{**common,'id':2,'station':2,'worker_id':'D70517','project':'screw','screw_count':3}]
    packaging=[{**common,'project':'packaging','barcode':'001234-AbC','barcode_status':'MATCH','logo':'OK','flame':'NG'},
               {**common,'id':2,'station':2,'worker_id':'D70518','project':'packaging','barcode':'001234-OTHER','barcode_status':'MISMATCH','logo':'NG','flame':'OK'}]
    fake={}
    for project,rows in [('screw',screw),('packaging',packaging)]:
        fake[project]={'project':project,'generated_utc':common['received_utc'],'fatal':None,'listening':'127.0.0.1:9000','records':rows,
            'stations':[{'station':r['station'],'connected':True,'worker_id':r['worker_id'],'address':f'192.168.1.{100+r["station"]}:5001','last_is_current':True,'latest':r} for r in rows],
            'logs':[{'at_utc':common['received_utc'],'direction':'RX','station':r['station'],'raw':json.dumps(r)} for r in rows]}
    stub='''window.fixture=FIXTURES;window.syntheticStandard={barcode:'001234-AbC',revision:1};window.fakeOffline=false;
window.fetch=async function(url,opts={}){
 if(window.fakeOffline)throw new Error('离线渲染测试：模拟连接中断');
 let path=String(url),data={},status=200;
 if(path==='/api/auth/me')data={username:'离线演示',csrf:'offline-test'};
 else if(path==='/api/auth/logout')data={ok:true};
 else if(path.startsWith('/api/admin/state')){data=structuredClone(window.fixture[new URL(path,'http://test').searchParams.get('project')]);data.standard=structuredClone(window.syntheticStandard);}
 else if(path==='/api/admin/standard-barcode'){let b=JSON.parse(opts.body);if(b.revision!==syntheticStandard.revision){status=409;data={error:{message:'标准已改变'}};}else{syntheticStandard={barcode:b.barcode,revision:b.revision+1};data={ok:true};}}
 else throw new Error('unexpected offline request '+path);
 return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
};'''.replace('FIXTURES',json.dumps(fake,ensure_ascii=False))
    report={'mode':'offline synthetic DOM tests, not live browser E2E','passed':False,'checks':[]}
    with sync_playwright() as pw:
        options={'headless':True,'args':['--no-sandbox']}
        if shutil.which('chromium'):options['executable_path']=shutil.which('chromium')
        browser=pw.chromium.launch(**options);context=browser.new_context(viewport={'width':1440,'height':1100},locale='zh-CN')
        context.route('**/*',lambda r:r.abort())
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.set_content(html);page.add_style_tag(content=(root/'style.css').read_text())
        page.add_script_tag(content=stub);page.add_script_tag(content=(root/'app.js').read_text())
        expect(page.locator('#app-screen')).to_be_visible();expect(page.locator('.count').first).to_contain_text('4')
        expect(page.locator('#login-screen')).to_be_hidden();expect(page.locator('#barcode-config')).to_be_hidden()
        page.evaluate("document.querySelector('#actor').textContent='离线界面检查 · 示例数据'")
        page.screenshot(path=str(args.output_dir/'screw-page-offline.png'),full_page=True)
        report['checks'].append('screw count, operator, correct visibility and layout')
        page.locator('[data-project=packaging]').click();expect(page.locator('#barcode-config')).to_be_visible()
        expect(page.locator('#records-head')).to_contain_text('火焰标识')
        assert page.locator('#records-body tr').first.locator('td').all_text_contents()[-2:]==['OK','NG']
        page.screenshot(path=str(args.output_dir/'packaging-page-offline.png'),full_page=True)
        report['checks'].append('barcode, LOGO, flame have independent display columns')
        page.locator('#standard-barcode').fill('UNSAVED-000');page.wait_for_timeout(1800)
        expect(page.locator('#standard-barcode')).to_have_value('UNSAVED-000')
        page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('已保存')
        assert page.evaluate('window.syntheticStandard.barcode')=='UNSAVED-000'
        report['checks'].append('polling preserves input; save uses exact text')
        page.locator('#standard-barcode').fill('not-latest');page.evaluate('window.syntheticStandard.revision+=1')
        page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('标准已改变')
        report['checks'].append('stale configuration response displayed without overwriting typed text')
        hostile='<img src=x onerror=alert(1)>'
        page.evaluate('(text)=>{fixture.packaging.records[0].barcode=text;fixture.packaging.stations[0].latest.barcode=text}',hostile)
        expect(page.locator('#records-body')).to_contain_text(hostile)
        assert page.locator('#records-body img').count()==0
        report['checks'].append('barcode markup is text, not injected HTML')
        for _ in range(3):page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
        expect(page.locator('#records-body')).to_contain_text(hostile)
        report['checks'].append('tab switching keeps the selected project')
        page.locator('#station-filter').select_option('2');expect(page.locator('#records-body tr')).to_have_count(1)
        page.locator('#station-filter').select_option('0');expect(page.locator('#records-body tr')).to_have_count(2)
        report['checks'].append('station record filter')
        page.evaluate('window.fakeOffline=true');expect(page.locator('#web-state')).to_have_text('连接中断')
        expect(page.locator('#save-standard')).to_be_disabled();expect(page.locator('#stations')).to_contain_text('状态未知')
        report['checks'].append('stale display and disabled writes during simulated outage')
        assert not errors,errors
        report['checks'].append('no JavaScript errors')
        report['passed']=True;context.close();browser.close()
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
