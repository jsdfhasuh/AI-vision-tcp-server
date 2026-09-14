# v1.3.0-web 验证报告

日期：2026-09-14。基础源码：GitHub main `1d3992d6f06def52b1221a6e557d5b6dc164eeab`，Git tree `28b08e2bcccdc16cf3963b7c0b59c197325ffa33`。本报告针对新增Web/Docker源码交付，不表示远端仓库已更新。

## 实际执行

| 检查 | 结果与范围 |
|---|---|
| 原版回归 | 开发前125项通过 |
| 完整单元/API/通信套件 | 176项通过（新增51项） |
| 原生HTTP + 独立TCP进程 | Uvicorn子进程 + httpx真实HTTP + 原模拟客户端独立进程；包装箱10轮、螺钉10轮全部收到且与预期一致 |
| 数据恢复 | 备份下载成功；模拟异常退出后重启，旧未完成轮次为INTERRUPTED且无actual值；旧浏览器Cookie失效 |
| 浏览器直连 | 环境策略返回 `net::ERR_BLOCKED_BY_ADMINISTRATOR`，未能完成，不记录为通过 |
| 前端离线渲染 | 使用实际HTML/CSS/JS、确定性演示数据、完全模拟fetch，不访问网络；登录/裁判/历史/数据库/设置/大屏截图成功，无pageerror |
| 视口 | 1920×1080、1366×768、1024×768、390×844；离线渲染未发现横向溢出 |
| 静态配置 | Compose YAML可解析、生产入口导入不加载Tkinter、两套JS通过语法检查；不是 `docker compose config` 或镜像构建验证 |
| Docker / Oracle | 环境无Docker CLI/daemon，未构建镜像，未进行容器或Oracle实机验收 |

测试在Linux / Python3.13.5环境执行。Web依赖使用 `requirements-web.txt` 中固定版本。运行环境已有这些依赖；本次未完成联网创建干净容器/虚拟环境的安装验证。数据库运行时SQLite3.46.1，原版本检查会显示WAL提示；本次未据此宣称运行时已经包含所有官方修复。依赖固定不代表已经过全面漏洞审计。

## 新增测试覆盖

配置拒绝默认非本机HTTP、严格Host/Origin、登录及限流、HTTPS Cookie标记、伪造和过期会话、CSRF、正文大小、JSON错误不回显密码、静态路径不能读取数据库/凭据、公共只读字段隔离。

正式/练习持久化、单独查看接入码、请求ID重放与冲突、旧状态令牌拒绝、缺少客户端拒绝开始、目标更新重新确认、活动轮不允许结束场次、正确NG/超时不补NG、历史分页、标题持久化。

授权备份下载与恢复、导出和完整性任务、慢导出不阻塞检测结果、监听控制和重复引擎数据库锁、密码/Cookie不进入数据库、两个赛项各10轮真实TCP协议。另验证全局登录限流后不会持续分配新来源条目、关闭场次写入失败后仍清理线程并释放数据库锁。

## 可复验命令

```bash
python -m pip install -r requirements-web-test.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/web_http_smoke.py --output-dir /tmp/vision-http-check
```

可选浏览器测试依赖：安装Playwright并按其官方说明安装Chromium；不要为运行本应用把浏览器测试依赖加入生产容器。浏览器脚本参数先用 `--help` 查看。

```bash
python tests/web_browser_smoke.py --output-dir /tmp/vision-browser-check
python tests/web_frontend_render.py --output-dir /tmp/vision-render-check
```

`web_frontend_render.py` 仅验证界面布局和脚本与模拟数据交互；不能替代 `web_browser_smoke.py`。这次没有通过关闭浏览器策略、代理桥接或修改限制来宣称直连成功。原生HTTP/TCP测试是独立的后端测试，不是浏览器测试。

## 尚待部署验收

构建镜像（拉取基础镜像/apt/PyPI）、ARM64与AMD64本机运行、只读文件系统与UID10001目录权限、Compose健康检查、SSH/HTTPS、Oracle NSG/主机端口隔离、不同来源设备、大屏物理投影、Windows与真实相机/参赛软件。

另外验收磁盘将满、容器强杀、备份队列慢、管理员密码重置、升级回退、断网/恢复和长时间运行。云端网络耗时不视为模型净耗时。单实例、单活动场次、单管理员限制必须纳入实际赛事安排。

所有页面预览标注“演示队伍/练习/非正式比赛”；本交付不包含现场数据库、密码哈希、登录Cookie、接入码配置或实际比赛日志。截图是离线演示渲染，不是Oracle部署截图。
