> v1.2.0-db 更新：数据库用途区分、设置入库、版本迁移、备份恢复及运行时提示，请优先阅读 `DATABASE_GUIDE.md`；本版测试以 `TEST_REPORT_V1_2_DB.md` 为准。下文保留原有部署/投屏流程。

# v1.1 大屏补充

默认随裁判程序启动 `http://127.0.0.1:9080/`；标题在“展板设置”修改。完整投屏/远程观看说明见 `DISPLAY_GUIDE.md`。本版仍需要 Windows 实机验收，不提供已经编译的 EXE。

# Windows部署、网络联调与打包

## 1. 源码运行

安装带Tcl/Tk的Python。项目部署建议先选Python 3.12，并在实际比赛机器验证；本交付环境的自动化测试使用Linux/Python 3.13.5，不能代替Windows验收。

检查命令：

```powershell
py -3.12 --version
py -3.12 -m tkinter
```

第二条应弹出Tk测试窗口。若提示无法找到Python或Tk，需要修复Python安装；本程序不会通过pip安装Tk。

完整解压后双击 `start_server.cmd`。它优先尝试 `py -3.12`，否则尝试 `py -3`，没有Python Launcher时尝试 `python`。启动失败会保留错误提示窗口。

源码运行无需安装第三方pip包。第一次启动不会访问互联网。

## 2. 本机与局域网地址

本机联调：服务端监听 `127.0.0.1:9000`，模拟客户端连接同一地址。

两台电脑：用 `ipconfig` 确认裁判电脑比赛网卡的IPv4地址，优先绑定该具体地址。也可以监听 `0.0.0.0`，它表示所有本机IPv4网卡，不是客户端应该填写的目标地址。

例如服务器 `192.168.1.10`、客户端 `192.168.1.20`，参赛端使用：

```powershell
py -3.12 client_example.py --host 192.168.1.10 --port 9000 --access-code 实际接入码
```

两个示例IP仅为说明，须替换为现场真实IP。首次联调应先用模拟客户端，再接入真实视觉程序。

## 3. 检查端口与防火墙

在参赛电脑PowerShell中检查：

```powershell
Test-NetConnection 192.168.1.10 -Port 9000
```

`TcpTestSucceeded`为True仅表示TCP端口可连接，不表示身份握手、目标应用或检测结果协议已通过。端口探测会产生一个未握手连接，服务端会按策略关闭，它不等于参赛程序掉线。

若服务端正在正确监听但另一台电脑不能连接，检查网卡、子网、交换机隔离、端口与Windows防火墙。不要关闭整个防火墙。

经现场网络管理员确认后，可在裁判电脑的**管理员PowerShell**中创建仅允许指定参赛IP的规则。下例只作用于Private网络配置文件，端口与IP都必须按现场修改：

```powershell
New-NetFirewallRule `
  -DisplayName "Vision Competition TCP 9000" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 9000 `
  -RemoteAddress 192.168.1.20 `
  -Profile Private `
  -Action Allow
```

不要把公司/不可信网络随意改成Private来套用示例；Domain或Public网络应由管理员按相应策略放行。该规则只是防火墙通行条件，不给TCP协议增加加密。

比赛结束后不再需要，可按规则名称删除本例创建的规则：

```powershell
Remove-NetFirewallRule -DisplayName "Vision Competition TCP 9000"
```

主程序及启动脚本不会自动执行上述管理员操作。

## 4. 端口占用

若启动提示地址被占用，先检查是否已启动另一个服务端，或其他软件使用同一端口。改变端口时服务端和客户端要一致。

同一个数据目录只允许一个服务端进程打开，这是为了避免第二个进程把第一个进程的活动轮次当作异常退出恢复。确实需要并行多个独立席位时，必须分开端口和数据目录，且分别新建场次；本版界面不提供多席位集中管理。

```powershell
py -3.12 server.py --data-dir D:\VisionCompetitionSeat2\data
```

## 5. 打包Windows发行目录

本包没有预构建EXE。请在Windows上双击 `build_exe.cmd`，或查看脚本了解构建命令。PyInstaller按运行它的操作系统及Python环境收集依赖，不是跨操作系统编译器，因此不能把Linux上的测试等同于已产出Windows可执行文件。

脚本会创建 `.venv-build`，安装PyInstaller，然后使用：

```powershell
.\.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name VisionCompetitionServer server.py
```

输出在 `dist\VisionCompetitionServer\`，分发时复制整个文件夹，包括 `_internal`。推荐先使用该目录形式验收，避免误把缺依赖的单独EXE发给裁判电脑。

直接运行EXE默认把数据写入 `%LOCALAPPDATA%\VisionCompetitionTCP\data`，不依赖EXE旁边目录可写。需要便携数据目录时，在可写目录用命令行指定：

```powershell
.\VisionCompetitionServer.exe --data-dir D:\VisionCompetitionData
```

该打包脚本未在本交付的Linux环境执行。请在Windows目标机器检查图形界面、字体、DPI缩放、杀毒软件提示、网络监听和目录权限，不能仅以“打包完成”判定可上场。

## 6. 数据备份

正常关闭程序后复制整个数据目录。运行中不要只复制 `competition.sqlite3` 而漏掉可能存在的WAL文件。场次导出是在程序内取得一致快照，适合比赛记录交接；原始导出含身份接入码，限制接收人员。

不要把活动SQLite数据库放到网络共享目录、多人共用目录或自动同步盘；不要在测试中改系统时间来“调整耗时”。程序用单调时钟计时，但墙上时间跳变仍会影响人工查看日志的直观顺序，应以审计事件ID和保存的耗时为准。

## 官方依据

- Python Tkinter测试与发行说明：https://docs.python.org/3.12/library/tkinter.html
- PyInstaller操作系统依赖与构建方式：https://pyinstaller.org/en/stable/operating-mode.html
- Microsoft New-NetFirewallRule：https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule
- Microsoft Test-NetConnection：https://learn.microsoft.com/en-us/powershell/module/nettcpip/test-netconnection
