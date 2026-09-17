"""Offline synthetic DOM tests for group/result fields; never claims live E2E."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil


def main():
    from playwright.sync_api import sync_playwright, expect
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True);root=Path(__file__).resolve().parents[1]/'competition'/'monitor'
    html=re.sub(r'<link[^>]*>|<script[^>]*>.*?</script>','',(root/'index.html').read_text(encoding='utf-8'),flags=re.S)
    fixtures={}
    for proj in ['screw','packaging']:
        rows=[]
        for n in [1,2]:
            row={'id':n,'station':n,'project':proj,'group_id':'00'+str(n),'worker_id':'','received_utc':'2026-09-17T08:00:00+00:00'}
            row.update({'screw_count':4 if n==1 else 0,'detection_result':'NG' if n==1 else 'OK'} if proj=='screw' else
                {'barcode':'001-AbC','barcode_status':'MATCH' if n==1 else 'MISMATCH','logo':'OK','flame':'NG','total_result':'OK',
                 'standard_box_id':'B'+str(n),'standard_box_name':'箱'+str(n)})
            rows.append(row)
        fixtures[proj]={'project':proj,'generated_utc':rows[0]['received_utc'],'fatal':None,'listening':'127.0.0.1:9000','records':rows,
          'stations':[{'station':r['station'],'connected':True,'group_id':r['group_id'],'worker_id':None,'address':'127.0.0.1:5000',
                       'last_is_current':True,'latest':r} for r in rows],'logs':[]}
    catalog={'revision':1,'boxes':[{'id':'B1','name':'箱1','standard_barcode':'001-AbC'},
               {'id':'B2','name':'箱2','standard_barcode':'OTHER'}],'selections':{'1':'B1','2':'B2'}}
    stub=r'''window.fixtures=FIXTURES;window.catalog=CATALOG;window.offlineTest=false;
window.fetch=async(url,opts={})=>{
 if(offlineTest)throw new Error('offline synthetic outage');
 let data={},status=200;
 if(url==='/api/auth/me')data={username:'离线测试 · 合成数据',csrf:'offline'};
 else if(url.startsWith('/api/admin/state')){data=structuredClone(fixtures[new URL(url,'http://test').searchParams.get('project')]);data.catalog=structuredClone(catalog);}
 else if(url==='/api/admin/standard-barcode/select'){const d=JSON.parse(opts.body);catalog.selections[d.station]=d.box_id;catalog.revision++;data={ok:true,revision:catalog.revision};}
 else throw new Error('unexpected synthetic request '+url);
 return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
};'''.replace('FIXTURES',json.dumps(fixtures,ensure_ascii=False)).replace('CATALOG',json.dumps(catalog,ensure_ascii=False))
    report={'mode':'offline synthetic DOM, not live HTTP/browser E2E','passed':False,'checks':[]}
    try:
        with sync_playwright() as pw:
            options={'headless':True}
            if shutil.which('chromium'):options['executable_path']=shutil.which('chromium')
            b=pw.chromium.launch(**options);ctx=b.new_context(viewport={'width':1440,'height':1100},locale='zh-CN')
            ctx.route('**/*',lambda r:r.abort());page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            page.set_content(html);page.add_style_tag(content=(root/'style.css').read_text());page.add_script_tag(content=stub);page.add_script_tag(content=(root/'app.js').read_text())
            expect(page.locator('#app-screen')).to_be_visible();expect(page.locator('#login-screen')).to_be_hidden()
            expect(page.locator('#stations')).to_contain_text('当前组别号');expect(page.locator('[data-station="1"]')).to_contain_text('001')
            assert page.locator('.count').all_text_contents()==['4颗','0颗']
            assert page.locator('#records-body tr').first.locator('td').all_text_contents()[-1]=='NG'
            assert page.locator('#records-body tr').nth(1).locator('td').all_text_contents()[-1]=='OK'
            report['checks'].append('screw station/group/count/reported verdict; no recomputation from count')
            page.screenshot(path=str(args.output_dir/'screw-groups-offline.png'),full_page=True)
            page.locator('#issues-only').check();expect(page.locator('#records-body tr')).to_have_count(1);page.locator('#issues-only').uncheck()
            report['checks'].append('screw NG filter uses uploaded result')
            page.locator('[data-project=packaging]').click();expect(page.locator('#records-head')).to_contain_text('总结果（上报）')
            assert page.locator('#records-body tr').nth(1).locator('td').all_text_contents()[-4:]==['不匹配','OK','NG','OK']
            expect(page.locator('#box-select-1')).to_have_value('B1');expect(page.locator('#box-select-2')).to_have_value('B2')
            report['checks'].append('barcode comparison, LOGO, flame and total are independent; two station standards retained')
            page.screenshot(path=str(args.output_dir/'packaging-groups-offline.png'),full_page=True)
            page.locator('#box-select-1').select_option('B2');page.evaluate("window.selectRef=document.querySelector('#box-select-1')")
            page.wait_for_timeout(1700);expect(page.locator('#box-select-1')).to_have_value('B2');assert page.evaluate("selectRef===document.querySelector('#box-select-1')")
            report['checks'].append('polling preserves unapplied selection/native select element')
            page.locator('[data-station="1"] .apply-box').click();expect(page.locator('[data-station="1"] .current-box')).to_contain_text('B2')
            expect(page.locator('#box-select-2')).to_have_value('B2');report['checks'].append('applying standard still works independently')
            page.evaluate("for(const [i,r] of fixtures.packaging.records.entries()){r.barcode_status='MATCH';r.logo='OK';r.flame='OK';r.total_result=i===0?'NG':'OK'}")
            page.locator('#issues-only').check();expect(page.locator('#records-body tr')).to_have_count(1)
            report['checks'].append('packaging total NG is included even when all other checks are OK')
            page.locator('#issues-only').uncheck()
            hostile='<img src=x onerror=alert(1)>'
            page.evaluate('(s)=>{fixtures.packaging.records[0].group_id=s;fixtures.packaging.stations[0].group_id=s}',hostile)
            expect(page.locator('#records-body')).to_contain_text(hostile);assert page.locator('#records-body img,#stations img').count()==0
            report['checks'].append('untrusted group text cannot inject HTML')
            page.evaluate("fixtures.packaging.records[0].group_id=null;fixtures.packaging.records[0].total_result=null;fixtures.packaging.records[0].worker_id='OLD-WORKER';fixtures.packaging.stations[0].group_id=null")
            expect(page.locator('#records-body tr').first.locator('td').nth(2)).to_have_text('—')
            expect(page.locator('#records-body tr').first.locator('td').last).to_have_text('—')
            assert 'OLD-WORKER' not in page.locator('#records-body').inner_text()
            report['checks'].append('legacy worker not relabeled as group; absent historical total is dash, not NG')
            page.locator('#station-filter').select_option('2');expect(page.locator('#records-body tr')).to_have_count(1)
            page.locator('#station-filter').select_option('0');report['checks'].append('station record filter still works')
            for _ in range(3):page.locator('[data-project=screw]').click();page.locator('[data-project=packaging]').click()
            expect(page.locator('#records-head')).to_contain_text('总结果（上报）');report['checks'].append('rapid page switching does not mix columns/data')
            page.evaluate('offlineTest=true');expect(page.locator('#web-state')).to_have_text('连接中断')
            expect(page.locator('#stations')).to_contain_text('状态未知');expect(page.locator('#box-select-1')).to_be_disabled()
            report['checks'].append('outage retains stale observations and disables controls')
            assert not errors,errors;report['checks'].append('no JavaScript runtime errors');report['passed']=True
            ctx.close();b.close()
    finally:
        (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
