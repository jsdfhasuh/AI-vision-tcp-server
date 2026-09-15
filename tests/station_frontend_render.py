"""Offline DOM/JSON-file interaction tests. Synthetic HTTP responses, not live E2E."""
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
    html=re.sub(r'<link[^>]*>|<script[^>]*>.*?</script>','', (root/'index.html').read_text(encoding='utf-8'),flags=re.S)
    common={'received_utc':'2026-09-15T10:32:18+00:00','worker_id':'D70516','id':1,'station':1}
    screw=[{**common,'project':'screw','screw_count':4},{**common,'id':2,'station':2,'worker_id':'D70517','project':'screw','screw_count':3}]
    packaging=[{**common,'project':'packaging','barcode':'001234-AbC','barcode_status':'MATCH','logo':'OK','flame':'NG'},
               {**common,'id':2,'station':2,'worker_id':'D70518','project':'packaging','barcode':'001234-OTHER','barcode_status':'MISMATCH','logo':'NG','flame':'OK'}]
    fixtures={}
    for project,rows in [('screw',screw),('packaging',packaging)]:
        fixtures[project]={'project':project,'generated_utc':common['received_utc'],'fatal':None,'listening':'127.0.0.1:9000','records':rows,
            'stations':[{'station':r['station'],'connected':True,'worker_id':r['worker_id'],'address':f'192.168.1.{100+r["station"]}:5001','last_is_current':True,'latest':r} for r in rows],
            'logs':[{'at_utc':common['received_utc'],'direction':'RX','station':r['station'],'raw':json.dumps(r)} for r in rows]}
    stub='''window.fixture=FIXTURES;window.syntheticStandard={barcode:'001234-AbC',revision:1};window.fakeOffline=false;window.imports=[];
window.fetch=async function(url,opts={}){
 if(window.fakeOffline)throw new Error('离线DOM测试：模拟连接中断');
 let path=String(url),data={},status=200;
 if(path==='/api/auth/me')data={username:'离线测试',csrf:'offline-test'};
 else if(path==='/api/auth/logout')data={ok:true};
 else if(path.startsWith('/api/admin/state')){data=structuredClone(fixture[new URL(path,'http://test').searchParams.get('project')]);data.standard=structuredClone(syntheticStandard);}
 else if(path==='/api/admin/standard-barcode/import'){
   let b=JSON.parse(opts.body);imports.push(b);
   if(b.revision!==syntheticStandard.revision){status=409;data={error:{message:'标准已改变'}};}
   else{syntheticStandard={barcode:JSON.parse(b.content.replace(/^\\uFEFF/,'')).standard_barcode,revision:b.revision+1};data={ok:true,revision:syntheticStandard.revision};}
 } else throw new Error('unexpected offline request '+path);
 return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
};'''.replace('FIXTURES',json.dumps(fixtures,ensure_ascii=False))
    report={'mode':'offline synthetic DOM tests, not live browser E2E','passed':False,'checks':[]}
    with sync_playwright() as pw:
        options={'headless':True,'args':['--no-sandbox']}
        if shutil.which('chromium'):options['executable_path']=shutil.which('chromium')
        browser=pw.chromium.launch(**options);context=browser.new_context(viewport={'width':1440,'height':1100},locale='zh-CN')
        context.route('**/*',lambda r:r.abort())
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.set_content(html);page.add_style_tag(content=(root/'style.css').read_text(encoding='utf-8'))
        page.add_script_tag(content=stub);page.add_script_tag(content=(root/'app.js').read_text(encoding='utf-8'))
        def select(value, *, name='standard.json', raw=None):
            content=json.dumps({'standard_barcode':value},ensure_ascii=False).encode('utf-8') if raw is None else raw
            page.locator('#standard-file').set_input_files({'name':name,'mimeType':'application/json','buffer':content})
        expect(page.locator('#app-screen')).to_be_visible();expect(page.locator('.count').first).to_contain_text('4')
        expect(page.locator('#barcode-config')).to_be_hidden()
        report['checks'].append('screw page unaffected')
        page.locator('[data-project=packaging]').click();expect(page.locator('#barcode-config')).to_be_visible()
        expect(page.locator('#records-head')).to_contain_text('火焰标识')
        assert page.locator('#records-body tr').first.locator('td').all_text_contents()[-2:]==['OK','NG']
        report['checks'].append('three independent packaging results')
        assert page.locator('#standard-barcode').evaluate('(e)=>e.readOnly')
        expect(page.locator('#save-standard')).to_be_disabled()
        report['checks'].append('manual entry disabled and file required')
        content='\ufeff{\r\n"standard_barcode":" 0001-AbC "\r\n}\r\n'
        select(None,raw=content.encode('utf-8'));expect(page.locator('#standard-barcode')).to_have_value(' 0001-AbC ')
        expect(page.locator('#file-status')).to_contain_text('待导入')
        page.wait_for_timeout(1800);expect(page.locator('#standard-barcode')).to_have_value(' 0001-AbC ')
        assert page.evaluate('imports.length')==0
        report['checks'].append('file selection is preview only, preserved across polling')
        page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('已导入并生效')
        assert page.evaluate('syntheticStandard.barcode')==' 0001-AbC '
        assert page.evaluate('imports[0].content')==content
        expect(page.locator('#save-standard')).to_be_disabled()
        report['checks'].append('BOM UTF8 and exact original JSON sent to import endpoint')
        for name,raw in [('number.json',b'{"standard_barcode":123}'),('bad.json',b'{'),
                         ('fields.json',b'{"standard_barcode":"x","logo":"OK"}'),
                         ('not-json.txt',b'{}'),('large.json',b' '*4097),
                         ('utf16.json','{"standard_barcode":"001"}'.encode('utf-16'))]:
            select(None,name=name,raw=raw);expect(page.locator('#save-standard')).to_be_disabled()
            expect(page.locator('#file-status')).to_contain_text('文件未导入')
        assert page.evaluate('syntheticStandard.barcode')==' 0001-AbC '
        report['checks'].append('bad file, number, unknown field, extension, size, encoding rejected')
        select('new-standard');expect(page.locator('#save-standard')).to_be_enabled()
        page.evaluate('syntheticStandard.revision+=1')
        page.locator('#save-standard').click();expect(page.locator('#toast')).to_contain_text('标准已改变')
        expect(page.locator('#standard-barcode')).to_have_value('new-standard')
        report['checks'].append('revision conflict preserves pending file')
        page.once('dialog',lambda d:d.accept());page.locator('#reload-standard').click()
        expect(page.locator('#save-standard')).to_be_disabled()
        report['checks'].append('cancel clears pending import')
        select('');expect(page.locator('#file-status')).to_contain_text('空字符串')
        count=page.evaluate('imports.length')
        page.once('dialog',lambda d:d.dismiss());page.locator('#save-standard').click()
        assert page.evaluate('imports.length')==count
        report['checks'].append('empty standard requires explicit confirmation')
        hostile='<img src=x onerror=alert(1)>'
        select(hostile);expect(page.locator('#standard-barcode')).to_have_value(hostile)
        page.evaluate('(text)=>{fixture.packaging.records[0].barcode=text;fixture.packaging.stations[0].latest.barcode=text}',hostile)
        expect(page.locator('#records-body')).to_contain_text(hostile)
        assert page.locator('#records-body img').count()==0
        report['checks'].append('file and station barcodes treated as text, not HTML')
        page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
        expect(page.locator('#records-body')).to_contain_text(hostile)
        expect(page.locator('#standard-barcode')).to_have_value(hostile)
        report['checks'].append('switching tabs preserves pending JSON and selected project')
        page.locator('#station-filter').select_option('2');expect(page.locator('#records-body tr')).to_have_count(1)
        page.locator('#station-filter').select_option('0');expect(page.locator('#records-body tr')).to_have_count(2)
        report['checks'].append('station filter unchanged')
        page.evaluate('window.fakeOffline=true');expect(page.locator('#web-state')).to_have_text('连接中断')
        expect(page.locator('#save-standard')).to_be_disabled();expect(page.locator('#standard-file')).to_be_disabled()
        expect(page.locator('#stations')).to_contain_text('状态未知')
        report['checks'].append('offline disables imports without deleting records')
        page.evaluate('window.fakeOffline=false');expect(page.locator('#web-state')).to_have_text('实时连接')
        page.locator('#logout').click();expect(page.locator('#login-screen')).to_be_visible()
        assert page.locator('#standard-file').evaluate('(e)=>e.files.length')==0
        expect(page.locator('#standard-barcode')).to_have_value('')
        report['checks'].append('logout clears file and standard preview')
        assert not errors,errors;report['checks'].append('no JavaScript errors')
        report['passed']=True;context.close();browser.close()
    (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
