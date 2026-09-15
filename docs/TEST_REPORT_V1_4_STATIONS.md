# v1.4.0-stations 实施与测试记录

基于远程提交 `8db21f72534398f4cd9a5a65fddedeeefe9ef916`。本地基础目录经 Git 换行规范化后的完整树为 `ffcebae0412ca62a2b1aa9ae82021326b12968a3`，与该提交一致，包含 Windows 备份句柄修复。

## 实现范围

四个独立工位共用TCP监听；电机螺钉仅记录数量，包装箱精确比对条码并分别记录LOGO/火焰OK或NG。工号仅作为上报元数据，不建场次、轮次或注册账户。分页Web显示真实新记录，配置标准条码只在服务端生效，不下发。旧业务、旧库与旧入口保留为显式兼容模式。

## 本次实际验证

环境：Linux、Python 3.13、FastAPI 0.128.2、Starlette 0.50.0、Uvicorn 0.48.0、Pydantic 2.13.4，SQLite运行时3.46.1。运行时版本不代表生产环境安全验收；部署时仍须检查发行版SQLite安全修复与依赖更新。

- `python -m unittest discover -s tests -p 'test_*.py' -v`：**240项通过**（原176项 + 新64项）。
- `python tests/station_http_smoke.py --output-dir ...`：实际启动默认 `web_server.py` 子进程，真实HTTP登录/配置/读取/导出/备份，四个独立TCP连接各上报10次，共**40条新检测 + 4次幂等重试**，通过。
- `node --check competition/monitor/app.js`：通过。
- `python tests/station_frontend_render.py --output-dir ...`：**9项离线DOM/交互检查通过**。全部使用明确的合成数据，禁用浏览器网络，不是联机验收。
- `python tests/station_browser_smoke.py --output-dir ...`：尝试真实浏览器联机，首次访问本地HTTP即收到 `net::ERR_BLOCKED_BY_ADMINISTRATOR`；**未完成，不计为通过**。没有改动浏览器安全策略，也没有把合成fetch替代为已完成联机测试。

## 新测试覆盖

严格整数与工号文本、前导零和大小写、空条码、必填LOGO/火焰字段、未知字段与旧版协议拒绝；四工位并发、独占连接、禁止串工位、拆包粘包、CRLF、持久化后ACK、重复报文/冲突、重连与重启去重；工号更新不建立场次；数据库写入失败不假报成功；标准并发修改冲突、标准历史快照、公共接口不泄漏标准；CSV公式防护、原始JSONL文本保真、备份失败保护源数据。

离线界面验证了螺钉数量展示、包装箱三项独立列、轮询不覆盖未保存输入、旧版本配置冲突、条码HTML作为文本显示、页签切换不串项目、工位筛选、断网旧数据显示与禁用写操作、无JavaScript运行异常。

## 未验证的现场条件

尚未完成浏览器直连服务端端到端验收、真实相机/PLC与参赛软件联调、Windows新入口实机、Docker镜像构建、Oracle/ARM64部署与长时间高负载稳定性。没有改动现场服务、生产数据或凭据。

上线前先用 `station_client.py` 完成两项目四工位联调，再按协议接入视觉程序；尤其确认三个包装箱字段及新 `msg_id` 生成/重试策略。旧v1模拟器不能连接新版默认接收器。
