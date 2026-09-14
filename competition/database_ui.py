"""Private referee database console. Nothing here is exposed by the public HTTP service."""
from __future__ import annotations
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from typing import Any, Callable

MODES = {"全部用途": "", "正式比赛": "OFFICIAL", "练习联调": "PRACTICE", "旧版未分类": "LEGACY"}
COMPS = {"全部赛项": "", "包装箱双相机检测": "packaging", "螺钉漏打检测": "screw"}
MODE_NAMES = {v: k for k, v in MODES.items()}
STATE_NAMES = {"OPEN": "进行中", "CLOSED": "已结束", "INTERRUPTED": "中断"}


class DatabaseWindow:
    def __init__(self, app: Any):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("比赛数据库管理 · 仅裁判可见")
        self.win.geometry("1150x720")
        self.win.minsize(960, 620)
        self.win.transient(app.root)
        self.offset, self.limit, self.total = 0, 50, 0
        self.records: dict[str, dict[str, Any]] = {}
        self.filters: dict[str, Any] = {}
        body = ttk.Frame(self.win, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="数据库 / 历史场次 / 一致性备份", font=(app.ui_font, 17, "bold")).pack(anchor="w")
        ttk.Label(body, text=str(app.store.path), foreground="#385370").pack(anchor="w", pady=(3, 9))
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="检查数据库", command=self.check).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="立即一致性备份", command=self.backup).pack(side="left", padx=6)
        ttk.Button(toolbar, text="恢复方法（不覆盖原库）", command=self.restore_help).pack(side="left", padx=6)
        ttk.Label(toolbar, text="无删除 / 改分入口；正式与练习用途创建后锁定。", foreground="#66768a").pack(side="right")
        frame = ttk.LabelFrame(body, text="历史筛选（日期按UTC，YYYY-MM-DD；留空不限）", padding=8)
        frame.pack(fill="x", pady=10)
        self.team = tk.StringVar()
        self.mode = tk.StringVar(value="全部用途")
        self.competition = tk.StringVar(value="全部赛项")
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        ttk.Label(frame, text="队伍包含").grid(row=0, column=0, padx=3)
        ttk.Entry(frame, textvariable=self.team, width=19).grid(row=0, column=1, padx=3)
        ttk.Combobox(frame, textvariable=self.mode, values=list(MODES), state="readonly", width=13).grid(row=0, column=2, padx=3)
        ttk.Combobox(frame, textvariable=self.competition, values=list(COMPS), state="readonly", width=20).grid(row=0, column=3, padx=3)
        ttk.Label(frame, text="日期从").grid(row=0, column=4, padx=3)
        ttk.Entry(frame, textvariable=self.date_from, width=12).grid(row=0, column=5, padx=3)
        ttk.Label(frame, text="至").grid(row=0, column=6, padx=3)
        ttk.Entry(frame, textvariable=self.date_to, width=12).grid(row=0, column=7, padx=3)
        ttk.Button(frame, text="查询", command=self.search).grid(row=0, column=8, padx=6)
        self.status = tk.StringVar(value="点击查询加载历史场次；长时检查与备份在工作线程执行。")
        ttk.Label(body, textvariable=self.status, foreground="#195c9e", wraplength=1080).pack(fill="x", pady=(0, 6))
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        cols = ("time", "team", "mode", "competition", "state", "rounds", "received")
        self.table = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse", height=9)
        for col, title, width in zip(cols, ("创建时间UTC", "队伍", "用途", "赛项", "状态", "发起轮次", "接收结果"), (182, 200, 110, 160, 82, 76, 76)):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, anchor="center", stretch=col == "team")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Double-1>", lambda _: self.open_session())
        pagination = ttk.Frame(body)
        pagination.pack(fill="x", pady=8)
        ttk.Button(pagination, text="上一页", command=lambda: self.page(-1)).pack(side="left")
        ttk.Button(pagination, text="下一页", command=lambda: self.page(1)).pack(side="left", padx=5)
        ttk.Button(pagination, text="查看所选场次（只读）", command=self.open_session).pack(side="right")
        self.details = ScrolledText(body, height=9, wrap="word", font=(app.ui_font, 10), state="disabled")
        self.details.pack(fill="x")
        self.set_details("数据库检查会显示结构版本、记录数量、完整性、磁盘空间和SQLite运行时。\n备份包含裁判预期与接入码，只交授权裁判。\n切换场次和正常退出时会自动备份；异常退出后，只恢复记录，不恢复上一轮计时。")

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    def set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def run(self, kind: str, fn: Callable[[], Any]) -> None:
        if self.app.busy or self.app.closing:
            self.status.set("裁判端正在处理其他操作，请稍后重试。")
            return
        self.status.set("处理中……（检测结果仍由独立网络线程接收）")
        def work() -> dict[str, Any]:
            try:
                return {"window": self, "kind": kind, "value": fn()}
            except Exception as exc:
                return {"window": self, "kind": "error", "value": str(exc)}
        self.app._submit(work, "database_result")

    def search(self) -> None:
        if self.app.busy:
            self.status.set("其他操作尚未完成，请稍后查询。")
            return
        self.offset = 0
        self.filters = {"team": self.team.get().strip(), "mode": MODES[self.mode.get()],
                        "competition": COMPS[self.competition.get()],
                        "date_from": self.date_from.get().strip(), "date_to": self.date_to.get().strip()}
        self.query()

    def query(self) -> None:
        filters = {**self.filters, "limit": self.limit, "offset": self.offset}
        self.run("search", lambda: self.app.store.search_sessions(**filters))

    def page(self, step: int) -> None:
        if self.app.busy:
            return
        next_offset = max(0, self.offset + step * self.limit)
        if step > 0 and next_offset >= self.total:
            return
        self.offset = next_offset
        self.query()

    def check(self) -> None:
        self.run("health", self.app.store.health)

    def backup(self) -> None:
        folder = filedialog.askdirectory(title="选择备份上级目录（会生成独立备份文件夹）", initialdir=self.app.data_dir, parent=self.win)
        if folder:
            self.run("backup", lambda: self.app.store.backup(Path(folder)))

    def receive(self, result: dict[str, Any]) -> None:
        if not self.alive():
            return
        kind, value = result["kind"], result["value"]
        if kind == "error":
            self.status.set("未完成：" + value)
            return
        if kind == "search":
            self.total, self.offset = value["total"], value["offset"]
            self.table.delete(*self.table.get_children())
            self.records = {r["id"]: r for r in value["sessions"]}
            for r in self.records.values():
                competition = next((k for k, v in COMPS.items() if v == r["competition"]), r["competition"])
                self.table.insert("", "end", iid=r["id"], values=(r["created_utc"][:19], r["team"], MODE_NAMES.get(r["mode"], r["mode"]), competition, STATE_NAMES.get(r["state"], r["state"]), r["rounds"], r["received"]))
            self.status.set(f"共 {self.total} 个场次 · 第 {self.offset // self.limit + 1} 页 · 每页 {self.limit} 条。双击进入主窗口只读查看。")
        elif kind == "health":
            self.status.set("数据库完整性检查：" + ("通过" if value["integrity_ok"] else "异常，请保留原数据"))
            self.set_details(json.dumps(value, ensure_ascii=False, indent=2))
        elif kind == "backup":
            self.status.set("备份完成：" + str(value))
            self.set_details(f"备份位置：{value}\n包含 database.sqlite3 和 manifest.json；已完成一致性复制、完整性检查与SHA256计算。\n只复制这个备份目录即可迁移。不要单独复制运行中的主数据库。\n清单用于校验文件变化，不是防篡改数字签名。")

    def open_session(self) -> None:
        items = self.table.selection()
        if not items:
            return
        row = self.records[items[0]]
        self.app._view_database_session(row["id"], f"数据库查询：{row['team']} [{MODE_NAMES.get(row['mode'], row['mode'])}] {row['id'][:8]}")

    def restore_help(self) -> None:
        messagebox.showinfo("恢复到新目录", "恢复不会覆盖原比赛记录。请先保留完整备份文件夹，再使用：\n\n"
            'py -3.12 database_tools.py restore --backup "备份文件夹" --to "D:\\比赛恢复数据"\n\n'
            '目标必须是新目录或空目录。恢复成功后启动：\n'
            'start_server.cmd --data-dir "D:\\比赛恢复数据"\n\n'
            "未完成轮次会标记中断，不能在重启后恢复原计时。不要将同一个数据目录同时交给两个服务端。", parent=self.win)
