"""Create a local password-hash file. Does not contact any network service."""
from __future__ import annotations
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from competition.web_auth import credential_record


def main() -> None:
    p = argparse.ArgumentParser(description='建立Web裁判管理员（密码不回显）')
    p.add_argument('--output', type=Path, default=Path('secrets/admin.json'))
    p.add_argument('--username', default='admin')
    args = p.parse_args()
    if args.output.exists():
        p.error('文件已存在，不覆盖。重置密码时生成新文件，停服替换后重新启动。')
    password = getpass.getpass('新密码（至少12位）：')
    if password != getpass.getpass('再次输入密码：'):
        p.error('两次输入不一致。')
    try:
        record = credential_record(args.username, password)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
    except (ValueError, OSError) as exc:
        p.error(str(exc))
    print(f'已生成密码哈希文件：{args.output}。请勿提交GitHub，服务账号必须能够读取。')

if __name__ == '__main__':
    main()
