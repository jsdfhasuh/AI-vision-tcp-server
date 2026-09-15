# 视觉比赛 TCP 服务器 · v1.4.0-stations

面向 **两个项目、每项目两个工位** 的 TCP 数据接收与 Web 展示程序。服务器不负责排赛、建立测试轮次、自动评分或排名。

| 项目页 | 参赛端上传的业务数据 | 服务端处理 |
|---|---|---|
| 电机螺钉（1、2号工位） | 当前选手工号、螺钉数量 | 记录数量；不自行推断缺钉位置或判定 NG |
| 包装箱检查（1、2号工位） | 当前选手工号、完整条码、LOGO OK/NG、火焰标识 OK/NG | 完整条码与预录标准精确比较；另两项原样记录 |

**工号只是操作者标识，不是登录认证，也不用于创建比赛场次。** 型号判断只靠标签上的完整条码比对，不识别印刷型号文字。LOGO、火焰标识均不细分缺陷原因。三个包装箱结果独立保存，无自动综合判定。

## 启动与更新

默认入口现在启动新四工位接收器：

```powershell
python -m pip install -r requirements-web.txt
# 首次部署才执行；已经有 secrets/admin.json 时不要覆盖。
python tools/init_web_admin.py
python web_server.py
```

管理端：`http://localhost:9080/admin/`；只读展板：`http://localhost:9080/board/`。在顶部页签切换电机螺钉与包装箱检查。TCP 默认监听 `127.0.0.1:9000`，按报文中的项目和工位区分四路，切页面不会断开另一项目的 TCP 连接。

在包装箱管理页保存完整标准条码。**两个包装箱工位共用当前标准，标准不会下发给参赛端。** 空标准表示未配置。每条包装箱记录保存当时的标准快照；修改标准不会重新判定历史记录。

已有数据、凭据和配置目录请原样保留。更新前先停旧服务、备份数据，再拉取代码并启动，避免同一端口被重复监听。非默认端口时需同时设置 `--port` 与匹配的 `--public-url`。

Docker 沿用原有目录挂载和凭据初始化方式；镜像标记更新为 `1.4.0-stations`：

```bash
docker compose up -d --build
docker compose logs --tail=100 app
```

首次 Docker/Oracle 安装的账户、挂载权限、HTTPS 与 SSH 隧道配置见 [部署指南](docs/DOCKER_ORACLE.md)；其中 v1.3 的单场次操作说明不适用于新默认入口。此次没有实际构建镜像或部署 Oracle/ARM64。

## TCP v2 上报示例

每个 JSON 对象一行，以真正的 LF 换行结尾；不是把字符串 `\n` 放在报文末尾。无需创建场次、获取接入码、确认目标或等待“开始本轮”。第一次完整有效上报即可绑定工位，也可以先发送 `hello`。

电机螺钉：

```json
{"v":2,"type":"result","msg_id":"unique-detection-001","project":"screw","station":1,"worker_id":"D70516","screw_count":4}
```

包装箱：

```json
{"v":2,"type":"result","msg_id":"unique-detection-002","project":"packaging","station":1,"worker_id":"D70516","barcode":"001234-AbC","logo":"OK","flame":"NG"}
```

完整字段、连续连接、心跳、重复上报和重连规则见 [TCP v2 协议](docs/PROTOCOL_V2_STATIONS.md)。ACK 仅表示数据已保存，不返回标准条码或条码核对结论。

可用新示例客户端联调：

```powershell
python station_client.py --project screw --station 1 --worker-id D70516 --count 4
python station_client.py --project packaging --station 1 --worker-id D70516 --barcode "001234-AbC" --logo OK --flame NG
```

两条命令各发送一条记录后退出；连续上报程序应维持长连接并发送心跳。**旧 `client_example.py` / `vision_client.py` 是 v1 场次协议客户端，不能直接连接新版。**

## 数据与安全边界

新数据保存在 `data/station-results.sqlite3`，旧 `competition.sqlite3` 不覆盖、不自动转换。新旧数据库的业务含义不同，不能把旧 OK/NG 历史伪装成新的螺钉数量、条码或火焰检查记录。

管理页保留最新 100 条记录和 50 条通信日志，可按工位筛选。CSV/JSONL 导出读取全部所选项目/工位记录，不只是屏幕上的 100 条；“仅不匹配/NG”只影响页面筛选。JSONL 保留精确文本，CSV 在电子表格软件中应将工号和条码列按文本导入以保留前导零。导出内容和备份包含标准条码快照，必须保管好。

新备份位于 `data/station-backups/`，通过管理页备份按钮生成独立 SQLite 文件；正常停机默认也备份（`AUTO_BACKUP=0` 可关闭）。旧 `database_tools.py` 只管理旧数据库，不要用它恢复新库。恢复新库时先停服务，在**新的空数据目录**中把独立备份复制为 `station-results.sqlite3`，使用原凭据启动验证；不要直接覆盖一个仍有 WAL/SHM 的运行目录。

浏览器操作沿用管理员登录、Cookie、Origin/Host 和 CSRF 校验。标准条码、原始通信日志、来源地址只在登录后的接口提供；只读展板展示上报数据，不提供这些配置或维护操作。

TCP v2 是可信隔离网络内的明文协议，没有客户端密码认证；工位号和工号不能证明发送者身份。**不要将 9000 端口直接开放到公网**，跨网使用可信 VPN/SSH 隧道，现场限制接入设备。四个工位各允许一个活动连接，后来的连接不能抢占已有工位。通信异常不补成 NG；存储故障停止接收，不虚报已保存。

## 兼容与代码位置

- 新业务：`competition/station_protocol.py`、`station_store.py`、`station_runtime.py`、`station_webapp.py`。
- 新界面：`competition/monitor/`；并非先前只修改页面的 UI 骨架包。
- 旧核心和界面保留为兼容路径；`python web_server.py --legacy-v1` 明确启动旧协议。它不是四工位接收器，不能与新入口占用同一端口。
- `server.py` / `start_server.cmd` 仍为旧桌面入口。新版请启动 `web_server.py`。
- 继承 `8db21f7` 的 Windows 备份 `fsync` 修复，没有覆盖掉它。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/station_http_smoke.py --output-dir ./test-evidence/http
# 以下两项需可选 Playwright 与 Chromium，不是运行服务的必需依赖：
python tests/station_frontend_render.py --output-dir ./test-evidence/offline
python tests/station_browser_smoke.py --output-dir ./test-evidence/browser
```

本次：240 项自动化测试通过；真实 HTTP + 四个 TCP 连接完成 40 次检测和 4 次重复确认；离线浏览器 DOM 检查通过。**浏览器直接访问本地 HTTP 被测试环境策略拦截，端到端浏览器验收尚未完成**；没有把离线示例渲染当成现场实测。详见 [v1.4 测试报告](docs/TEST_REPORT_V1_4_STATIONS.md)。
