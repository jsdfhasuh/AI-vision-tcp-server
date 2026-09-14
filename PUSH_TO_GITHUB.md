# 将完整源码推送到 GitHub（Windows）

目标仓库：`https://github.com/jsdfhasuh/AI-vision-tcp-server`
目标分支：`main`
版本：`1.2.0-db`

这是完整源码包，不是补丁，也不是 EXE 安装包。服务端、网页前端、数据库建表/迁移/管理代码、测试及文档均已包含。实际比赛数据库、接入码、运行日志、备份、缓存及 Git 历史不在包内。

## 1. 解压源码包

解压后会得到 `AI-vision-tcp-server` 文件夹。其中可以直接看到 `server.py`、`competition`、`tests` 和 `start_server.cmd`。

建议解压在 `D:\比赛源码包`，然后将仓库克隆到另一个目录，例如 `D:\Projects`。这两个目录不要混用。

## 2. 克隆已有仓库

在准备存放 Git 仓库的文件夹中打开 PowerShell，执行：

```powershell
git clone --branch main https://github.com/jsdfhasuh/AI-vision-tcp-server.git
cd AI-vision-tcp-server
```

任意命令报错时先停止，不要继续后面的步骤。如果电脑尚未安装 Git，先安装 Git 并重新打开终端；GitHub 需要登录时在本机完成授权，不要把 Token 发给别人。

已有本地克隆时，不要重复克隆。先确保本地修改已经妥善保存，确认 `origin` 指向上面的仓库，再执行：

```powershell
git remote -v
git status
git switch main
git pull --ff-only origin main
```

工作区不是干净状态、分支不存在或者拉取失败时，先处理原因，不要继续覆盖文件。

## 3. 复制源码到仓库根目录

在资源管理器中，进入刚解压的 `AI-vision-tcp-server` 文件夹，复制**里面的全部文件和子文件夹**，粘贴到刚克隆的仓库根目录。

包含 `.gitignore` 和 `.gitattributes`，允许覆盖原来的 `.gitignore`。不要删除或替换克隆目录里的 `.git`。

正确结构应为：

```text
AI-vision-tcp-server/       <- Git 仓库根目录
├── .git/                  <- clone 创建，必须保留，不在源码包中
├── .gitignore
├── .gitattributes
├── README.md
├── PUSH_TO_GITHUB.md
├── server.py
├── competition/
│   ├── storage.py
│   ├── schema.py
│   ├── dbtools.py
│   └── web/
├── tests/
├── docs/
└── start_server.cmd
```

不要变成 `AI-vision-tcp-server/AI-vision-tcp-server/server.py`。

本次导入以此前只含初始化文件的仓库为基础。若发现远端后来增加了其他业务代码，请先核对差异，不要盲目覆盖。

## 4. 检查并推送

在克隆目录的 PowerShell 中逐条执行：

```powershell
git status --short
git add .
git diff --cached --stat
```

检查暂存清单，应只有源码、静态资源、测试、脚本和文档，不应有 `.sqlite3`、`.db`、日志、实际比赛备份或凭据。

确认后执行：

```powershell
git commit -m "feat: import v1.2.0-db TCP server, display and database management"
git push origin main
```

不使用 `--force`，不强制覆盖远端历史。如果推送被拒绝，先保留当前文件和提交，查看报错，不要改用强推。

如果提交提示未设置身份，只为当前仓库填写你自己的 Git 提交身份，再重新提交：

```powershell
git config user.name "你的名字"
git config user.email "你的 GitHub 提交邮箱"
```

上面的示例占位内容需要替换。

## 5. 核对是否完成

推送成功后刷新 GitHub 仓库的 `main` 分支，确认能看到 `server.py`、`competition/web`、数据库管理源码和 `tests`。仅下载或复制文件，不代表已经推送。

也可以比较以下命令输出的提交号：

```powershell
git rev-parse HEAD
git ls-remote origin refs/heads/main
```

第一条的提交号应与第二条行首的提交号相同。

## 自测及运行

源码包已在本次整理时重新执行 125 项自动化测试，全部通过；这不等同于 Windows 实机或相机、大屏现场验收。

有 Python 的电脑上可执行：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
.\start_server.cmd
```

或者双击 `run_tests.cmd` 和 `start_server.cmd`。服务端运行需要含 Tcl/Tk 的 Python；常规运行不需要 MySQL 服务或额外的 pip 依赖。详细部署条件见 `docs/WINDOWS_DEPLOYMENT.md`。

运行后自动产生本地数据库；不要手动强制把数据库加入 Git。程序初次运行自动建表，不需要从别处下载实际比赛数据库。
