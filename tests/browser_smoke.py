"""Optional browser QA (Playwright, not a runtime dependency).

python tests/browser_smoke.py [--browser PATH] [--content-bridge]

--content-bridge is for restricted test environments where browser navigation
is prohibited. It loads the exact HTML/CSS/JS via set_content and bridges fetch
through Python's real HTTP client. This is NOT a direct browser-network test.
Production assets are not modified. Normal mode uses browser HTTP directly.
"""
from __future__ import annotations
import argparse
import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from competition.core import Engine
from competition.display import DisplayService
from competition.network import Service
from competition.storage import Store
from vision_client import VisionClient


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--browser")
    parser.add_argument("--content-bridge",action="store_true")
    args=parser.parse_args()
    out=ROOT / "docs" / "screenshots";out.mkdir(parents=True,exist_ok=True)
    report={"browser_loading":"set_content + actual HTTP fetch bridge" if args.content_bridge else "direct browser HTTP", "checks":[], "viewports":[], "javascript_errors":[]}
    def check(name):report["checks"].append(name)
    temp=tempfile.TemporaryDirectory(prefix="vision_display_qa_")
    store=Store(Path(temp.name)/"qa.sqlite3")
    engine=Engine(store);tcp=Service(engine);web=DisplayService(engine)
    tcp.start("127.0.0.1",0);web.start("127.0.0.1",0)
    client=None
    unavailable=False
    try:
      with sync_playwright() as p:
        options={"headless":True}
        if args.browser:options["executable_path"]=args.browser
        browser=p.chromium.launch(**options)
        report["browser_version"]=browser.version
        page=browser.new_page(viewport={"width":1920,"height":1080},device_scale_factor=1)
        page.on("pageerror", lambda e:report["javascript_errors"].append(str(e)))
        def http_read():
            if unavailable:raise OSError("intentional test outage")
            conn=http.client.HTTPConnection(*web.address,timeout=2)
            conn.request("GET","/api/display")
            response=conn.getresponse();body=response.read().decode();conn.close()
            return {"status":response.status,"body":body}
        if args.content_bridge:
            page.expose_function("__qa_http_read",http_read)
            html=(ROOT/"competition/web/index.html").read_text()
            html=html.replace('<link rel="stylesheet" href="/style.css">',"<style>"+(ROOT/"competition/web/style.css").read_text()+"</style>")
            html=html.replace('<script src="/app.js" defer></script>',"")
            script='window.fetch=async()=>{const r=await window.__qa_http_read();return new Response(r.body,{status:r.status,headers:{"Content-Type":"application/json"}})};'
            html=html.replace("</body>","<script>"+script+(ROOT/"competition/web/app.js").read_text()+"</script></body>")
            page.set_content(html)
        else:page.goto(web.url)
        page.wait_for_function("document.querySelector('#result-value').textContent==='待开赛'")
        assert page.locator("#demo-badge").is_hidden()
        assert page.locator("#demo-controls").is_hidden()
        check("live page starts with no fabricated data; demo controls hidden")

        def wait_server(predicate):
            end=time.monotonic()+3
            while time.monotonic()<end:
                if predicate():return
                time.sleep(.01)
            raise AssertionError("server condition timed out")
        def read_until(kind):
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                msg=client.receive(.25)
                if msg and msg["type"]==kind:return msg
            raise AssertionError("client did not receive "+kind)
        def begin(mode,team):
            nonlocal client
            if client:client.close()
            engine.new_session(team,"qa-client",mode,"QA123456")
            client=VisionClient(*tcp.address,"qa-client","QA123456")
            hello=client.connect();client.acknowledge_target(hello["target_revision"])
            wait_server(lambda:bool(engine.peer and engine.peer.target_revision==1))
            page.wait_for_function("arg => document.querySelector('#team-name').textContent===arg",arg=team)
        def do_round(verdict,expected=None):
            engine.start_round("PRIVATE_CASE_MUST_NEVER_LEAK",expected or verdict,0,"arm","PRIVATE_NOTES_MUST_NEVER_LEAK")
            msg=read_until("round")
            client.send(client.make_result(msg,verdict));read_until("result_ack")
        begin("packaging","包装箱联调演示队伍")
        for i in range(10):do_round("OK" if i%2==0 else "NG")
        page.wait_for_function("document.querySelector('#count-received').textContent==='10'")
        assert page.locator("#count-ok").inner_text()=="5"
        assert page.locator("#count-ng").inner_text()=="5"
        assert page.locator("#records-body tr").count()==6
        assert page.locator("#result-value").inner_text()=="NG"
        assert "不等于" in page.locator("#result-detail").inner_text()
        check("packaging: 10 actual TCP submissions -> HTTP -> public DOM; 5 OK / 5 NG")
        # Contents, not merely the visible DOM, must omit judge data.
        wire=json.loads(http_read()["body"])
        assert all(value not in json.dumps(wire) for value in ("PRIVATE_CASE_MUST_NEVER_LEAK","PRIVATE_NOTES_MUST_NEVER_LEAK","QA123456"))
        check("actual HTTP response excludes access code, sample name and notes")
        begin("screw","螺钉联调演示队伍")
        page.wait_for_function("document.querySelector('#count-total').textContent==='0'")
        assert page.locator("#competition-name").inner_text()=="螺钉漏打检测"
        for i in range(10):do_round("OK" if i%2==0 else "NG")
        page.wait_for_function("document.querySelector('#count-received').textContent==='10'")
        check("screw: clean session switch and 10 actual TCP submissions")
        engine.start_round("PRIVATE_WAITING_SAMPLE","OK")
        read_until("round")
        page.wait_for_function("document.querySelector('#result-value').textContent==='待结果'")
        assert "轮次已布置" in page.locator("#result-description").inner_text()
        assert "练习联调" in page.locator("#contest-label").inner_text()
        check("armed round does not claim camera started; session purpose visible")
        before=page.locator("#elapsed").inner_text();page.wait_for_timeout(200)
        assert before!=page.locator("#elapsed").inner_text()
        check("waiting round elapsed display advances without fabricating a verdict")
        engine.cancel_round("PRIVATE_CANCEL")
        page.wait_for_function("document.querySelector('#result-value').textContent==='已取消'")
        assert "10 / 10" in page.locator("#round-goal").inner_text()
        assert "非连续测试判定" in page.locator("#round-goal").inner_text()
        check("cancellation shown separately from NG; result counter not attempts drives reference")
        engine.start_round("PRIVATE_TIMEOUT_SAMPLE","NG",30)
        read_until("round")
        page.wait_for_function("document.querySelector('#result-value').textContent==='已超时'")
        assert page.locator("#count-ng").inner_text()=="5"
        check("timeout is not converted to NG")
        client.close();client=None
        page.wait_for_function("document.querySelector('#client-status span').textContent==='客户端未连接'")
        check("contestant disconnect visible independently of HTTP health")
        # Hold the engine lock: publisher cannot refresh, but HTTP remains responsive.
        entered=threading.Event();release=threading.Event()
        def hold():
            with engine.lock:entered.set();release.wait(7)
        thread=threading.Thread(target=hold);thread.start();entered.wait(1)
        try:
            page.wait_for_function("document.querySelector('#result-value').textContent==='已离线'",timeout=5500)
            assert page.locator("#connection-banner").is_visible()
            check("stalled publisher ages out; old verdict removed, records visibly stale")
        finally:release.set();thread.join(1)
        page.wait_for_function("document.querySelector('#connection-banner').hidden===true")
        check("automatic recovery restores live state")
        if args.content_bridge:
            unavailable=True
            page.wait_for_function("document.querySelector('#result-value').textContent==='已离线'")
            assert page.locator("#count-ng").inner_text()=="5"
            check("HTTP failure clears live verdict, preserves labelled old records and never substitutes demo data")
            unavailable=False
            page.wait_for_function("document.querySelector('#connection-banner').hidden===true")
            check("HTTP outage recovers automatically")
        # Markup is untrusted user-supplied content, rendered with textContent only.
        begin("screw",'<img src=x onerror="window.XSS=1">')
        assert page.locator("#team-name img").count()==0
        assert page.evaluate("window.XSS===undefined")
        check("team-name markup is rendered as text; no DOM injection")
        client.close();client=None
        page.close()

        preview=browser.new_page(viewport={"width":1920,"height":1080},device_scale_factor=1)
        preview.on("pageerror",lambda e:report["javascript_errors"].append(str(e)))
        if args.content_bridge:preview.set_content((ROOT/"preview_display.html").read_text())
        else:preview.goto((ROOT/"preview_display.html").as_uri())
        preview.locator("#demo-pause").click();preview.locator("#demo-pause").blur()
        assert preview.locator("#demo-badge").is_visible()
        check("standalone preview explicitly labelled as simulated, never competition data")
        preview.wait_for_timeout(3400)
        preview.screenshot(path=str(out/"display_dark_1920.png"))
        for width,height in ((1366,768),(1920,1080),(3840,2160),(1280,1024)):
            preview.set_viewport_size({"width":width,"height":height});preview.wait_for_timeout(100)
            sizes=preview.evaluate('''() => { const r=document.querySelector('#stage').getBoundingClientRect();return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,bodyWidth:document.documentElement.scrollWidth,bodyHeight:document.documentElement.scrollHeight,clip:[...document.querySelectorAll('.panel')].some(x=>x.scrollWidth>x.clientWidth+2||x.scrollHeight>x.clientHeight+2)};}''')
            assert sizes["left"]>=-1 and sizes["top"]>=-1 and sizes["right"]<=width+1 and sizes["bottom"]<=height+1,sizes
            assert not sizes["clip"],sizes
            report["viewports"].append({"width":width,"height":height,"no_panel_overflow":True,"stage_inside_viewport":True})
            if width==3840:preview.screenshot(path=str(out/"display_4k.png"))
        preview.set_viewport_size({"width":1920,"height":1080})
        preview.locator("#contrast-button").evaluate("e=>e.click()")
        assert preview.locator("body").get_attribute("class")=="light"
        preview.screenshot(path=str(out/"display_light_1920.png"))
        preview.locator("#contrast-button").evaluate("e=>e.click()")
        preview.locator("#demo-mode").evaluate("e=>e.click()")
        preview.locator("#demo-next").evaluate("e=>e.click()")
        assert preview.locator("#result-value").inner_text()=="NG"
        assert preview.locator("#competition-name").inner_text()=="螺钉漏打检测"
        preview.screenshot(path=str(out/"display_screw_ng_1920.png"))
        check("light/dark theme and explicit demo competition switch")
        for expected in ("待结果","已超时","OK","NG","已取消","已中断"):
            preview.locator("#demo-next").evaluate("e=>e.click()")
            assert preview.locator("#result-value").inner_text()==expected
        check("demo renders waiting, timeout, disconnect, late, cancelled and interrupted states")
        preview.mouse.move(400,30);preview.locator("#fullscreen-button").click()
        preview.wait_for_function("Boolean(document.fullscreenElement)")
        check("fullscreen button activates browser fullscreen")
        preview.keyboard.press("f")
        preview.wait_for_function("!document.fullscreenElement")
        check("F key exits fullscreen")
        assert not report["javascript_errors"],report["javascript_errors"]
        browser.close()
    finally:
        if client:client.close()
        web.stop();tcp.stop();engine.close_session();store.close();temp.cleanup()
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=="__main__":main()
