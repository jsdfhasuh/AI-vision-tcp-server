# GitHub 仓库导入说明

本仓库导入自先前交付的 `vision_competition_tcp_v1_2_db.zip`，版本 `1.2.0-db`，数据库结构版本 2，TCP 协议版本 1。

原压缩包 SHA256：`5028d21900a73d5d6a9a0b758590ea56e9f3a0d0a40b936873d989a7fbf76013`。

## 导入范围

保留服务端及模拟客户端源码、中文裁判界面、只读大屏静态资源、独立演示页、Windows 启动/打包脚本、全部测试脚本、数据库迁移夹具和使用文档。程序逻辑未因入库而修改。

不跟踪运行数据库、WAL/SHM 文件、备份、导出、日志、个人设置、编译产物、生成的测试截图和测试结果文件。历史测试报告作为交付记录保留，引用的原始截图、JSON、ERR、TXT 在原压缩包中，而非远端 Git 目录。自动生成的 `RELEASE_MANIFEST.json` 是原压缩包清单，不适合作为仓库实时文件清单，因此不纳入 Git。

补充 `.gitignore` 和 `.gitattributes`；批处理文件在 Git 中规范化为 LF，Windows 检出时恢复 CRLF。未添加 CI 工作流或发布 EXE。

## 本次入库前复验

在当前 Linux 环境重新执行：

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

结果：125 项测试全部通过。Python 3.13.5，SQLite 3.46.1。此次复验是自动化回归，不是重新完成 Tk/浏览器的整套人工联调，也不是 Windows 实机验收。

Windows EXE 构建、真实相机、物理大屏、双屏 DPI、突然断电与长时间压力验收仍待现场完成。历史报告中的检查日期不代表本次入库复验时间。

## 运行

在 Windows 上安装含 Tcl/Tk 的 Python 后，从仓库根目录运行 `start_server.cmd`；数据库管理入口仍在裁判窗口。不要将私有裁判数据添加到这个公开仓库。
