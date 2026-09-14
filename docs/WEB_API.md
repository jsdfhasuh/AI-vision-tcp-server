# Web API 与安全边界 · 1.3.0-web

本接口供裁判网页使用，**不是参赛视觉客户端的新协议**。视觉客户端继续使用 `PROTOCOL_V1.md` 的 TCP JSON Lines v1。API 服务与 TCP 服务共享唯一 Engine。

## 页面和读取范围

| 路径 | 授权 | 内容 |
|---|---|---|
| `/admin/` | 静态登录页可公开，数据必须登录 | 裁判后台 |
| `/board/` | 不要求裁判登录 | 公开大屏 |
| `/api/display` | 不要求裁判登录 | 现有公开白名单快照，无预期/样件/接入码/备注 |
| `/healthz` | 最小健康状态 | `{ "ok": true/false }`，不包含私有数据 |
| `/api/auth/me` | 会话 Cookie | 当前管理员及 CSRF token |
| `/api/admin/state` | 会话 Cookie | 当前场次、最多100轮、私有核对结果、state_token；接入码另读 |
| `/api/admin/access-code` | 会话 Cookie | 当前场次接入码（界面必须主动显示） |
| `/api/admin/sessions` | 会话 Cookie | 场次分页，team/mode/competition/date_from/date_to/limit/offset |
| `/api/admin/sessions/{sid}` | 会话 Cookie | 只读场次及轮次，limit1～200/offset |
| `/api/admin/jobs`、`/{jid}`、`/{jid}/download` | 会话 Cookie | 当前进程维护任务列表、进度、授权ZIP下载 |

日期按 UTC 筛选，格式 YYYY-MM-DD。旧数据保留 LEGACY 分类，不能自动当作 OFFICIAL。

## 登录

所有 POST 都要求 `Origin` 与配置 `WEB_PUBLIC_URL` 完全一致，Content-Type 为 application/json，最大正文16KiB。Host 同样严格检查。应用不允许 CORS；不根据伪造的转发头建立信任。默认安全响应头包括 no-store、nosniff、CSP、禁止被 iframe 嵌入。

```http
POST /api/auth/login
Origin: http://localhost:9080
Content-Type: application/json

{"username":"admin","password":"<本机初始化时设置的密码>"}
```

成功返回 `username`、`csrf`、`version`，并设置 `vision_referee` HttpOnly / SameSite=Strict 会话 Cookie；配置 HTTPS 时额外启用 Secure。密码使用加盐 PBKDF2-HMAC-SHA256，600000迭代；文件没有明文密码。日志不写密码或 Cookie。

会话绝对有效期8小时，最多16个，重启失效。单IP每分钟最多5次登录尝试，全局每分钟20次；错误密码与正确尝试都计入。反代环境不信任任意 X-Forwarded-For，因此可能共享代理来源限额。本版只有一个管理员身份，无角色/权限分级。

`POST /api/auth/logout` 除 Cookie/Origin/JSON 外还要求 `X-CSRF-Token`，成功立即撤销会话。Cookie/CSRF 不放进浏览器 localStorage。

## 裁判写操作

统一路径 `POST /api/admin/commands/{name}`，必须提供 Cookie、准确 Origin、JSON、`X-CSRF-Token` 和 `X-Request-ID`。请求ID为16～128个字母/数字/下划线/连字符，建议UUID。比赛控制须从最新 `/state` 读取 `state_token`。

```json
{"state_token":"<最新值>","team":"练习队伍","client_id":"vision-01","competition":"packaging","mode":"PRACTICE"}
```

上面用于 `new_session`。其他允许的命令和字段如下（未知字段直接拒绝）：

| name | JSON字段（除另说明外均须state_token） |
|---|---|
| new_session | team, client_id, competition=packaging/screw, mode=PRACTICE/OFFICIAL |
| target | box_type, product_model, label_type |
| start_round | case_name, expected=OK/NG, timeout_ms, action=arm/trigger, notes |
| cancel_round | reason |
| end_session | 无其他字段；活动轮必须先结束/取消 |
| start_listener / stop_listener | 无其他字段；活动轮期间拒绝，地址取启动配置 |
| branding | title（最多28字）, subtitle（最多56字） |
| backup / check | 空JSON；不需要state_token，仍需请求ID和CSRF |
| export | session_id；不需要state_token，仍需请求ID和CSRF |

成功的同一请求编号/相同内容在当前进程回执缓存内再次提交，会返回 `replayed=true` 而不是重复执行；相同编号改内容返回409。回执只保留最近512条、重启失效。**不是永久幂等账本**。状态令牌含进程epoch，旧页面在重启后不能用旧令牌继续操作。

state_token 会随着场次/轮次/目标/监听状态变化；不匹配返回409 `STATE_CHANGED`。前端写操作没有无条件自动重试；网络超时不代表操作一定未执行，先重新读取当前状态确认。不得刷新预期并将未知结果补 NG。

后台维护任务返回 job，状态 QUEUED/RUNNING/DONE/FAILED。导出/备份/完整性检查在独立有界队列中执行，文件写入阶段不持有 Engine.lock。最多8个未完成任务，队列满拒绝新任务。下载只允许已完成且在生成目录内的任务文件，没有任意文件路径或目录遍历接口。

任务列表最多100条，内存保存，重启后只保留磁盘文件不恢复下载链接。自动备份仅生成备份目录，手动任务额外生成ZIP。网页不提供在线恢复、删除历史、覆盖最终结果或任意SQL。

## 错误与恢复

错误格式 `{ "error": { "code": "...", "message": "..." } }`。主要状态：401未登录，403来源/CSRF不匹配，409状态或请求冲突，413正文过大，429限流/队列满，503忙或证据写入失败。输入错误不回显提交的密码。

数据库写入失败会按原引擎逻辑冻结比赛，不向参赛端假报保存成功。后台维护任务失败和核心检测结果失败独立；通过日志及任务状态核实。服务器恢复后旧未完成轮次标记INTERRUPTED，不重建旧单调时钟、不补NG。

## 公网边界

Web HTTPS必须由可靠反向代理终止，服务端只信任配置 origin，而不是客户端可伪造的 scheme 头。公开大屏没有登录墙，拿到域名的人能看到被白名单允许的比赛信息；需要限制观众时应在VPN或反代加独立访问控制，不给观众裁判账号。

TCP v1仍为明文，须通过SSH/VPN/可信局域网。本版不是公网安全认证产品，未做独立渗透测试。数据库/导出不加密，哈希校验不是数字签名；请限制文件权限和下载传播。
