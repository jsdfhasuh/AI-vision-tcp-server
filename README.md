# 视觉比赛 TCP 服务器 · v1.5.0-station-groups

两个项目、每项目两个工位。负责TCP接收、持久化和分页显示，不管理场次、轮次、评分或排名。

## 当前两种上报格式

```text
screw,工位号,组别号,螺钉数量,检测结果,end
packaging,工位号,组别号,完整条码,LOGO结果,火焰标识结果,总结果,end
```

可直接发送的例子（每行使用对应工位的连接）：

```text
screw,1,G1,4,OK,end
packaging,2,G2,001234-AbC,OK,NG,NG,end
```

UTF-8，英文逗号分隔，小写`end`结尾，不需要换行。工位号只能1或2；组别号按文本保存，`001`不会变成`1`。当前格式不再额外携带工号。检测结果、LOGO、火焰标识、总结果均须为大写`OK`或`NG`。

- 电机螺钉：保存组别、数量和**参赛端上报的检测结果**，不按数量重算。
- 包装箱：服务器按本工位选定标准核对完整条码；LOGO、火焰和**参赛端上报的总结果**分别原样保存。条码校验不覆盖总结果；总结果不由服务器重新合成。
- 工位号区分设备，组别号区分参赛组；组别变化不要求重连，也不决定使用哪个条码标准。

成功保存后回复`ACK,end`，格式错误回复`ERR,FORMAT,end`。ACK不是检测OK，也不是条码匹配反馈。缺字段、旧文本布局不会被猜测为新布局。详见[当前文本协议](docs/PROTOCOL_TEXT.md)。

没有读取条码时保留空字段，例如`packaging,1,G1,,OK,NG,NG,end`。条码前导零、大小写、首尾空格和Unicode原样保留；组别号和条码都不能含英文逗号或控制字符。连续相同文本分别记为新检测；没有检测ID，ACK丢失时先核对记录，不能盲目重发。

## 包装箱标准：本地JSON批量录入

```json
{
  "boxes": [
    {"id": "BOX01", "name": "1号包装箱", "standard_barcode": "001234-AbC"},
    {"id": "BOX02", "name": "2号包装箱", "standard_barcode": "005678-DeF"}
  ]
}
```

管理端选择电脑上的JSON文件，预览后确认导入；**两个包装箱工位分别选择当前标准并应用**，不是清单中任意条码命中即通过。文件最多1000条且256 KiB。导入整批替换；删除或修改当前条目的条码会取消受影响工位的选择。旧`{"standard_barcode":"..."}`兼容为共享单条标准。

标准只供服务器核对，不下发给工位，不识别印刷型号文字。不配置/不选择标准/未读条码分别显示对应状态，不假报NG。每条记录保留入库当时的标准快照，切换标准不改判历史；换箱前先等待上一件结果入库。详细格式和规则见[JSON清单指南](docs/STANDARD_BARCODE_JSON.md)，示例在`examples/packaging-standards.example.json`。

## 更新与启动

先停旧服务并另存数据目录备份，确认本地改动已处理，再执行：

```powershell
git pull --ff-only origin main
python -m pip install -r requirements-web.txt
python web_server.py
```

首次部署才执行`python tools/init_web_admin.py`，已有凭据不重建。管理页`http://localhost:9080/admin/`；只读页`http://localhost:9080/board/`。TCP默认`127.0.0.1:9000`；跨电脑连接需配置可信局域网监听地址。网页端口和TCP端口不同。非默认网页地址同时设置匹配的`--public-url`。

示例客户端默认发送当前文本格式：

```powershell
python station_client.py --project screw --station 1 --group-id G1 --count 4 --result OK
python station_client.py --project packaging --station 2 --group-id G2 --barcode "001234-AbC" --logo OK --flame NG --result NG
```

`--result`对应电机的检测结果或包装箱的总结果，必须显式填写，不能自动推断。客户端发一条后退出，不自动重试，页面之后显示断开但记录保留。长连接可先发`hello,screw,1,end`或`hello,packaging,2,end`，空闲每5秒发`ping,end`；回复分别为`HELLO,end`、`PONG,end`。

Docker挂载及管理员配置沿用[部署指南](docs/DOCKER_ORACLE.md)。更新使用`docker compose up -d --build`重建，不能只重启旧镜像。旧版场次操作不适用于默认入口，本次没有进行Docker或Oracle部署验收。

## 数据升级和兼容

工位数据使用`data/station-results.sqlite3`，旧`competition.sqlite3`不覆盖。此版本升级工位schema至3，**已有库先生成独立备份再升级**；新增可空的`group_id`、`detection_result`、`total_result`。历史工号、原始报文、ID和检测记录不改写，不拿工号冒充组别，不补历史缺失的OK/NG。新文本记录不写假工号；旧NOT NULL工号列留空串用于结构兼容。

升级后旧代码不能直接打开新工位库。回退时停服务，在新的空数据目录使用升级前备份和旧代码，不覆盖带WAL/SHM的运行目录。原标准目录、工位选择、去重回执和Windows备份句柄修复保留。

原[JSON v2](docs/PROTOCOL_V2_STATIONS.md)作为显式兼容保留，仍上传worker_id，不支持新组别/总结果字段，不能把JSON工号转换成组别。CLI使用`--json-v2 --worker-id ...`，旧Python函数`send_result()`不变；`send_text_result()`现在要求新组别与结果字段。旧文本`...工号...`且没有检测/总结果的格式不再接受，参赛端须同步更新。

管理端/只读页均显示组别和新结果列；历史无对应数据时显示`—`。管理端CSV和JSONL导出包含新字段，并保留历史工号；CSV工号/组别/条码列应按文本导入。标准、原始日志、导出和维护仍受管理员权限保护，只读页不提供标准清单或历史标准快照。

两种TCP格式均为可信隔离网络中的明文，组别/工位号不是身份认证。**不要直接把9000端口开放到公网**；跨网使用可信VPN/SSH。每个项目+工位独占连接，后来的连接不能抢占。存储故障不虚报成功，通信错误不伪造NG。

旧桌面`server.py`、`start_server.cmd`及`python web_server.py --legacy-v1`保持独立旧协议路径。新版使用默认`web_server.py`，不要与旧进程占同一端口。

## 验证

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/group_frontend_render.py --output-dir ./test-evidence/group-offline
```

本次实际执行范围见[工位、组别与结果测试记录](docs/TEST_REPORT_STATION_GROUPS.md)。离线浏览器使用合成响应，不等同于浏览器联机或现场验收。`SOURCE_MANIFEST.json`继续为v1.4.0原始发布快照，不代表后续Git增量提交。
