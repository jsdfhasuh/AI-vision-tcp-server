"""SIMULATOR ONLY: does not operate a real camera or perform visual inspection.

Examples:
 python client_example.py --access-code ABCD1234
 python client_example.py --host 192.168.1.10 --access-code ABCD1234 --results OK,NG --delay 0.3
 python client_example.py --access-code ABCD1234 --repeat-same-id

Connects once. After disconnect, rerun it using the current session access code.
It requests the open round without resetting the server's original timer.
"""
from __future__ import annotations

import argparse
import queue
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from vision_client import VisionClient


def simulate_detection(round_message: dict[str, Any], verdict: str, delay: float, stop: threading.Event | None = None) -> tuple[str, dict[str, Any]]:
    """Replace this function with YOUR acquisition + final visual judgment.

    For action='arm', a real client must wait for its external/manual trigger;
    action='trigger' permits initiating a new acquisition immediately.
    The simulator intentionally responds to either action for bench testing.
    Never derive the verdict from server-supplied target values alone.
    """
    if stop is not None:
        if stop.wait(delay):
            raise RuntimeError("simulation stopped")
    else:
        time.sleep(delay)
    return verdict, {"source": "SIMULATOR_NOT_REAL_INSPECTION"}


def main() -> None:
    parser = argparse.ArgumentParser(description="视觉比赛模拟客户端（不是真实视觉检测程序）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--client-id", default="vision-01")
    parser.add_argument("--access-code", default=None)
    parser.add_argument("--results", default="OK,NG", help="循环发送的模拟结果，例如OK,NG,NG")
    parser.add_argument("--delay", type=float, default=0.3, help="模拟检测等待秒数")
    parser.add_argument("--repeat-same-id", action="store_true", help="同ID同内容发送两次，验证幂等重传")
    parser.add_argument("--new-id-duplicate", action="store_true", help="不同ID重复发送，验证重复检测")
    args = parser.parse_args()
    results = args.results.split(",")
    if any(v not in {"OK", "NG"} for v in results) or not 0 <= args.delay <= 3600:
        parser.error("--results只能用逗号分隔的大写OK/NG；delay须在0～3600秒。")
    access_code = args.access_code or input("输入裁判端当前场次的接入码：").strip()
    print("=== 模拟客户端：不采图、不检测，只用于TCP联调。Ctrl+C退出。 ===", flush=True)
    print("模拟器对arm/trigger均自动回应；真实视觉软件须按协议区分触发方式。", flush=True)
    completed: queue.Queue[tuple[dict[str, Any], str | None, dict[str, Any] | None, str | None]] = queue.Queue()
    seen: set[str] = set()
    cancelled: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    index = 0
    stop_event = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="simulated-vision")
    client = VisionClient(args.host, args.port, args.client_id, access_code)
    try:
        hello = client.connect()
        print(f"握手成功，场次：{hello['session_id']}，赛项：{hello['competition']}", flush=True)
        # A real implementation must apply camera/recognition target settings here
        # before acknowledging. This simulator only displays them.
        print(f"目标 v{hello['target_revision']}：{hello['target']}", flush=True)
        client.acknowledge_target(hello["target_revision"])
        client.request("get_target")
        client.request("get_state")
        while True:
            client.heartbeat()
            while not completed.empty():
                round_msg, verdict, details, error = completed.get()
                if round_msg["round_id"] in cancelled:
                    print("本轮已取消，丢弃尚未发送的检测任务结果。", flush=True)
                    continue
                if error:
                    print(f"检测异常，不伪造NG：{error}", flush=True)
                    continue
                assert verdict is not None
                result = client.make_result(round_msg, verdict, details)
                client.send(result)
                pending[result["msg_id"]] = {"message": result, "sent": time.monotonic(), "attempts": 1}
                print(f"SEND result {result['round_id'][:8]} = {verdict}", flush=True)
                if args.repeat_same_id:
                    client.send(result)
                if args.new_id_duplicate:
                    client.send({**result, "msg_id": uuid.uuid4().hex})
            # Retries preserve msg_id and the entire result, never a new detection.
            for mid, entry in list(pending.items()):
                if time.monotonic() - entry["sent"] >= 2:
                    if entry["attempts"] >= 3:
                        print(f"ACK未确认：{mid}。不得当作送达或重新检测；请核对裁判端日志。", flush=True)
                        del pending[mid]
                    else:
                        client.send(entry["message"])
                        entry["attempts"] += 1
                        entry["sent"] = time.monotonic()
            message = client.receive(0.15)
            if message is None:
                continue
            kind = message.get("type")
            if kind == "target":
                print(f"目标 v{message['target_revision']}：{message['target']}", flush=True)
                client.acknowledge_target(message["target_revision"])
            elif kind == "result_ack":
                pending.pop(message["reply_to"], None)
                print(f"ACK persisted={message['recorded']} duplicate={message['duplicate']}（不是评分反馈）", flush=True)
            elif kind == "error":
                pending.pop(message.get("reply_to", ""), None)
                print(f"SERVER ERROR {message['code']}: {message['message']}", flush=True)
            elif kind == "round_cancelled":
                cancelled.add(message["round_id"])
                for mid in list(pending):
                    if pending[mid]["message"]["round_id"] == message["round_id"]:
                        del pending[mid]
                print("裁判取消了当前轮次。", flush=True)
            elif kind in {"round", "state"}:
                round_msg = message if kind == "round" else message.get("active_round")
                if not round_msg or round_msg["round_id"] in seen:
                    continue
                seen.add(round_msg["round_id"])
                verdict = results[index % len(results)]
                index += 1
                print(f"收到轮次 {round_msg['round_id'][:8]}，action={round_msg['action']}；模拟值={verdict}", flush=True)
                def work(rm: dict[str, Any] = round_msg, value: str = verdict) -> None:
                    try:
                        outcome, details = simulate_detection(rm, value, args.delay, stop_event)
                        completed.put((rm, outcome, details, None))
                    except Exception as exc:
                        completed.put((rm, None, None, str(exc)))
                pool.submit(work)
    except KeyboardInterrupt:
        print("\n模拟客户端退出。", flush=True)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"连接/协议异常：{exc}\n请核对监听地址、接入码和客户端ID；修复后重新运行。", flush=True)
    finally:
        stop_event.set()
        client.close()
        pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
