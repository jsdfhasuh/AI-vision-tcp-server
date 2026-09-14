# 视觉比赛 TCP 裁判服务端 + 大屏 · v1.2.0-db

面向 Windows 裁判电脑的离线工具：包装箱双相机检测与螺钉漏打检测共用 TCP 服务端、中文裁判界面、只读网页大屏和本地 SQLite 数据库。客户端仍使用 UTF-8 JSON Lines / TCP v1。

## 推送到 GitHub

本包为待导入的完整源码，并不表示已经上传到远端。Windows 操作步骤见 [推送说明](PUSH_TO_GITHUB.md)。请先克隆目标仓库，再复制源码并提交，保留仓库原有的 `.git`。

## 启动

安装带 Tcl/Tk 的 Python 3.10+ 后完整解压，双击 `start_server.cmd`。运行不需要第三方 pip 包或独立数据库服务器；查看数据库管理页的SQLite运行时提示，正式部署前核验官方WAL修复版本。

```powershell
.\start_server.cmd
# 使用固定数据目录：
.\start_server.cmd --data-dir "D:\VisionCompetitionData"
# 允许其他大屏电脑访问网页：
.\start_server.cmd --data-dir "D:\VisionCompetitionData" --display-host 0.0.0.0 --display-port 9080
```

TCP默认9000，网页默认9080，各自独立。局域网TCP监听地址需在裁判窗口另外设置。默认只绑定本机，适合先本机联调。

## v1.2.0-db 已实现

- 正式/练习用途创建后锁定；旧数据保持LEGACY未分类，不猜测用途。
- 数据库管理窗口：队伍、用途、赛项、UTC日期筛选与场次分页；历史记录只读保护。
- 目标版本历史、界面设置入库、显式schema v2迁移；升级前备份；拒绝未知版本/自动降级。
- SQLite一致性在线备份、完整性/SHA256验证，以及恢复到新空目录，不覆盖原证据。
- 界面切场及正常退出自动备份；保留结果先提交再ACK、去重和异常恢复逻辑。
- 导出不再在写文件阶段持有引擎锁；新增快照边界与目标历史。
- 大屏显示练习/正式标签；“待结果”不冒充相机已开拍；取消不增加已接收参考进度。

数据库管理窗口不会开放到大屏HTTP服务。数据库和完整备份包含裁判预期与接入码，未加密，请仅交授权裁判。

## 使用流程

创建场次并确认练习/正式用途 → 配置指定箱型 → 启动TCP监听 → 参赛端握手/确认目标 → 裁判开始轮次 → 视觉软件发送最终OK/NG → 服务端保存后确认。开启主窗口“打开现场大屏”，只投屏网页，不镜像裁判桌面。

先使用 `start_mock_client.cmd` 与服务端联调。模拟客户端循环发送OK/NG，不读裁判预期，不代表真实视觉识别能力。`preview_display.html` 是完全独立且明确标注的演示页，不能作为比赛实时页面。

## 数据管理命令

```powershell
.\database_tools.cmd check
.\database_tools.cmd sessions --mode OFFICIAL --team "队伍" --limit 50
.\database_tools.cmd backup
.\database_tools.cmd restore --backup "D:\备份\某次备份文件夹" --to "D:\恢复到的新目录"
```

非默认数据目录：在子命令**前**加 `--data-dir`。例如：

```powershell
.\database_tools.cmd --data-dir "D:\VisionCompetitionData" backup --output "D:\备份"
```

正常使用只需点界面“数据库管理”，不必手写SQL。需要安装环境/EXE打包说明可查看下面文档；本交付包不包含已编译EXE。

## 文档

- [数据库、迁移、备份与恢复](docs/DATABASE_GUIDE.md)
- [本版测试报告与验收边界](docs/TEST_REPORT_V1_2_DB.md)
- [TCP完整协议](docs/PROTOCOL_V1.md)
- [投屏使用说明](docs/DISPLAY_GUIDE.md)
- [Windows部署与打包](docs/WINDOWS_DEPLOYMENT.md)
- [现场验收清单](docs/ACCEPTANCE_CHECKLIST.md)

相机画面上传、观众大字专属布局、完整候场/暂停/完赛流程、自动总分/成绩发布/排行榜尚未加入。本版不是上一次复查建议的全部实现。

## 升级注意

关闭旧程序，用新版 `--data-dir` 指向原数据目录。会先备份旧库再迁移。不要只搬运运行中的 `.sqlite3` 主文件，也不要再用旧版打开升级后的主库。具体回退方法见数据库说明。

## 自测

```powershell
py -3.12 -m unittest discover -s tests -p "test_*.py" -v
```

Tk、浏览器及真实网络联调脚本在 `tests/` 中，属于另外的验收检查；Playwright/Pillow只用于测试和截图，不是程序运行依赖。

## GitHub 仓库说明

此仓库导入自 `vision_competition_tcp_v1_2_db.zip`，程序源码与该交付版本一致。运行数据、数据库、备份、日志和自动生成的截图不提交；历史测试报告中提及的原始截图、JSON 和运行输出保留在原交付压缩包内。仓库保留全部测试脚本和数据库迁移测试夹具，可重新生成验证记录。

本次入库复验与文件范围见 [仓库导入说明](docs/REPOSITORY_IMPORT.md)。尚未构建 Windows EXE，也未完成 Windows 实机及现场大屏验收。
