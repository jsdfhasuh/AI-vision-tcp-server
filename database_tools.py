"""Referee-only database maintenance CLI. It never opens a Store to read history,
so inspection and backup do not mark live sessions as interrupted.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from competition.dbtools import inspect_database, backup_database, restore_backup, search_sessions


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉比赛数据库工具：检查、备份、只读历史查询、恢复到新目录")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="只读完整性检查，不恢复或改变场次")
    backup = sub.add_parser("backup", help="一致性在线备份")
    backup.add_argument("--output", type=Path)
    history = sub.add_parser("sessions", help="历史场次分页查询（日期按UTC）")
    for flag in ("team", "mode", "competition", "state", "date-from", "date-to"):
        history.add_argument("--" + flag, default="")
    history.add_argument("--limit", type=int, default=50)
    history.add_argument("--offset", type=int, default=0)
    restore = sub.add_parser("restore", help="验证备份并恢复到新/空目录；不覆盖原数据库")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--to", type=Path, required=True)
    args = parser.parse_args()
    database = args.data_dir.resolve() / "competition.sqlite3"
    try:
        if args.command == "check":
            result = inspect_database(database)
        elif args.command == "backup":
            result = {"backup_folder": str(backup_database(database, args.output or database.parent / "backups", reason="cli_manual"))}
        elif args.command == "sessions":
            result = search_sessions(database, **{k: getattr(args, k) for k in ("team", "mode", "competition", "state", "date_from", "date_to", "limit", "offset")})
        else:
            result = {"restored_database": str(restore_backup(args.backup, args.to)),
                      "note": "使用新版服务端 --data-dir 指向恢复目录启动；旧未完成轮次会标记中断，不恢复计时。"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.command == "check" and not result["integrity_ok"] else 0
    except Exception as exc:
        print("操作未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
