"""Optional desktop smoke test, run separately from unittest.

Run: python tests/gui_smoke.py [--screenshot PATH]
Linux requires a display (for example xvfb-run); screenshot capture additionally
requires Pillow. Neither is required by the server or the automated tests.
"""
from __future__ import annotations

import argparse
import json
import http.client
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from competition.gui import App


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    temp = tempfile.TemporaryDirectory(prefix="vision_gui_qa_")
    data_dir = Path(temp.name)
    app = App(root, data_dir, display_port=0)
    errors = []
    root.report_callback_exception = lambda kind, value, tb: errors.append(f"{kind.__name__}: {value}")
    def wait_for(predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            root.update()
            if predicate():
                return
            time.sleep(.015)
        raise TimeoutError("GUI smoke condition not met")
    clients = []
    report = {}
    try:
        app.team.set("联调演示队伍")
        app._submit(lambda: app.engine.new_session("联调演示队伍", "vision-01", "packaging", "DEMO1234"), "new_session")
        wait_for(lambda: app.state.get("session") is not None and not app.busy)
        app._submit(lambda: app.service.start("127.0.0.1", 0))
        wait_for(lambda: app.service.address is not None and not app.busy)
        app.port.set(str(app.service.address[1]))
        for mode in ("packaging", "screw"):
            if mode == "screw":
                app._submit(lambda: app.engine.new_session("螺钉联调演示", "vision-01", "screw", "DEMO5678"), "new_session")
                wait_for(lambda: (app.state.get("session") or {}).get("competition") == "screw" and not app.busy)
                app.team.set("螺钉联调演示")
                app.competition.set("螺钉漏打检测")
            code = "DEMO1234" if mode == "packaging" else "DEMO5678"
            log = (data_dir / (mode + ".log")).open("w", encoding="utf-8")
            process = subprocess.Popen([sys.executable, "client_example.py", "--port", str(app.service.address[1]),
                                        "--access-code", code, "--delay", "0.03"], cwd=ROOT,
                                       stdout=log, stderr=subprocess.STDOUT)
            clients.append((process, log))
            wait_for(lambda: app.state.get("ready"))
            for i in range(10):
                expected = "OK" if i % 2 == 0 else "NG"
                case = ("合格品" if expected == "OK" else "侧面LOGO脏污") if mode == "packaging" else ("完整品" if expected == "OK" else "2号螺钉漏打")
                app.case.set(case)
                app.expected.set(expected)
                app._start_round()
                wait_for(lambda: app.state.get("stats", {}).get("received", 0) == i + 1 and not app.busy)
            report[mode] = app.state["stats"]
            def public_count():
                conn = http.client.HTTPConnection(*app.display.address, timeout=2)
                conn.request("GET", "/api/display")
                response = conn.getresponse()
                packet = json.loads(response.read())
                conn.close()
                return (packet["data"]["session"]["competition"] == mode
                        and packet["data"]["counts"]["received"] == 10)
            wait_for(public_count)
            report[mode]["public_display_received"] = 10
            assert report[mode]["total"] == 10 and report[mode]["matched"] == 10, report
            if mode == "packaging" and args.screenshot:
                from PIL import ImageGrab
                root.update()
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                bbox = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height())
                ImageGrab.grab(bbox=bbox).save(args.screenshot)
            process.terminate()
            process.wait(timeout=5)
            log.close()
            wait_for(lambda: app.state.get("client") == "未连接")
        assert not errors, errors
        report["tk_callback_errors"] = errors
    finally:
        for process, log in clients:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            log.close()
        app._close()
        for _ in range(500):
            try:
                root.update()
                if not root.winfo_exists():
                    break
            except tk.TclError:
                break
            time.sleep(.01)
        temp.cleanup()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
