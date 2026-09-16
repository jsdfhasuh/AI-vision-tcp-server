# 视觉比赛 TCP 服务器 · v1.4.1-catalog

两个项目、每项目两个工位。提供TCP接收、数据保存和两个项目分页展示，不管理比赛场次、轮次、评分或排名。

| 项目 | 参赛端上传 | 服务器处理 |
|---|---|---|
| 电机螺钉1/2工位 | 当前选手工号、螺钉数量 | 记录数量，不推断缺钉位置或判NG |
| 包装箱1/2工位 | 当前选手工号、完整条码、LOGO OK/NG、火焰标识 OK/NG | 按本工位当前标准精确比对条码；另两项独立记录 |

工号只是操作者信息，不是身份认证，也不决定校验标准。型号只靠完整条码比对，不另做印刷型号文字识别；LOGO/火焰不细分缺陷原因，不输出综合得分。

## 新增：多包装箱标准清单

一个本地JSON文件可批量导入最多1000条标准（同时受256 KiB文件限制）。管理端预览、确认导入后，**两个包装箱工位分别选择当前标准**。不是“上传条码在清单里就通过”。

```json
{
  "boxes": [
    {"id": "BOX01", "name": "1号包装箱", "standard_barcode": "001234-AbC"},
    {"id": "BOX02", "name": "2号包装箱", "standard_barcode": "005678-DeF"}
  ]
}
```

完整说明、兼容规则和接口见 [JSON批量录入指南](docs/STANDARD_BARCODE_JSON.md)，示例在`examples/packaging-standards.example.json`。原来的`{"standard_barcode":"..."}`仍兼容，但会替换为共享单条标准并应用到两个工位。

批量导入是整批替换。保留条目编号和原条码可保留已有选择；删除或更改条码会取消受影响工位的选择。未选择不会自动判不匹配。历史记录保存当时的标准快照，修改不重判。标准只在服务器使用，不下发给选手。

## 启动与更新

更新前先停止旧服务、备份数据目录；不要删除已有凭据和数据库。

```powershell
git pull --ff-only origin main
python -m pip install -r requirements-web.txt
# 首次部署才执行；已有 secrets/admin.json 时不要覆盖：
# python tools/init_web_admin.py
python web_server.py
```

管理端`http://localhost:9080/admin/`；只读展板`http://localhost:9080/board/`。TCP默认`127.0.0.1:9000`；项目和工位由报文区分，切页面不影响其余工位接收。自定义端口时配置匹配的`--public-url`。

Docker挂载与管理员初始化沿用 [部署指南](docs/DOCKER_ORACLE.md)，升级时使用`docker compose up -d --build`重建本地镜像，不要只重启旧镜像。旧文档中的v1.3场次操作不适用于新默认入口。本次不包含Oracle/ARM64或真实相机、PLC部署验收。

## TCP v2

每条报文一个UTF-8 JSON对象，以真正的LF换行结束。可先hello或直接上报；不需要创建场次、接入码或轮次。

```json
{"v":2,"type":"result","msg_id":"unique-001","project":"screw","station":1,"worker_id":"D70516","screw_count":4}
{"v":2,"type":"result","msg_id":"unique-002","project":"packaging","station":1,"worker_id":"D70516","barcode":"001234-AbC","logo":"OK","flame":"NG"}
```

详细约束见 [协议v2](docs/PROTOCOL_V2_STATIONS.md)。ACK只表示持久化，不反馈标准或条码是否匹配；同ID原样重试不重复入库、不重判。连续工作程序保持TCP长连接并发送心跳。

```powershell
python station_client.py --project screw --station 1 --worker-id D70516 --count 4
python station_client.py --project packaging --station 1 --worker-id D70516 --barcode "001234-AbC" --logo OK --flame NG
```

示例客户端一次上报后退出。旧`client_example.py`、`vision_client.py`是v1协议，不能连接新版默认接收器。此次新增批量JSON与分工位选择**不改变TCP v2报文**。

## 数据、兼容与安全

新数据在`data/station-results.sqlite3`；旧`competition.sqlite3`不覆盖不转换。此次工位库升级schema v1→v2，先在`data/station-backups/`生成独立备份，然后事务升级；原标准继续有效、旧结果不变。回退旧代码需使用升级前备份和新空数据目录，不能对新库强制降级。

管理端显示最近100条记录、50条通信日志，可筛工位和包装箱不匹配/NG。导出读取全部所选项目/工位记录，包含历史标准编号、名称、条码和版本；JSONL保留精确文本。CSV在电子表格中应按文本导入工号、条码列以保留前导零。备份及导出包含私有标准，注意保管。

标准、日志和维护操作要求管理员登录；只读展板不返回标准清单、标准快照或原始日志。原管理员Cookie、Host、Origin、CSRF保护保留。

TCP v2是可信隔离网络内的明文协议，没有客户端密码认证；工位号和工号不能证明身份。**不要把9000端口直接开放到公网**；跨网使用可信VPN/SSH隧道。四工位各独占一个连接，不允许后来连接抢占。通信异常不补NG，存储失败不虚报已保存。

旧桌面`server.py`/`start_server.cmd`和显式`python web_server.py --legacy-v1`保留为旧协议路径；不能与新服务占用同一端口。Windows备份fsync修复仍保留。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/catalog_http_smoke.py --output-dir ./test-evidence/catalog-http
# 以下依赖可选的Playwright和Chromium：
python tests/station_frontend_render.py --output-dir ./test-evidence/catalog-offline
python tests/station_browser_smoke.py --output-dir ./test-evidence/catalog-browser
```

本次实际执行范围及未验证条件见 [批量标准测试报告](docs/TEST_REPORT_V1_4_CATALOG.md)。离线浏览器合成数据渲染不等同于联机验收。`SOURCE_MANIFEST.json`是v1.4.0原始发布快照，不代表后续Git增量提交。
