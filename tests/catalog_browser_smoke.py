"""Optional browser checks. --offline is explicitly synthetic, not live E2E."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil

from catalog_http_smoke import live_server, TCPClient
from competition.standard_json import EXAMPLE_CATALOG


def offline(page, out, report):
    from playwright.sync_api import expect
    root=Path(__file__).resolve().parents[1]/'competition'/'monitor'
    html=re.sub(r'<link[^>]*>|<script[^>]*>.*?</script>','', (root/'index.html').read_text(encoding='utf-8'),flags=re.S)
    common={'received_utc':'2026-09-16T10:32:18+00:00','worker_id':'001234','id':1,'station':1}
    screw=[{**common,'project':'screw','screw_count':4},{**common,'id':2,'station':2,'worker_id':'005678','project':'screw','screw_count':3}]
    packaging=[{**common,'project':'packaging','barcode':'001234-AbC','barcode_status':'MATCH','logo':'OK','flame':'NG','standard_box_id':'BOX01','standard_box_name':'1号包装箱'},
               {**common,'id':2,'station':2,'project':'packaging','barcode':'001234-AbC','barcode_status':'MISMATCH','logo':'NG','flame':'OK','standard_box_id':'BOX02','standard_box_name':'2号包装箱'}]
    fixtures={}
    for proj,rows in [('screw',screw),('packaging',packaging)]:
        fixtures[proj]={'project':proj,'generated_utc':common['received_utc'],'fatal':None,'listening':'127.0.0.1:9000','records':rows,
            'stations':[{'station':r['station'],'connected':True,'worker_id':r['worker_id'],'address':f'192.168.1.{100+r["station"]}:5001','last_is_current':True,'latest':r} for r in rows],
            'logs':[{'at_utc':common['received_utc'],'direction':'RX','station':r['station'],'raw':json.dumps(r)} for r in rows]}
    catalog={'revision':1,'boxes':EXAMPLE_CATALOG['boxes'],'selections':{'1':'BOX01','2':'BOX02'}}
    stub=r'''window.fixtures=FIXTURES;window.currentCatalog=CATALOG;window.fakeOffline=false;
window.fetch=async(url,opts={})=>{
 if(fakeOffline)throw new Error('合成测试：连接中断');
 const p=String(url);let data={},status=200;
 if(p==='/api/auth/me')data={username:'离线界面检查 · 合成数据',csrf:'offline'};
 else if(p==='/api/auth/logout')data={ok:true};
 else if(p.startsWith('/api/admin/state')){data=structuredClone(fixtures[new URL(p,'http://test').searchParams.get('project')]);data.catalog=structuredClone(currentCatalog);}
 else if(p==='/api/admin/standard-barcode/import'||p==='/api/admin/standard-barcode/select'){
  const b=JSON.parse(opts.body);
  if(b.revision!==currentCatalog.revision){status=409;data={error:{message:'配置版本已改变，请重新载入。'}};}
  else if(p.endsWith('/select')){
   currentCatalog.selections[String(b.station)]=b.box_id;currentCatalog.revision++;data={revision:currentCatalog.revision,ok:true};
  }else{
   const x=JSON.parse(b.content.replace(/^\uFEFF/,''));
   if('standard_barcode' in x){currentCatalog.boxes=x.standard_barcode?[{id:'LEGACY',name:'原标准条码',standard_barcode:x.standard_barcode}]:[];currentCatalog.selections={'1':x.standard_barcode?'LEGACY':null,'2':x.standard_barcode?'LEGACY':null};}
   else{const old=new Map(currentCatalog.boxes.map(v=>[v.id,v.standard_barcode]));currentCatalog.boxes=x.boxes;for(const n of ['1','2']){const match=x.boxes.find(v=>v.id===currentCatalog.selections[n]&&v.standard_barcode===old.get(v.id));if(!match)currentCatalog.selections[n]=null;}}
   currentCatalog.revision++;data={revision:currentCatalog.revision,ok:true};
  }
 }else throw new Error('Unexpected synthetic request '+p);
 return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
};'''.replace('FIXTURES',json.dumps(fixtures,ensure_ascii=False)).replace('CATALOG',json.dumps(catalog,ensure_ascii=False))
    # All external browser traffic is blocked. This is not a workaround for live E2E.
    page.context.route('**/*',lambda route:route.abort())
    page.set_content(html);page.add_style_tag(content=(root/'style.css').read_text(encoding='utf-8'))
    page.add_script_tag(content=stub);page.add_script_tag(content=(root/'app.js').read_text(encoding='utf-8'))
    expect(page.locator('#app-screen')).to_be_visible();expect(page.locator('#login-screen')).to_be_hidden()
    expect(page.locator('.count').first).to_contain_text('4');expect(page.locator('.count').nth(1)).to_contain_text('3')
    page.screenshot(path=str(out/'screw-catalog-offline.png'),full_page=True)
    report['checks'].append('screw count and operator fields unchanged')
    page.locator('[data-project=packaging]').click();expect(page.locator('#box-select-1')).to_have_value('BOX01');expect(page.locator('#box-select-2')).to_have_value('BOX02')
    expect(page.locator('#records-head')).to_contain_text('本条校验标准');expect(page.locator('#records-head')).to_contain_text('火焰标识')
    page.screenshot(path=str(out/'packaging-catalog-offline.png'),full_page=True)
    report['checks'].append('catalog preview, independent current selections, historical target/LOGO/flame columns')
    page.locator('#catalog-search').fill('BOX03');expect(page.locator('#catalog-body tr')).to_have_count(1)
    page.locator('#catalog-search').fill('');expect(page.locator('#catalog-body tr')).to_have_count(3)
    report['checks'].append('catalog search')
    page.locator('#box-select-1').select_option('BOX03')
    page.evaluate("window.savedSelect=document.querySelector('#box-select-1')")
    page.wait_for_timeout(1700);expect(page.locator('#box-select-1')).to_have_value('BOX03')
    assert page.evaluate("savedSelect===document.querySelector('#box-select-1')")
    report['checks'].append('polling preserves pending selection and the actual native select element')
    page.locator('[data-station="1"] .apply-box').click()
    expect(page.locator('[data-station="1"] .current-box')).to_contain_text('BOX03')
    expect(page.locator('#box-select-2')).to_have_value('BOX02')
    report['checks'].append('apply one selection without changing the other')
    def upload(data):
        page.locator('#standard-file').set_input_files({'name':'batch.json','mimeType':'application/json','buffer':json.dumps(data,ensure_ascii=False).encode('utf-8')})
    batch={'boxes':[{'id':'NEW1','name':'新箱一','standard_barcode':'000-New-A'},{'id':'NEW2','name':'新箱二','standard_barcode':'000-New-B'}]}
    upload(batch);expect(page.locator('#file-status')).to_contain_text('尚未生效');expect(page.locator('#catalog-body')).to_contain_text('新箱一')
    assert page.evaluate('currentCatalog.boxes.length')==3
    page.wait_for_timeout(1700);expect(page.locator('#catalog-body')).to_contain_text('新箱一')
    page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
    expect(page.locator('#file-status')).to_contain_text('尚未生效')
    report['checks'].append('pending file survives polling and page switches, has no effect before import')
    page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('已导入')
    expect(page.locator('#box-select-1')).to_have_value('');expect(page.locator('#box-select-2')).to_have_value('')
    report['checks'].append('replacement import clears removed current targets')
    upload(batch);page.evaluate('currentCatalog.revision++')
    page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('配置版本已改变')
    expect(page.locator('#file-status')).to_contain_text('尚未生效')
    page.locator('#reload-standard').click();expect(page.locator('#save-standard')).to_be_disabled()
    report['checks'].append('stale import rejected without destroying pending file')
    upload({'boxes':[{'id':'N','name':'n','standard_barcode':123}]})
    expect(page.locator('#file-status')).to_contain_text('未导入');expect(page.locator('#save-standard')).to_be_disabled()
    report['checks'].append('numeric barcode file rejected without replacing current catalog')
    upload({'standard_barcode':' 000-AbC '});page.locator('#save-standard').click()
    expect(page.locator('#box-select-1')).to_have_value('LEGACY');expect(page.locator('#box-select-2')).to_have_value('LEGACY')
    assert page.evaluate('currentCatalog.boxes[0].standard_barcode')==' 000-AbC '
    report['checks'].append('legacy single JSON compatibility and exact spaces/leading zeros')
    hostile='<img src=x onerror=alert(1)>'
    page.evaluate('(s)=>{fixtures.packaging.records[0].barcode=s;fixtures.packaging.stations[0].latest.barcode=s;currentCatalog.boxes[0].name=s;currentCatalog.revision++}',hostile)
    expect(page.locator('#catalog-body')).to_contain_text(hostile);expect(page.locator('#records-body')).to_contain_text(hostile)
    assert page.locator('#catalog-body img, #records-body img').count()==0
    report['checks'].append('untrusted catalog names and barcodes are literal text, not HTML')
    page.locator('#station-filter').select_option('2');expect(page.locator('#records-body tr')).to_have_count(1)
    page.locator('#station-filter').select_option('0');expect(page.locator('#records-body tr')).to_have_count(2)
    report['checks'].append('record station filters retained')
    for _ in range(3):page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
    expect(page.locator('#records-head')).to_contain_text('火焰标识')
    report['checks'].append('rapid tab switching keeps project data isolated')
    page.evaluate('fakeOffline=true');expect(page.locator('#web-state')).to_have_text('连接中断')
    expect(page.locator('#standard-file')).to_be_disabled();expect(page.locator('#box-select-1')).to_be_disabled()
    expect(page.locator('#stations')).to_contain_text('状态未知')
    report['checks'].append('network loss disables both import and station selection')


def online(page,out,report,base,password,app):
    from playwright.sync_api import expect
    peers=[]
    page.goto(base+'/admin/')  # Do not bypass a browser policy failure here.
    try:
        page.locator('[name=password]').fill(password);page.locator('#login-form button').click()
        expect(page.locator('#app-screen')).to_be_visible();page.locator('[data-project=packaging]').click()
        page.locator('#standard-file').set_input_files({'name':'batch.json','mimeType':'application/json','buffer':json.dumps(EXAMPLE_CATALOG).encode('utf-8')})
        page.locator('#save-standard').click();expect(page.locator('#box-select-1 option')).to_have_count(4)
        page.locator('#box-select-1').select_option('BOX01');page.locator('[data-station="1"] .apply-box').click()
        expect(page.locator('[data-station="1"] .current-box')).to_contain_text('BOX01')
        page.locator('#box-select-2').select_option('BOX02');page.locator('[data-station="2"] .apply-box').click()
        expect(page.locator('[data-station="2"] .current-box')).to_contain_text('BOX02')
        for n in (1,2):
            c=TCPClient(app.state.runtime.tcp.address,'packaging',n);peers.append(c)
            assert c.send(c.result(barcode='001234-AbC',logo='OK',flame='NG'))['recorded']
        expect(page.locator('[data-station="1"] .result-block')).to_contain_text('匹配')
        expect(page.locator('[data-station="2"] .result-block')).to_contain_text('不匹配')
        expect(page.locator('#records-body tr')).to_have_count(2)
        page.screenshot(path=str(out/'packaging-catalog-live.png'),full_page=True)
        report['checks'].append('real browser login, batch file import, selections and independent results from TCP')
        with page.expect_download() as downloaded:page.locator('#export-csv').click()
        assert downloaded.value.suggested_filename=='packaging-records.csv'
        report['checks'].append('authenticated CSV download')
        board=page.context.browser.new_page();board.goto(base+'/board/');board.locator('[data-project=packaging]').click()
        expect(board.locator('#barcode-config')).to_be_hidden();expect(board.locator('.box-select')).to_have_count(0)
        board.close();report['checks'].append('public page has no catalog or standard controls')
    finally:
        for c in peers:c.close()


def main(default_offline=False):
    from playwright.sync_api import sync_playwright
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--offline',action='store_true',default=default_offline);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    report={'mode':'offline synthetic DOM, NOT live E2E' if args.offline else 'real browser/HTTP/TCP E2E','passed':False,'checks':[]}
    try:
        with sync_playwright() as pw:
            options={'headless':True}
            if shutil.which('chromium'):options['executable_path']=shutil.which('chromium')
            browser=pw.chromium.launch(**options);context=browser.new_context(viewport={'width':1440,'height':1100},locale='zh-CN')
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            if args.offline:offline(page,args.output_dir,report)
            else:
                with live_server() as (base,password,app):online(page,args.output_dir,report,base,password,app)
            assert not errors,errors
            report['checks'].append('no JavaScript errors');report['passed']=True
            context.close();browser.close()
    except Exception as exc:
        report['error']=str(exc);raise
    finally:
        (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
