"""Standard-library regression tests for the public display boundary and HTTP routes."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from competition import protocol
from competition.core import Engine
from competition.display import DisplayService, SnapshotCache, public_snapshot, validate_branding
from competition.storage import Store


class Peer:
    def __init__(self):
        self.name = "10.2.3.4:1234"
        self.session_id = self.client_id = self.target_revision = None
        self.alive = True
        self.disconnect_reported = False
        self.messages = []

    def send(self, message):
        self.messages.append(message)

    def close(self):
        self.alive = False


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.engine = Engine(self.store)
        self.server = None
        self.mid = 0

    def tearDown(self):
        if self.server:
            self.server.stop()
        self.engine.close_session()
        self.store.close()
        self.temp.cleanup()

    def connect(self, mode="packaging", team="公开队伍"):
        session = self.engine.new_session(team, "PRIVATE_CLIENT_99", mode, "SECRET_ACCESS_88")
        peer = Peer()
        self.send(peer, "hello", client_id="PRIVATE_CLIENT_99", access_code="SECRET_ACCESS_88")
        self.send(peer, "target_ack", target_revision=session["target_revision"])
        self.engine.listening = "10.1.1.9:9000"
        return peer

    def send(self, peer, kind, **values):
        self.mid += 1
        msg = {"v":1, "type":kind, "msg_id":f"m-{self.mid}"}
        if kind != "hello":
            msg["session_id"] = self.engine.session_id
        msg.update(values)
        self.engine.receive(peer, protocol.encode(msg))

    def snapshot(self):
        return public_snapshot(self.engine, "测试标题", "测试副标题")

    def result(self, peer, actual="OK", expected="OK", timeout=0):
        rid = self.engine.start_round("PRIVATE_SAMPLE_NAME", expected, timeout, "arm", "PRIVATE_JUDGE_NOTES")
        self.send(peer, "result", round_id=rid, target_revision=1, verdict=actual)
        return rid

    def http(self, method="GET", route="/api/display"):
        if not self.server:
            self.server = DisplayService(self.engine)
            self.server.start("127.0.0.1", 0)
        conn = http.client.HTTPConnection(*self.server.address, timeout=3)
        conn.request(method, route)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def test_empty_snapshot(self):
        s = self.snapshot()
        self.assertIsNone(s["session"])
        self.assertIsNone(s["latest"])
        self.assertEqual(s["counts"]["total"],0)

    def test_snapshot_never_exposes_private_columns_or_raw_secrets(self):
        peer = self.connect()
        self.result(peer, actual="NG", expected="OK")
        self.engine.log("PRIVATE_EVENT", "PRIVATE_RAW_AUDIT")
        encoded = json.dumps(self.snapshot(), ensure_ascii=False)
        for forbidden in ("SECRET_ACCESS_88", "PRIVATE_CLIENT_99", "PRIVATE_SAMPLE_NAME", "PRIVATE_JUDGE_NOTES",
                          "PRIVATE_RAW_AUDIT", "10.2.3.4", "10.1.1.9", self.engine.session_id,
                          '"expected"', '"matched"', '"access_code"', '"case_name"', '"notes"', '"target"'):
            self.assertNotIn(forbidden, encoded)

    def test_result_ng_is_not_judge_error_stat(self):
        peer = self.connect()
        self.result(peer,actual="NG",expected="NG")
        s=self.snapshot()
        self.assertEqual(s["counts"]["ng"],1)
        self.assertEqual(s["latest"]["actual"],"NG")
        self.assertNotIn("matched",s["counts"])

    def test_current_session_only(self):
        peer = self.connect()
        self.result(peer)
        self.engine.new_session("另一队", "other", "screw")
        s = self.snapshot()
        self.assertEqual(s["session"]["team"], "另一队")
        self.assertEqual(s["session"]["competition"], "screw")
        self.assertEqual(s["counts"]["total"],0)
        self.assertEqual(s["recent"],[])

    def test_peer_disconnect_is_public_but_not_ip(self):
        peer=self.connect(); self.result(peer)
        self.engine.disconnected(peer,"PRIVATE_REASON")
        s=self.snapshot()
        self.assertFalse(s["connection"]["connected"])
        self.assertFalse(s["connection"]["ready"])
        self.assertEqual(s["latest"]["actual"],"OK")
        self.assertNotIn("PRIVATE_REASON", json.dumps(s))

    def test_target_not_acknowledged(self):
        self.connect()
        self.engine.set_target("SECRET_BOX_MODEL")
        s=self.snapshot()
        self.assertTrue(s["connection"]["connected"])
        self.assertFalse(s["connection"]["ready"])
        self.assertNotIn("SECRET_BOX_MODEL",json.dumps(s))

    def test_active_elapsed_uses_monotonic_clock(self):
        self.connect()
        rid=self.engine.start_round("SAMPLE","OK")
        self.engine.started_ns[rid] -= 15_000_000
        s=self.snapshot()
        self.assertEqual(s["latest"]["status"],"WAITING")
        self.assertGreaterEqual(s["latest"]["elapsed_ms"],15)
        self.assertEqual(s["counts"]["received"],0)
        self.assertEqual(s["timings"],[])

    def test_timeout_does_not_fabricate_ng(self):
        self.connect()
        rid=self.engine.start_round("SAMPLE","NG",5)
        self.engine.started_ns[rid]-=10_000_000
        self.engine.tick()
        s=self.snapshot()
        self.assertEqual(s["latest"]["status"],"TIMEOUT")
        self.assertIsNone(s["latest"]["actual"])
        self.assertEqual(s["counts"]["ng"],0)
        self.assertEqual(s["counts"]["timeout"],1)

    def test_cancelled_not_received(self):
        self.connect()
        self.engine.start_round("SAMPLE","NG")
        self.engine.cancel_round("PRIVATE_CANCEL_REASON")
        s=self.snapshot()
        self.assertEqual(s["latest"]["status"],"CANCELLED")
        self.assertEqual(s["counts"]["received"],0)
        self.assertNotIn("PRIVATE_CANCEL_REASON",json.dumps(s))

    def test_interrupted_not_received(self):
        self.connect()
        self.engine.start_round("SAMPLE","NG")
        self.engine.cancel_round("PRIVATE_REASON","INTERRUPTED")
        s=self.snapshot()
        self.assertEqual(s["latest"]["status"],"INTERRUPTED")
        self.assertEqual(s["counts"]["received"],0)

    def test_old_late_result_never_replaces_current_round(self):
        peer=self.connect()
        old=self.engine.start_round("SAMPLE_A","NG",5)
        self.engine.started_ns[old]-=10_000_000
        self.engine.tick()
        latest=self.engine.start_round("SAMPLE_B","OK")
        self.send(peer,"result",round_id=old,target_revision=1,verdict="NG")
        s=self.snapshot()
        self.assertEqual(s["latest"]["number"],2)
        self.assertEqual(s["latest"]["status"],"WAITING")
        self.assertEqual(s["recent"][1]["status"],"LATE")
        self.assertEqual(s["counts"]["late"],1)
        self.assertEqual(self.engine.active_id,latest)

    def test_fatal_reason_does_not_leak(self):
        self.connect()
        self.engine.fatal="SECRET_WINDOWS_PATH / C:/Users/name PRIVATE_FAIL"
        s=self.snapshot()
        self.assertTrue(s["connection"]["fault"])
        self.assertNotIn("SECRET_WINDOWS_PATH",json.dumps(s))
        self.engine.fatal=None

    def test_bounded_rows_but_totals_complete(self):
        peer=self.connect()
        for i in range(27):
            self.result(peer, "OK" if i%2 else "NG")
        s=self.snapshot()
        self.assertEqual(s["counts"]["total"],27)
        self.assertEqual(s["counts"]["received"],27)
        self.assertEqual(len(s["recent"]),6)
        self.assertEqual(len(s["timings"]),20)
        self.assertEqual(s["timings"][0]["number"],8)
        self.assertEqual(s["recent"][0]["number"],27)

    def test_busy_engine_skips_publish(self):
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.engine.lock:
                entered.set();release.wait(2)
        t=threading.Thread(target=hold);t.start();entered.wait(1)
        try:
            start=time.monotonic()
            self.assertIsNone(self.snapshot())
            self.assertLess(time.monotonic()-start,.3)
        finally:
            release.set();t.join(1)

    def test_cache_read_is_deep_copy(self):
        cache=SnapshotCache(self.engine,"标题","副标题");cache.refresh()
        first=cache.read();first["data"]["counts"]["ok"]=100
        self.assertEqual(cache.read()["data"]["counts"]["ok"],0)

    def test_cache_staleness(self):
        cache=SnapshotCache(self.engine,"标题","副标题");cache.refresh()
        self.assertFalse(cache.read()["stale"])
        cache.updated-=3
        self.assertTrue(cache.read()["stale"])
        cache.refresh();cache.error=True
        self.assertTrue(cache.read()["stale"])

    def test_display_http_returns_only_public_json(self):
        peer=self.connect(); self.result(peer)
        status,headers,body=self.http()
        self.assertEqual(status,200)
        self.assertIn("no-store",headers["Cache-Control"])
        parsed=json.loads(body)
        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["data"]["latest"]["actual"],"OK")
        self.assertNotIn(b"SECRET_ACCESS_88",body)
        self.assertNotIn(b'"expected"',body)
        self.assertNotIn("Access-Control-Allow-Origin",headers)

    def test_http_does_not_call_private_engine_snapshot(self):
        with patch.object(self.engine,"snapshot",side_effect=AssertionError("private snapshot forbidden")):
            self.assertEqual(self.http()[0],200)

    def test_known_assets_and_content_types(self):
        for path,mime in (("/","text/html"),("/app.js","text/javascript"),("/style.css","text/css"),("/favicon.svg","image/svg+xml")):
            status,headers,body=self.http(route=path)
            self.assertEqual(status,200,path)
            self.assertIn(mime,headers["Content-Type"])
            self.assertGreater(len(body),100)

    def test_live_html_cannot_select_demo_via_query(self):
        _,_,body=self.http(route="/?mode=demo&demo=1")
        self.assertIn(b'content="live"',body)
        self.assertNotIn(b'content="demo"',body)

    def test_unknown_and_path_traversal_not_exposed(self):
        for path in ("/data/competition.sqlite3","/../server.py","/%2e%2e/server.py","/docs/PROTOCOL_V1.md","/api/admin","/api/export","/static/../settings.json"):
            self.assertEqual(self.http(route=path)[0],404,path)

    def test_all_mutation_methods_rejected(self):
        for method in ("POST","PUT","DELETE","PATCH","OPTIONS"):
            self.assertEqual(self.http(method=method)[0],405)
        self.assertIsNone(self.engine.session)

    def test_head_no_body_and_correct_length(self):
        status,headers,body=self.http(method="HEAD",route="/")
        self.assertEqual(status,200)
        self.assertEqual(body,b"")
        self.assertGreater(int(headers["Content-Length"]),100)

    def test_long_path_rejected(self):
        self.assertEqual(self.http(route="/"+"x"*3000)[0],414)

    def test_branding_validation(self):
        self.assertEqual(validate_branding(" 标题 "," 副标题 "),("标题","副标题"))
        for title,sub in (("","a"),("a"*29,"x"),("a","b"*57),("a\nb","c")):
            with self.assertRaises(ValueError):validate_branding(title,sub)

    def test_branding_applied_to_cache(self):
        cache=SnapshotCache(self.engine,"旧标题","旧副标题")
        cache.set_branding("新标题","新副标题");cache.refresh()
        self.assertEqual(cache.read()["data"]["title"],"新标题")

    def test_http_bind_conflict_does_not_break_engine(self):
        self.http()
        other=DisplayService(self.engine)
        try:
            with self.assertRaises(OSError):other.start(*self.server.address)
            self.connect();self.assertTrue(self.snapshot()["connection"]["ready"])
        finally:
            other.stop()

    def test_display_restart(self):
        self.http();self.server.stop();self.server.start("127.0.0.1",0)
        self.assertEqual(self.http()[0],200)

    def test_display_id_not_equal_tcp_session_id(self):
        self.connect()
        self.assertNotEqual(self.snapshot()["session"]["display_id"],self.engine.session_id)
        self.assertEqual(len(self.snapshot()["session"]["display_id"]),12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
