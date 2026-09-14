"""Chinese Tk desktop UI. Network, database and exports run off the UI thread."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from . import __version__
from .core import Engine
from .display import DisplayService, DEFAULT_TITLE, DEFAULT_SUBTITLE, validate_branding
from .network import Service
from .storage import Store
from .database_ui import DatabaseWindow, MODE_NAMES
from .dbtools import wal_runtime_warning

COMPETITIONS = {"包装箱双相机检测": "packaging", "螺钉漏打检测": "screw"}
CASES = {
    "packaging": ["合格品", "正面LOGO破损", "侧面LOGO破损", "正面LOGO脏污", "侧面LOGO脏污",
                  "产品型号错误", "标签类型错误", "标签位置异常", "标签方向异常", "强光合格品", "暗光合格品"],
    "screw": ["完整品", "1号螺钉漏打", "2号螺钉漏打", "3号螺钉漏打", "4号螺钉漏打",
              "强光完整品", "强光漏打品", "暗光完整品", "暗光漏打品"]}
STATUSES = {"WAITING": "等待结果", "RECEIVED": "已收到", "TIMEOUT": "超时未收",
            "LATE": "迟到结果", "CANCELLED": "裁判取消", "INTERRUPTED": "中断"}


class App:
    def __init__(self, root: tk.Tk, data_dir: Path, *, display_host: str = "127.0.0.1",
                 display_port: int = 9080, display_enabled: bool = True):
        self.root, self.data_dir = root, data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2500)
        self.dropped_ui = 0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judge-control")
        self.store = Store(data_dir / "competition.sqlite3")
        self.backup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="database-backup")
        self.pending_backups: set[str] = set()
        self.database_window: DatabaseWindow | None = None
        self.engine = Engine(self.store, self._notify)
        self.service = Service(self.engine)
        self.display: DisplayService | None = None
        self.display_error = "大屏服务未启用"
        self.busy = False
        self.closing = False
        self.dirty = True
        self.snapshot_pending = False
        self.state: dict[str, Any] = {}
        self.history_map: dict[str, str] = {}
        self.history_rows: list[dict[str, Any]] = []
        self.history_sid: str | None = None
        self.rows_signature: str = ""
        self.settings_path = data_dir / "settings.json"
        self.settings = self.store.load_settings(self.settings_path)
        self._build()
        if display_enabled:
            try:
                title, subtitle = validate_branding(self.display_title.get(), self.display_subtitle.get())
                self.display = DisplayService(self.engine, title, subtitle)
                self.display.start(display_host, display_port)
                self.display_error = ""
                self.footer.set(f"大屏展板：{self.display.url}  |  点击右上角打开；TCP端口与网页端口相互独立。")
            except Exception as exc:
                self.display_error = str(exc)
                self.footer.set(f"大屏服务未启动：{exc}。TCP裁判功能仍可使用；可用 --display-port 更换端口。")
        if self.store.migration_backup:
            self.footer.set(f"数据库已升级到v2，升级前备份：{self.store.migration_backup}")
        warning = wal_runtime_warning()
        if warning:
            self._append_log({"at": "启动检查", "direction": "SYSTEM", "kind": "SQLITE_RUNTIME", "peer": "", "text": warning})
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._poll)

    def _notify(self, kind: str, value: Any = None) -> None:
        try:
            self.events.put_nowait((kind, value))
        except queue.Full:
            self.dropped_ui += 1  # Only live display can drop; SQLite audit remains authoritative.

    def _var(self, key: str, default: str) -> tk.StringVar:
        return tk.StringVar(value=str(self.settings.get(key, default)))

    def _build(self) -> None:
        r = self.root
        r.title(f"视觉比赛 TCP 裁判服务端  v{__version__}")
        r.geometry("1280x880")
        r.minsize(1080, 760)
        r.configure(bg="#eef2f6")
        self.ui_font = "Microsoft YaHei UI" if os.name == "nt" else "Noto Sans CJK SC"
        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure(".", font=(self.ui_font, 10), background="#eef2f6")
        style.configure("TButton", padding=(10, 4))
        style.configure("TLabelframe", padding=(8, 4))
        style.configure("TLabelframe.Label", foreground="#233c59", font=(self.ui_font, 10, "bold"))
        style.configure("Accent.TButton", background="#195c9e", foreground="white", padding=(14, 6))
        style.map("Accent.TButton", background=[("active", "#2472b8")])
        style.configure("Treeview", rowheight=27, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", background="#dce7f2", font=(self.ui_font, 10, "bold"))
        header = tk.Frame(r, bg="#17324f", height=65)
        header.pack(fill="x")
        tk.Label(header, text="视觉比赛 · TCP 裁判服务端", font=(self.ui_font, 19, "bold"),
                 bg="#17324f", fg="white").pack(side="left", padx=18, pady=10)
        self.display_title = self._var("display_title", DEFAULT_TITLE)
        self.display_subtitle = self._var("display_subtitle", DEFAULT_SUBTITLE)
        ttk.Button(header, text="打开现场大屏 ↗", command=self._open_display).pack(side="right", padx=(5, 18), pady=12)
        ttk.Button(header, text="展板设置", command=self._display_settings).pack(side="right", padx=5, pady=12)
        ttk.Button(header, text="数据库管理", command=self._database_manager).pack(side="right", padx=5, pady=12)
        body = ttk.Frame(r, padding=(12, 8))
        body.pack(fill="both", expand=True)
        net = ttk.LabelFrame(body, text="1  网络监听（本机联调用127.0.0.1；局域网可改0.0.0.0）")
        net.pack(fill="x", pady=(0, 6))
        self.host = self._var("host", "127.0.0.1")
        self.port = self._var("port", "9000")
        ttk.Label(net, text="监听IP").pack(side="left")
        ttk.Entry(net, textvariable=self.host, width=17).pack(side="left", padx=(6, 12))
        ttk.Label(net, text="端口").pack(side="left")
        ttk.Entry(net, textvariable=self.port, width=7).pack(side="left", padx=6)
        ttk.Button(net, text="启动监听", command=self._start_service).pack(side="left", padx=5)
        ttk.Button(net, text="停止监听", command=self._stop_service).pack(side="left", padx=5)
        self.net_label = ttk.Label(net, text="未监听")
        self.net_label.pack(side="right", padx=8)

        session = ttk.LabelFrame(body, text="2  场次与参赛身份（切换队伍/赛项必须新建场次，旧记录保留）")
        session.pack(fill="x", pady=(0, 6))
        self.team = self._var("team", "练习队伍01")
        self.client_id = self._var("client_id", "vision-01")
        self.competition = self._var("competition", next(iter(COMPETITIONS)))
        if self.competition.get() not in COMPETITIONS:
            self.competition.set(next(iter(COMPETITIONS)))
        for col, (label, var, width) in enumerate((("队伍", self.team, 19), ("客户端ID", self.client_id, 17))):
            ttk.Label(session, text=label).grid(row=0, column=col * 2, padx=4)
            ttk.Entry(session, textvariable=var, width=width).grid(row=0, column=col * 2 + 1, padx=4)
        ttk.Label(session, text="赛项").grid(row=0, column=4, padx=5)
        ttk.Combobox(session, textvariable=self.competition, values=list(COMPETITIONS),
                     state="readonly", width=21).grid(row=0, column=5, padx=5)
        ttk.Button(session, text="新建场次 / 切换队伍", command=self._new_session).grid(row=0, column=6, padx=8)
        self.session_mode = self._var("session_mode", "练习联调")
        if self.session_mode.get() not in {"练习联调", "正式比赛"}:
            self.session_mode.set("练习联调")
        ttk.Label(session, text="场次用途").grid(row=1, column=0, padx=4, pady=3)
        ttk.Combobox(session, textvariable=self.session_mode, values=["练习联调", "正式比赛"], state="readonly", width=17).grid(row=1, column=1, padx=4)
        ttk.Label(session, text="创建后锁定；练习记录不会自动转为正式成绩。", foreground="#66768a").grid(row=1, column=2, columnspan=4, sticky="w", padx=4)
        self.identity = tk.StringVar(value="尚未新建场次")
        ttk.Label(session, textvariable=self.identity, foreground="#195c9e").grid(row=2, column=0, columnspan=6, sticky="w", padx=4, pady=(7, 1))
        ttk.Button(session, text="复制接入码", command=self._copy_code).grid(row=1, column=6, sticky="e")
        self.client_label = ttk.Label(session, text="客户端：未连接")
        self.client_label.grid(row=3, column=0, columnspan=7, sticky="w", padx=4, pady=(4, 0))

        target = ttk.LabelFrame(body, text="3  包装箱指定目标（空的型号/标签类型表示由参赛端按箱型映射；螺钉赛项不使用）")
        target.pack(fill="x", pady=(0, 6))
        self.box_type = self._var("box_type", "BOX_A")
        self.product_model = self._var("product_model", "")
        self.label_type = self._var("label_type", "")
        for label, var, width in (("箱型", self.box_type, 16), ("产品型号", self.product_model, 20), ("标签类型", self.label_type, 18)):
            ttk.Label(target, text=label).pack(side="left", padx=(4, 5))
            ttk.Entry(target, textvariable=var, width=width).pack(side="left", padx=(0, 8))
        ttk.Button(target, text="应用目标并下发", command=self._set_target).pack(side="left", padx=6)
        self.rev_label = ttk.Label(target, text="未设置")
        self.rev_label.pack(side="right", padx=6)

        control = ttk.LabelFrame(body, text="4  开始本轮测试（样件类别、预期结果和裁判备注不会下发给参赛端）")
        control.pack(fill="x", pady=(0, 6))
        self.case = tk.StringVar(value="合格品")
        self.expected = tk.StringVar(value="OK")
        self.timeout = self._var("timeout", "0")
        self.action = tk.StringVar(value="只布置轮次，外部/人工触发")
        self.notes = tk.StringVar()
        ttk.Label(control, text="样件类别").grid(row=0, column=0, padx=4)
        self.case_combo = ttk.Combobox(control, textvariable=self.case, values=CASES["packaging"], width=23)
        self.case_combo.grid(row=0, column=1, padx=4)
        self.case_combo.bind("<<ComboboxSelected>>", self._case_selected)
        ttk.Label(control, text="裁判预期").grid(row=0, column=2, padx=4)
        ttk.Combobox(control, textvariable=self.expected, values=["OK", "NG"], state="readonly", width=6).grid(row=0, column=3, padx=4)
        ttk.Label(control, text="时限ms").grid(row=0, column=4, padx=4)
        ttk.Entry(control, textvariable=self.timeout, width=9).grid(row=0, column=5, padx=4)
        ttk.Label(control, text="0 = 只记录，不判超时").grid(row=0, column=6, padx=4, sticky="w")
        ttk.Label(control, text="控制方式").grid(row=1, column=0, padx=4, pady=7)
        ttk.Combobox(control, textvariable=self.action, values=["只布置轮次，外部/人工触发", "收到本轮命令即触发检测"],
                     state="readonly", width=27).grid(row=1, column=1, padx=4, sticky="w")
        ttk.Label(control, text="备注").grid(row=1, column=2, padx=4)
        ttk.Entry(control, textvariable=self.notes, width=30).grid(row=1, column=3, columnspan=4, padx=4, sticky="we")
        actions = ttk.Frame(control)
        actions.grid(row=0, column=7, rowspan=2, padx=(8, 4))
        self.start_button = ttk.Button(actions, text="开始本轮", style="Accent.TButton", command=self._start_round)
        self.start_button.pack(side="left", padx=5)
        ttk.Button(actions, text="取消本轮", command=self._cancel_round).pack(side="left", padx=5)
        self.result_text = tk.StringVar(value="尚无结果  |  产品NG ≠ 检测错误")
        ttk.Label(control, textvariable=self.result_text, font=(self.ui_font, 13, "bold"),
                  foreground="#17324f").grid(row=2, column=0, columnspan=8, sticky="w", padx=5, pady=(2, 5))
        ttk.Label(control, text="计时：服务端开始本轮 → 完整结果进入服务端处理；不是纯推理耗时。结果以客户端最终汇总为准。",
                  foreground="#66768a").grid(row=3, column=0, columnspan=8, sticky="w", padx=5)

        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True)
        records = ttk.Frame(notebook, padding=6)
        logs = ttk.Frame(notebook, padding=6)
        notebook.add(records, text="  测试记录与导出  ")
        notebook.add(logs, text="  原始通信与事件  ")
        toolbar = ttk.Frame(records)
        toolbar.pack(fill="x", pady=(0, 5))
        self.history = tk.StringVar(value="当前场次")
        self.history_combo = ttk.Combobox(toolbar, textvariable=self.history, values=["当前场次"], state="readonly", width=59)
        self.history_combo.pack(side="left")
        self.history_combo.bind("<<ComboboxSelected>>", self._select_history)
        ttk.Button(toolbar, text="导出所选场次", command=self._export).pack(side="right", padx=4)
        ttk.Button(toolbar, text="打开数据目录", command=self._open_data).pack(side="right", padx=4)
        self.stats_text = tk.StringVar(value="尚无测试记录。连续测试请逐轮开始并保留不少于10轮有效记录，最终评分由裁判确认。")
        ttk.Label(records, textvariable=self.stats_text, foreground="#385370").pack(fill="x", pady=(0, 5))
        table_frame = ttk.Frame(records)
        table_frame.pack(fill="both", expand=True)
        columns = ["number", "case", "expected", "actual", "matched", "status", "ms", "retry", "duplicate", "error", "disconnect"]
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=4)
        headings = ["轮次", "样件类别", "预期", "实际", "结果核对", "接收状态", "耗时ms", "重传", "重复", "协议错", "断线"]
        widths = [50, 205, 50, 50, 84, 90, 90, 55, 55, 60, 55]
        for col, heading, width in zip(columns, headings, widths):
            self.table.heading(col, text=heading)
            self.table.column(col, width=width, anchor="center", stretch=(col == "case"))
        self.table.tag_configure("mismatch", foreground="#a72323")
        self.table.tag_configure("warning", foreground="#9b5c00")
        self.table.tag_configure("match", foreground="#126646")
        self.table.bind("<Double-1>", self._detail)
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        ttk.Label(records, text="双击记录查看完整测试ID、目标快照和备注。表格最多显示最新1000轮，导出包含整场全部记录。",
                  foreground="#66768a").pack(side="bottom", fill="x", pady=(5, 0), before=table_frame)
        ttk.Label(logs, text="实时窗口只保留最近1500行；完整原始字节保存在SQLite及导出的audit.jsonl。日志含接入码，请仅交授权裁判。",
                  foreground="#66768a").pack(fill="x", pady=(0, 5))
        self.log_widget = ScrolledText(logs, height=5, wrap="word", font=("Consolas", 10), state="disabled")
        self.log_widget.pack(fill="both", expand=True)
        self.footer = tk.StringVar(value=f"本地数据：{self.data_dir}  |  不自动计算评分表总分")
        ttk.Label(body, textvariable=self.footer, foreground="#53667b").pack(side="bottom", fill="x", pady=(6, 0), before=notebook)

    def _save_settings(self) -> None:
        data = {k: getattr(self, k).get() for k in ("host", "port", "team", "client_id", "competition", "box_type", "product_model", "label_type", "timeout", "display_title", "display_subtitle", "session_mode")}
        try:
            self.store.save_settings(data)
        except sqlite3.Error as exc:
            self.engine.fail(exc)
            raise OSError("数据库设置保存失败：" + str(exc)) from exc

    def _database_manager(self) -> None:
        if self.database_window and self.database_window.alive():
            self.database_window.win.lift()
            return
        self.database_window = DatabaseWindow(self)

    def _view_database_session(self, sid: str, label: str) -> None:
        if self.busy or self.closing:
            return
        if sid == (self.state.get("session") or {}).get("id"):
            self.history_sid = None
            self.history.set("当前场次")
            self.dirty = True
            return
        self.history_sid = sid
        self.history.set(label)
        self.start_button.state(["disabled"])
        self.footer.set("正在只读查看历史场次；恢复操作请在历史下拉框选择“当前场次”。")
        self._submit(lambda: {"sid": sid, "rows": self.store.rows(sid), "stats": self.store.stats(sid)}, "history")

    def _queue_auto_backup(self, sid: str) -> None:
        if sid in self.pending_backups or self.closing:
            return
        if len(self.pending_backups) >= 4:
            self.events.put(("auto_backup", {"sid": sid, "path": None, "error": "自动备份队列已满，未排入本次备份；请在数据库管理中手动补做。"}))
            return
        self.pending_backups.add(sid)
        def work() -> None:
            try:
                path = self.store.backup(reason="session_closed:" + sid)
                self.events.put(("auto_backup", {"sid": sid, "path": str(path), "error": None}))
            except Exception as exc:
                self.events.put(("auto_backup", {"sid": sid, "path": None, "error": str(exc)}))
        self.backup_executor.submit(work)

    def _history_readonly(self) -> bool:
        if self.history_sid:
            messagebox.showwarning("历史场次只读", "当前正在查看历史场次，请先在历史下拉框切回“当前场次”。", parent=self.root)
            return True
        return False

    def _submit(self, action: Callable[[], Any], result_kind: str = "done") -> None:
        if self.busy or self.closing:
            return
        self.busy = True
        self.start_button.state(["disabled"])
        def job() -> None:
            try:
                result = action()
                self.events.put((result_kind, result))
            except sqlite3.Error as exc:
                self.engine.fail(exc)
                self.events.put(("error", str(exc)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("command_finished", None))
        self.executor.submit(job)

    def _open_display(self) -> None:
        if not self.display or not self.display.address:
            messagebox.showwarning("大屏展板", "展板未启动：" + self.display_error + "\nTCP裁判功能不受影响。更换网页端口后重启，例如：\nstart_server.cmd --display-port 9081", parent=self.root)
            return
        try:
            if not webbrowser.open(self.display.url):
                raise OSError("未能自动打开默认浏览器")
        except Exception as exc:
            messagebox.showinfo("打开大屏", f"{exc}\n请在浏览器打开：{self.display.url}", parent=self.root)

    def _display_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("现场展板设置（仅裁判端可修改）")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        title = tk.StringVar(value=self.display_title.get())
        subtitle = tk.StringVar(value=self.display_subtitle.get())
        for row, (label, var) in enumerate((("比赛标题（最多28字）", title), ("副标题（最多56字）", subtitle))):
            ttk.Label(frame, text=label).grid(row=row * 2, column=0, sticky="w", pady=(6, 4))
            ttk.Entry(frame, textvariable=var, width=65).grid(row=row * 2 + 1, column=0, sticky="ew")
        address = self.display.url if self.display and self.display.address else "未启动：" + self.display_error
        ttk.Label(frame, text="本机展板地址：" + address, foreground="#195c9e").grid(row=4, column=0, sticky="w", pady=(18, 8))
        ttk.Label(frame, text="投屏建议：Windows使用扩展屏，只把展板浏览器拖到大屏，点击全屏。\n不要镜像裁判桌面；裁判接入码与预设答案不应投放到观众屏。\n局域网访问：启动时加 --display-host 0.0.0.0，并使用服务器实际IP。\n观众页没有任何裁判操作接口；更改标题会同步到所有展板。",
                  justify="left", foreground="#53667b").grid(row=5, column=0, sticky="w", pady=(0, 16))
        def apply() -> None:
            try:
                t, sub = validate_branding(title.get(), subtitle.get())
                self.display_title.set(t)
                self.display_subtitle.set(sub)
                self._save_settings()
                if self.display:
                    self.display.cache.set_branding(t, sub)
                dialog.destroy()
            except (ValueError, OSError) as exc:
                messagebox.showerror("展板设置", str(exc), parent=dialog)
        ttk.Button(frame, text="保存并更新展板", command=apply).grid(row=6, column=0, sticky="e")
        dialog.grab_set()

    def _start_service(self) -> None:
        try:
            host, port = self.host.get().strip(), int(self.port.get())
            if not 1 <= port <= 65535:
                raise ValueError("端口必须为1～65535。")
            self._save_settings()
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return
        self._submit(lambda: self.service.start(host, port))

    def _stop_service(self) -> None:
        if self.state.get("active_id") and not messagebox.askyesno("停止监听", "本轮仍在等待。停止将标记为中断，确认继续？", parent=self.root):
            return
        self._submit(self.service.stop)

    def _new_session(self) -> None:
        if self.state.get("session") and not messagebox.askyesno("新建场次", "旧场次保留，新场次将更换接入码并断开旧客户端，确认继续？", parent=self.root):
            return
        team, client, mode = self.team.get(), self.client_id.get(), COMPETITIONS[self.competition.get()]
        purpose = "OFFICIAL" if self.session_mode.get() == "正式比赛" else "PRACTICE"
        if purpose == "OFFICIAL" and not messagebox.askyesno("创建正式场次", f"确认以正式比赛身份创建 {team} 的场次？用途创建后不能更改。", parent=self.root):
            return
        self._submit(lambda: self.engine.new_session(team, client, mode, mode=purpose), "new_session")

    def _set_target(self) -> None:
        if self._history_readonly():
            return
        args = (self.box_type.get(), self.product_model.get(), self.label_type.get())
        self._submit(lambda: self.engine.set_target(*args))

    def _copy_code(self) -> None:
        session = self.state.get("session")
        if session:
            self.root.clipboard_clear()
            self.root.clipboard_append(session["access_code"])
            self.footer.set("接入码已复制。请只发给当前参赛客户端。")

    def _case_selected(self, event: Any = None) -> None:
        name = self.case.get()
        # This is merely a judge-side preset; the judge may override it before starting.
        self.expected.set("OK" if name in {"合格品", "完整品", "强光合格品", "暗光合格品", "强光完整品", "暗光完整品"} else "NG")

    def _start_round(self) -> None:
        if self._history_readonly():
            return
        session = self.state.get("session")
        if session:
            if (self.team.get().strip() != session["team"] or self.client_id.get().strip() != session["client_id"]
                    or COMPETITIONS[self.competition.get()] != session["competition"]
                    or ("OFFICIAL" if self.session_mode.get() == "正式比赛" else "PRACTICE") != session.get("mode", "PRACTICE")):
                messagebox.showerror("尚未应用场次", "队伍/客户端ID/赛项已编辑但尚未应用，请先新建场次。", parent=self.root)
                return
            if session["competition"] == "packaging":
                edited = {"box_type": self.box_type.get().strip(), "product_model": self.product_model.get().strip(), "label_type": self.label_type.get().strip()}
                if edited != session["target"]:
                    messagebox.showerror("尚未应用目标", "包装箱目标已编辑但尚未应用，请先点击“应用目标并下发”。", parent=self.root)
                    return
        try:
            timeout = int(self.timeout.get())
        except ValueError:
            messagebox.showerror("参数错误", "时限必须填写整数毫秒；0表示不判超时。", parent=self.root)
            return
        case, expected, notes = self.case.get(), self.expected.get(), self.notes.get()
        action = "arm" if self.action.get().startswith("只") else "trigger"
        self._submit(lambda: self.engine.start_round(case, expected, timeout, action, notes))

    def _cancel_round(self) -> None:
        if self._history_readonly():
            return
        reason = simpledialog.askstring("取消当前轮", "填写取消原因（会保存到审计日志）：", parent=self.root)
        if reason:
            self._submit(lambda: self.engine.cancel_round(reason))

    def _select_history(self, event: Any = None) -> None:
        if self.busy:
            self.root.after(150, self._select_history)
            return
        sid = self.history_map.get(self.history.get())
        if sid is None:
            self.history_sid = None
            self.dirty = True
            return
        self.history_sid = sid
        self.start_button.state(["disabled"])
        def read() -> dict[str, Any]:
            return {"sid": sid, "rows": self.store.rows(sid), "stats": self.store.stats(sid)}
        self._submit(read, "history")

    def _export(self) -> None:
        sid = self.history_sid or (self.state.get("session") or {}).get("id")
        if not sid:
            messagebox.showinfo("导出", "请先选择一个场次。", parent=self.root)
            return
        path = filedialog.askdirectory(title="选择导出文件夹的上级目录", parent=self.root)
        if path:
            self._submit(lambda: self.engine.export(sid, Path(path)), "exported")

    def _open_data(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(self.data_dir))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.data_dir)])
        except OSError as exc:
            messagebox.showerror("打开目录", str(exc), parent=self.root)

    def _detail(self, event: Any = None) -> None:
        selected = self.table.selection()
        if not selected:
            return
        rows = self.history_rows if self.history_sid else self.state.get("rows", [])
        row = next((r for r in rows if r["id"] == selected[0]), None)
        if row:
            win = tk.Toplevel(self.root)
            win.title("本轮完整记录（只读）")
            win.geometry("800x540")
            widget = ScrolledText(win, wrap="word", font=("Consolas", 11))
            widget.pack(fill="both", expand=True)
            widget.insert("end", json.dumps(row, ensure_ascii=False, indent=2))
            widget.configure(state="disabled")

    def _request_snapshot(self) -> None:
        if self.snapshot_pending or self.closing:
            return
        self.snapshot_pending = True
        self.dirty = False
        def read() -> None:
            try:
                self.events.put(("snapshot", self.engine.snapshot()))
            except Exception as exc:
                self.events.put(("snapshot_error", str(exc)))
        self.executor.submit(read)

    def _render(self, state: dict[str, Any]) -> None:
        self.state = state
        session = state.get("session")
        self.net_label.config(text="监听：" + state["listening"])
        if session:
            self.identity.set(f"当前：[{MODE_NAMES.get(session.get('mode'), '未分类')}] {session['team']}  /  {session['competition']}  |  场次 {session['id'][:8]}  |  接入码：{session['access_code']}")
            self.rev_label.config(text=(f"已应用 v{session['target_revision']}: {session['target'].get('box_type', '')}" if session["competition"] == "packaging" else "螺钉赛项不使用目标"))
            self.case_combo.configure(values=CASES[session["competition"]])
        self.client_label.config(text=f"客户端：{state['client']}  |  " + ("目标版本已确认，可测试" if state["ready"] else "尚未就绪"))
        rows = state.get("rows", [])
        if rows:
            last = rows[0]
            match = "核对一致" if last["matched"] == 1 else "核对不一致" if last["matched"] == 0 else "尚无有效结果"
            self.result_text.set(f"第 {last['number']} 轮  |  实际：{last['actual'] or '—'}  |  {match}  |  {STATUSES[last['status']]}"
                                 + (f"  |  {last['elapsed_ms']:.1f} ms" if last["elapsed_ms"] is not None else ""))
        else:
            self.result_text.set("尚无结果  |  产品NG ≠ 检测错误")
        self.history_map = {f"{s['created_utc'][:19]}  {s['team']}  [{s.get('mode', 'LEGACY')} / {s['competition']}]  {s['id'][:8]}": s["id"] for s in state["sessions"] if not session or s["id"] != session["id"]}
        self.history_combo.configure(values=["当前场次", *self.history_map])
        if not self.history_sid:
            self._render_rows(rows, state.get("stats", {}))
        if state.get("fatal"):
            self.footer.set(state["fatal"])
        self.start_button.state(["!disabled"] if state["ready"] and not state["active_id"] and not self.history_sid and not self.busy and not state.get("fatal") else ["disabled"])

    def _render_rows(self, rows: list[dict[str, Any]], stats: dict[str, int]) -> None:
        signature = repr(rows)
        if signature != self.rows_signature:
            selected = self.table.selection()
            self.table.delete(*self.table.get_children())
            for row in rows:
                matched = "一致" if row["matched"] == 1 else "不一致" if row["matched"] == 0 else "—"
                ms = f"{row['elapsed_ms']:.1f}" if row["elapsed_ms"] is not None else "—"
                tag = "warning" if row["status"] in {"TIMEOUT", "LATE", "CANCELLED", "INTERRUPTED"} else "mismatch" if row["matched"] == 0 else "match" if row["matched"] == 1 else ""
                values = [row["number"], row["case_name"], row["expected"], row["actual"] or "—", matched,
                          STATUSES[row["status"]], ms, row["retries"], row["duplicates"], row["protocol_errors"], row["disconnects"]]
                self.table.insert("", "end", iid=row["id"], values=values, tags=(tag,))
            if selected and self.table.exists(selected[0]):
                self.table.selection_set(selected[0])
            self.rows_signature = signature
        self.stats_text.set(f"轮次 {stats.get('total', 0)}  |  有效报文 {stats.get('received', 0)}  |  结果一致 {stats.get('matched', 0)}"
                            f"  |  超时未收 {stats.get('timeout', 0)}  |  迟到 {stats.get('late', 0)}"
                            f"  |  重传 {stats.get('retries', 0)}  |  重复 {stats.get('duplicates', 0)}"
                            f"  |  协议错 {stats.get('protocol_errors', 0)}  |  断线 {stats.get('disconnects', 0)}")

    def _append_log(self, data: dict[str, Any]) -> None:
        w = self.log_widget
        w.configure(state="normal")
        w.insert("end", f"{data['at']}  {data['direction']}  {data['kind']}  {data['peer']}\n{data['text']}\n")
        lines = int(w.index("end-1c").split(".")[0])
        if lines > 1500:
            w.delete("1.0", f"{lines - 1500}.0")
        w.see("end")
        w.configure(state="disabled")

    def _poll(self) -> None:
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "dirty":
                self.dirty = True
            elif kind == "event":
                self._append_log(value)
            elif kind == "snapshot":
                self.snapshot_pending = False
                self._render(value)
            elif kind == "snapshot_error":
                self.snapshot_pending = False
                self.footer.set("读取状态失败：" + value)
            elif kind == "command_finished":
                self.busy = False
                self.dirty = True
            elif kind == "new_session":
                self.history.set("当前场次")
                self.history_sid = None
                self.session_mode.set(MODE_NAMES.get(value.get("mode"), "练习联调"))
                self.case.set(CASES[value["competition"]][0])
                self.expected.set("OK")
                # Reflect the newly-created session defaults, not stale editable fields.
                target = value["target"]
                self.box_type.set(target.get("box_type", ""))
                self.product_model.set(target.get("product_model", ""))
                self.label_type.set(target.get("label_type", ""))
                try:
                    self._save_settings()
                except OSError as exc:
                    self.footer.set("界面设置未保存：" + str(exc))
            elif kind == "history" and value["sid"] == self.history_sid:
                self.history_rows = value["rows"]
                self._render_rows(value["rows"], value["stats"])
            elif kind == "database_result":
                value["window"].receive(value)
            elif kind == "session_closed":
                self._queue_auto_backup(value)
            elif kind == "auto_backup":
                self.pending_backups.discard(value["sid"])
                if value["error"]:
                    self.footer.set("自动备份失败（比赛主库仍独立保存）：" + value["error"])
                    if not self.closing:
                        messagebox.showwarning("自动备份未完成", value["error"] + "\n请检查剩余空间和目录权限，稍后在数据库管理中手动备份。", parent=self.root)
                else:
                    self.footer.set("数据库自动备份完成：" + value["path"])
            elif kind == "exported":
                messagebox.showinfo("导出完成", f"已导出：\n{value}\n\n包含CSV、汇总JSON、原始通信审计和SHA256清单。\n原始通信含接入码，请妥善保存。", parent=self.root)
            elif kind == "error":
                if not self.closing:
                    messagebox.showerror("操作未完成", value, parent=self.root)
            elif kind == "fatal":
                self.footer.set(value)
                if not self.closing:
                    messagebox.showerror("证据保存异常", value, parent=self.root)
            elif kind == "shutdown_backup_error":
                messagebox.showwarning("退出前备份未完成", value, parent=self.root)
            elif kind == "closed":
                self.executor.shutdown(wait=False)
                self.root.destroy()
                return
        if self.dropped_ui:
            self.footer.set(f"实时显示队列有 {self.dropped_ui} 条未展示；完整证据请查看数据库/导出日志。")
        if self.dirty:
            self._request_snapshot()
        self.root.after(100, self._poll)

    def _close(self) -> None:
        if self.closing:
            return
        if self.state.get("active_id") and not messagebox.askyesno("关闭程序", "当前测试未结束，关闭会记录为中断，确认退出？", parent=self.root):
            return
        self.closing = True
        self.start_button.state(["disabled"])
        self.footer.set("正在保存并关闭连接……")
        try:
            self._save_settings()
        except OSError:
            pass
        def close() -> None:
            try:
                if self.display:
                    self.display.stop()
                self.service.stop()
                self.engine.close_session()
                self.backup_executor.shutdown(wait=True)
                if self.engine.session and not self.engine.fatal:
                    try:
                        self.store.backup(reason="normal_shutdown")
                    except Exception as exc:
                        self.events.put(("shutdown_backup_error", "退出前自动备份未完成；主数据库仍需保留：" + str(exc)))
            finally:
                self.backup_executor.shutdown(wait=True)
                self.store.close()
                self.events.put(("closed", None))
        self.executor.submit(close)
