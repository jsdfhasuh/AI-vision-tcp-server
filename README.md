# 视觉比赛 TCP 服务端 · v1.3.0-web

**纯 Web 裁判后台 + 只读现场大屏 + TCP v1 + SQLite + Docker 构建配置。**

服务器不再需要桌面、Tkinter、VNC 或 noVNC。裁判用浏览器操作 `/admin/`，观众访问 `/board/`；两者使用不同的数据接口。比赛状态机、TCP 协议和 schema v2 数据库沿用 v1.2.0-db。旧 Windows 桌面入口 `server.py` / `start_server.cmd` 仍保留，其界面版本仍为 1.2.0-db。

本版从仓库 `main` 的 `1d3992d6f06def52b1221a6e557d5b6dc164eeab` 开发。**交付物是完整源码和 Docker 构建配置，不是已经发布的镜像；本次没有修改远端仓库，也没有部署到 Oracle。**

## 首次部署

见 **[Oracle / Docker 部署指南](docs/DOCKER_ORACLE.md)**。指南包含首次创建管理员、目录权限、SSH 隧道、HTTPS 反向代理、升级和恢复。不要跳过密码文件初始化，直接运行 `docker compose up`。

安全默认值：网页和参赛 TCP 只发布到服务器的 `127.0.0.1`；通过 SSH 隧道联调。管理员没有默认密码；本地初始化工具只保存加盐密码哈希。实际数据目录与镜像分离。

```text
裁判浏览器 → /admin/ → 登录 / CSRF / 状态校验 → 同一个比赛引擎
参赛软件   → TCP 9000 → JSON Lines v1        → 同一个比赛引擎 → SQLite
观众浏览器 → /board/ → /api/display         → 仅白名单公开快照
```

## 已实现

| 模块 | 能力 |
|---|---|
| 网页裁判 | 新建练习/正式场次、选择赛项、指定箱型、查看接入码、开始/取消轮次、结束场次 |
| 检测记录 | 预期与实际分别显示，NG 可以是正确判断；未收到/超时/中断不会自动补 NG |
| 操作保护 | 登录、HttpOnly 会话 Cookie、同源/CSRF 检查、请求编号去重、旧页面状态拒绝 |
| 历史与维护 | 只读历史分页、后台备份/导出/完整性检查、登录后下载结果 |
| 大屏 | 沿用公开白名单，不返回裁判预期、样件类别、接入码、备注和内部原始日志 |
| 容器 | 单进程、非 root、只读根文件系统、数据挂载、健康检查、正常停机清理 |

一个实例同一时间只管理一个活动场次。包装箱和螺钉赛项切换时新建场次。**不是多赛道并发调度系统，也不是多管理员权限系统。** 多浏览器可以访问同一后台，但共享一个管理员身份和引擎。

## 非 Docker 的本机开发

Web 入口建议使用 Python 3.13；以下依赖锁按该运行环境验证。Windows 原桌面运行方式不受影响。

```bash
python -m venv .venv
# Linux: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-web.txt
python tools/init_web_admin.py
python web_server.py
```

浏览器使用配置中的**准确主机名**：默认 `http://localhost:9080/admin/` 和 `http://localhost:9080/board/`。默认配置下不要改用 `127.0.0.1` 作为浏览器地址，否则 Host 校验会拒绝。参赛软件连接 `127.0.0.1:9000`。

创建场次 → 设置箱型 → 查看接入码 → 运行 `client_example.py --help` 或原 `start_mock_client.cmd` → 客户端握手并确认目标版本 → 开始本轮 → 自动接收最终 OK/NG。先用模拟器联调，再接真实视觉程序。

`.env` 由 Docker Compose 读取；直接运行 Python 不会自动加载 `.env`，请设置系统环境变量或使用 `web_server.py --help` 中的参数。

## 数据与兼容性

数据在 `/data/competition.sqlite3` 及其附属目录中；凭据单独挂载到 `/run/secrets/admin.json`。不能让桌面版和 Web 版同时写同一数据库，也不能用多 worker 或多个容器共享同一个活动比赛引擎。

切场/结束场次自动排队备份，正常退出尽力完成最终备份；失败写日志，不假装完成。启动后未完成的旧轮次标记中断，不恢复旧计时。维护任务列表保存在内存，重启后清空，但已生成文件仍在磁盘。没有自动删除证据/备份的功能，需要裁判按制度归档。

数据库包含裁判私有数据和参赛接入码，**未加密**；密码哈希文件不在数据库备份内。原 TCP v1 仍是明文 TCP，HTTPS 只保护网页，参赛 TCP 应经 SSH/VPN 或可信隔离网络连接。

## 验证

```bash
python -m pip install -r requirements-web-test.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/web_http_smoke.py --output-dir /tmp/vision-web-smoke
```

本次 176 项自动化测试通过，另外完成两个赛项各 10 轮真实 HTTP + 独立 TCP 模拟客户端联调。实际浏览器直连被测试环境网络策略阻止；前端另外完成无网络的演示数据渲染检查，**不能把离线渲染当成端到端浏览器验收**。

当前环境没有 Docker，未构建镜像，未验证 ARM64 容器、Oracle 实机、Windows 或真实相机。详见 [本版测试报告](docs/TEST_REPORT_V1_3_WEB.md)。基础镜像更新和依赖安全更新需要部署时复核；SQLite 运行时提示保留，不宣称任意发行版都已经包含 WAL 修复。

## 文档

- [Oracle / Docker 首次启动、HTTPS、数据与运维](docs/DOCKER_ORACLE.md)
- [Web API、鉴权与操作约束](docs/WEB_API.md)
- [v1.3 实测结果与边界](docs/TEST_REPORT_V1_3_WEB.md)
- [TCP v1 协议](docs/PROTOCOL_V1.md)
- [SQLite 迁移、备份与恢复](docs/DATABASE_GUIDE.md)
- [原桌面版投屏说明](docs/DISPLAY_GUIDE.md)
- [变更记录](CHANGELOG.md)

相机图片上传/实时视频、自动总分/排行榜、完整候场和裁判暂停流程、多赛道调度、TCP TLS 与多账户权限本版未实现。正式比赛仍建议保留现场本地部署，云端往返时间不能等同于算法耗时。
