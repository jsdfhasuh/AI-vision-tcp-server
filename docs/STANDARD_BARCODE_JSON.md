# 从本地 JSON 文件录入标准条码

包装箱检查页改为“选择本地 JSON → 预览 → 导入并生效”。条码框只读，不再手动填写。

这里的“本地”指打开管理网页的这台电脑。文件可以放在任意位置，由管理员主动选择。服务器不会扫描电脑目录，也不会自动监视文件变化。文件导入成功后，条码和标准版本保存在服务器的 `station-results.sqlite3` 中；关闭浏览器、删除原文件或重启服务器，都不会撤销已导入的标准。修改 JSON 后需要重新选择并导入。

## 文件格式

```json
{
  "standard_barcode": "001234-AbC"
}
```

将示例值换为标签实际完整条码。文件名可以是 `standard-barcode.json`，UTF-8 编码，可带 UTF-8 BOM，Windows CRLF 换行可用。仓库示例为 `examples/standard-barcode.example.json`；管理页也可以下载示例。

只接受一个 `standard_barcode` 字段，值必须是字符串（双引号内），最长512个字符，文件最多4 KiB。不接受数字、数组、多余字段、重复字段、注释或尾随逗号。前导零、大小写、首尾空格均原样保留，不转换为数字、不自动截取或修剪。

工号、LOGO 和火焰标识仍由工位通过 TCP 上报，不填入标准文件。两个包装箱工位继续共用一个标准；不新增多个型号、多个条码或分工位标准。

## 操作

1. 停止旧服务，拉取最新代码，再用 `python web_server.py` 启动。已有凭据、数据库保留，不需要重建。
2. 管理员登录，打开“包装箱检查”，选择本地 `.json` 文件。
3. 检查只读预览。此时显示“待导入”，原服务器标准仍生效。
4. 点击“导入并生效”，收到成功提示后使用新标准。新标准只影响之后收到的记录，不改判历史，不下发给工位。

原先在页面录入的标准会继续保留，直到新文件导入成功。页面切换和轮询不清空待导入文件；断网时禁止导入。文件错误时不替换原标准。

当其他管理员已更新标准，会返回版本冲突。点击“取消选择 / 重新载入”，再重新选择文件并确认，不能静默覆盖对方的修改。提交响应超时表示结果未知，应重新载入查看当前标准，不要反复点击。

`{"standard_barcode":""}` 明确表示清除标准；网页会再次确认。清除后，未读取条码仍记为未读取，已读取条码记为未配置标准，LOGO/火焰结果照常记录，不自动补成 NG。

建议实际标准文件放在仓库之外或已忽略的 `data/` 目录，不要将真实比赛标准提交到公开仓库。

## 接口与兼容

`POST /api/admin/standard-barcode/import` 接受 `content`（所选文件的原始 UTF-8 文本）和 `revision`（当前版本）。沿用登录、Origin、CSRF、X-Request-ID、版本冲突和持久化去重保护；服务器再次严格解析原始文件文本，包括检测重复 JSON 键。

`GET /api/admin/standard-barcode/template` 仅供已登录管理员下载固定示例，不导出当前真实标准。

旧 `POST /api/admin/standard-barcode` 保留以兼容已有工具，网页改用 JSON 导入接口。TCP v2 报文、四工位映射、工号、螺钉数量、LOGO 和火焰标识格式均未改变；参赛程序不需要为这次标准录入方式变化而修改。

## 本次验证

- `python -m unittest discover -s tests -p 'test_standard_json.py' -v`：28项通过。包含真实临时数据库和四个真实 TCP 连接；HTTP 接口使用 FastAPI TestClient，并非浏览器联机测试。
- `python tests/station_frontend_render.py --output-dir ./test-evidence/json-offline`：15项离线浏览器 DOM/文件选择/交互检查通过。使用合成 HTTP 响应，不是端到端联机验收。
- `node --check competition/monitor/app.js` 和相关 Python 编译检查通过。
- 已更新 `tests/station_browser_smoke.py` 为真实网页 JSON 导入流程。此次尝试访问本机 HTTP 时被环境策略拦截（`ERR_BLOCKED_BY_ADMINISTRATOR`），未完成，不计为通过；未绕过浏览器策略。

本次没有重跑此前的全部240项测试，没有进行 Docker 构建、Windows/相机/PLC或 Oracle 实机验收。`SOURCE_MANIFEST.json` 保留为 v1.4.0 原始发布快照，不是后续 Git 增量提交的整包校验清单。
