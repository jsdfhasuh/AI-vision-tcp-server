# 视觉比赛 TCP 协议 v1

本协议是随本服务端制定的新约定，不是原评分截图已经公布的报文格式。正式比赛前应统一下发并冻结版本。

## 1. 传输与字段规则

传输使用IPv4 TCP长连接，UTF-8编码，不带BOM。每条消息是一个JSON对象，以字节LF（`0A`，即真正的 `\n`）结束。服务端发送LF；客户端也允许使用CRLF（`0D 0A`）。不能发送字符串形式的两个字符 `\`、`n` 来代替换行。

一条报文最多16384字节，不含LF，包含可选的尾部CR。JSON内部允许合法空白；字符串中的换行必须按JSON转义，不能把一条消息美化为多行发送。空白行拒绝，顶层数组拒绝，重复JSON键、NaN/Infinity、超深嵌套、无效UTF-8拒绝。JSON最大嵌套深度24。`OK`、`NG`严格区分大小写。

TCP接收块与消息边界不相等。接收实现必须保留缓冲区，按LF取完整帧，一次可以解析多条报文，超时时保留未收全的字节。`recv()` 返回空字节才表示对端关闭。

客户端每条消息都有 `v`（整数1）、`type`、`msg_id`。整数位置不接受JSON的 `true/false`。除可选 `details` 的内部字段外，未定义的顶层字段会被拒绝。ID/接入码字段为1～64字符的非空、去首尾空白字符串，不能包含控制字符。建议所有ID使用UUID十六进制字符串。

除 `hello` 外，每条客户端消息必须携带正确的 `session_id`。服务端响应使用 `reply_to` 对应请求的 `msg_id`；主动下发目标与轮次不一定有 `reply_to`。

## 2. 正常交互

```text
参赛端                               裁判服务端
  | -------- hello --------------------> |
  | <------- hello_ok（含目标版本）------ |
  | 本地应用/检查目标配置                  |
  | -------- target_ack ---------------->|
  | <------- ack ------------------------|
  | -------- get_target（需要时）-------->|
  | <------- target ---------------------|
  |                                      | 裁判选择样件、预期，开始本轮
  | <------- round ----------------------|
  | 绑定round_id、目标版本，采图并自动检测   |
  | -------- result -------------------->|
  |                                      | 首个有效结果提交到SQLite
  | <------- result_ack -----------------|
```

裁判的样件类别、预期结果、备注和判定是否正确，不在任何服务端响应中公开。

以下示例为了阅读分块展示；每个对象实际都应单独编码为一行，并添加一个LF。`S1`、`R1`、`H1`等均为说明性占位ID，真实场次和轮次ID由服务端生成。

## 3. 握手 hello

客户端：

```json
{"v":1,"type":"hello","msg_id":"H1","client_id":"vision-01","access_code":"ABCD1234"}
```

服务端：

```json
{"v":1,"type":"hello_ok","reply_to":"H1","session_id":"S1","competition":"packaging","target_revision":1,"target":{"box_type":"BOX_A","product_model":"","label_type":""},"heartbeat_interval_s":5,"idle_timeout_s":30,"max_frame_bytes":16384}
```

`competition` 是 `packaging` 或 `screw`。螺钉赛项目标为 `{}`，仍使用目标版本与确认步骤，以保持两个赛项的接入流程一致。

连接建立后5秒内完成有效握手。新场次的接入码需从裁判端获取。客户端ID必须匹配，接入码是区分大小写的。服务端不从 `client_id` 自动创建/切换队伍。

一条连接只允许一次成功握手。同一场次同时只允许一个已认证客户端。`BUSY` 表示已有客户端占用，不允许后来的连接强行抢占。

## 4. 读取与确认目标

主动读取：

```json
{"v":1,"type":"get_target","msg_id":"T1","session_id":"S1"}
```

响应或配置更新时的主动通知：

```json
{"v":1,"type":"target","reply_to":"T1","session_id":"S1","competition":"packaging","target_revision":2,"target":{"box_type":"BOX_B","product_model":"MODEL_B","label_type":"LABEL_B"}}
```

主动通知不带 `reply_to`。`box_type`最长128字符；另外两项可以为空，非空时最多128字符。不要求字符串只能为ASCII，可以用事先统一的中文值。

客户端必须先应用/验证目标，再确认版本。不能在配置尚未生效时提前确认：

```json
{"v":1,"type":"target_ack","msg_id":"T2","session_id":"S1","target_revision":2}
```

服务端：

```json
{"v":1,"type":"ack","reply_to":"T2","session_id":"S1","for":"target_ack"}
```

目标更新后，原有确认失效。正在等待结果的轮次不允许修改目标；已超时轮次可以迟到上报，它按自己的目标快照核验，不拿新目标版本覆盖旧版本。

## 5. 开始轮次 round

服务端主动下发：

```json
{"v":1,"type":"round","session_id":"S1","round_id":"R1","action":"arm","target_revision":2,"target":{"box_type":"BOX_B","product_model":"MODEL_B","label_type":"LABEL_B"},"timeout_ms":0,"started_utc":"2026-09-05T01:00:00.000+00:00","timer_restarted":false}
```

`round_id` 在本服务端生成，全局使用随机UUID，不会因显示轮次从1开始而复用旧编号。

| 字段 | 语义 |
|---|---|
| `action=arm` | 建立本轮上下文，但不发出立即采图指令；参赛端等待约定的外部触发 |
| `action=trigger` | 本轮消息同时请求参赛端启动一次新检测 |
| `target_revision` | 本轮不可变的目标版本，结果必须原样携带 |
| `target` | 本轮指定目标快照，不是该样件的正确识别答案 |
| `timeout_ms` | 0表示服务端不自动超时；非0为本轮时限 |
| `started_utc` | 服务端开始创建本轮的UTC时间；主要用于记录，不要求客户端时钟同步 |
| `timer_restarted` | 始终false；查询/重发轮次上下文不会重置服务端原计时 |

首次接收的同一 `round_id` 只应绑定一个检测任务。重复通知或重连后查询到相同轮次时，不得不加判断地再次触发相机。

真实检测程序应在任务入口保存 `(session_id, round_id, target_revision, target)` 快照，后续回调使用该快照。禁止在旧图像任务结束时读取一个已变化的“当前round_id”来发送结果。

## 6. 提交最终结果 result

```json
{"v":1,"type":"result","msg_id":"M1","session_id":"S1","round_id":"R1","target_revision":2,"verdict":"NG"}
```

可选诊断信息示例：

```json
{"v":1,"type":"result","msg_id":"M1","session_id":"S1","round_id":"R1","target_revision":2,"verdict":"NG","details":{"front":{"verdict":"OK"},"side":{"verdict":"NG","reason":"logo_stain"},"capture_id":"FRAME_TASK_01"}}
```

`details` 必须为JSON对象，总报文大小限制不变。这里的字段仅是示例，不是额外强制评分要求。若启用重传，整个 `details` 也必须保持不变，不要每次重传重新填“当前时间”等动态字段。

包装箱赛项必须由参赛端先得出双相机最终结果：任一不合格则最终NG。服务端不会用 `details.front` / `details.side` 替客户端重算答案。螺钉赛项同样只接收参赛端的最终完整性判定。

服务端先持久化，再发送：

```json
{"v":1,"type":"result_ack","reply_to":"M1","session_id":"S1","round_id":"R1","recorded":true,"duplicate":false}
```

**`recorded:true` 只表示服务端已保存这条最终结果。它不表示图像检测正确，不表示未超时，更不表示本轮得满分。**即使视觉漏检，或结果迟到，也可能收到该ACK；相应正确性/迟到状态仅保存在裁判端。

同一轮只有第一个有效最终结果可以被记录。格式错误、未知场次/轮次、目标版本不符不会消耗正常结果名额；但会保留错误证据与次数，时限也不会重置。

如果视觉算法异常，不要为了确保“有报文”而伪造NG。保留客户端错误日志，让服务端记录未收到有效结果/超时。后续需要扩展故障报文时应升版或事先统一，不擅自在v1里加入未定义顶层类型。

## 7. ACK丢失、重复与冲突

服务端只对**已接受的最终结果**保留幂等回执。建议客户端保存发送对象，ACK未确认时原样重发同一对象：`session_id`、`round_id`、`target_revision`、`msg_id`、`verdict`及`details`均不改变。

对同一 `msg_id`，键的书写顺序或合法JSON空白变化不影响幂等判断；改变字段、数据类型或值会导致冲突。为避免数值表示差异，最稳妥做法是重发完全相同的序列化内容。

| 情况 | 处理 |
|---|---|
| 相同msg_id、相同内容再次到达 | 返回 `result_ack`，`duplicate:true`；不新增结果；重传次数加1 |
| 相同msg_id、内容变化 | `MSG_ID_CONFLICT`；不覆盖原结果 |
| 同round_id使用新msg_id再次提交 | `DUPLICATE_RESULT`；重复次数加1；不覆盖原结果 |
| ACK未收到且程序准备重试 | 重传原报文，不重新检测，不创建新msg_id |
| 超时后首个有效结果到达 | 保存到原round_id，标记LATE，正常回持久化ACK |
| 已取消/中断的轮次首个结果到达 | `ROUND_CLOSED`，不转成正常完成 |

重传次数不自动当作完全正常，也不自动当作扣分。是否允许ACK重试以及它与“无重复/错发”评分项的关系，应在比赛前统一口径。

## 8. 心跳、重连和状态查询

客户端至少每5秒发一次 `ping`，其他有效业务消息也会刷新服务端的有效活动时间：

```json
{"v":1,"type":"ping","msg_id":"P1","session_id":"S1"}
```

```json
{"v":1,"type":"pong","reply_to":"P1","session_id":"S1"}
```

30秒没有有效消息会断开。慢速发送但始终不结束一帧，不算有效心跳。一次不完整帧超过5秒也会关闭连接。每个连接每秒最多100帧，累计5次协议错误后关闭；最多保留8条底层TCP连接，只有其中一条能成功认证为当前参赛端。

客户端恢复TCP后应重新 `hello`、重新应用/确认目标，再查询：

```json
{"v":1,"type":"get_state","msg_id":"Q1","session_id":"S1"}
```

```json
{"v":1,"type":"state","reply_to":"Q1","session_id":"S1","active_round":{"v":1,"type":"round","session_id":"S1","round_id":"R1","action":"arm","target_revision":2,"target":{"box_type":"BOX_B","product_model":"MODEL_B","label_type":"LABEL_B"},"timeout_ms":3000,"started_utc":"2026-09-05T01:00:00.000+00:00","timer_restarted":false}}
```

没有仍在等待的轮次时 `active_round` 为 `null`。已经超时的轮次不算活动轮次，但带正确原始编号的迟到结果仍可被存回原轮。

连接恢复不会删除断线证据，也不会给同轮重新计时。如果客户端缓存了待确认结果，应先按原消息ID重发，而不是根据 `get_state` 重新拍照。若无法确定之前是否采集过、是否已发送，应让裁判核对记录，不能自行把它解释为一次全新检测。

服务端进程重启后，旧场次转为历史，必须新建场次；客户端应丢弃旧场次的活动任务绑定，不得将其结果转发为新场次结果。需要跨客户端进程崩溃保留发送队列的参赛软件，应自行实现持久化发件箱；本包模拟器仅保存内存待确认消息。

## 9. 裁判取消

```json
{"v":1,"type":"round_cancelled","session_id":"S1","round_id":"R1"}
```

取消原因保存在裁判审计记录中，不下发私密备注。客户端应取消该轮未开始的任务、停止将该轮结果作为正常结果提交。已经在执行的采图/推理是否可以安全停止，由客户端自己处理；不要通过杀死驱动线程破坏相机状态。

## 10. 错误响应

```json
{"v":1,"type":"error","reply_to":"M1","code":"TARGET_MISMATCH","message":"result target_revision does not match its round"}
```

无法解析消息时可能没有 `reply_to`。

| 错误码 | 含义/建议 |
|---|---|
| `NO_SESSION` | 裁判端还未新建场次 |
| `AUTH_FAILED` | 客户端ID或接入码不正确 |
| `BUSY` | 当前已有参赛客户端 |
| `NOT_AUTHENTICATED` | 必须先正确握手 |
| `ALREADY_AUTHENTICATED` | 同连接重复hello |
| `SESSION_MISMATCH` | 旧场次或错误场次消息 |
| `UNKNOWN_ROUND` | 当前场次没有该测试编号 |
| `TARGET_MISMATCH` | 配置确认版本或结果目标版本不匹配 |
| `ROUND_CLOSED` | 轮次已取消或中断 |
| `DUPLICATE_RESULT` | 同轮用新消息ID再次交最终结果 |
| `MSG_ID_CONFLICT` | 已接受消息ID被用于不同内容 |
| `BAD_VERSION` | 版本不是整数1 |
| `INVALID_FIELDS` | 缺少必填或多了未知顶层字段 |
| `INVALID_FIELD` | 字段类型、长度或格式不符合要求 |
| `INVALID_VERDICT` | 结果不是严格的大写OK或NG |
| `INVALID_JSON` / `INVALID_ENCODING` | JSON/UTF-8/BOM/非有限数值问题 |
| `DUPLICATE_KEY` / `JSON_TOO_DEEP` | JSON重复键或嵌套过深 |
| `EMPTY_FRAME` / `FRAME_TOO_LARGE` | 空帧或报文过长 |
| `UNKNOWN_TYPE` / `INVALID_MESSAGE` | 消息类型或顶层结构无效 |
| `RATE_LIMIT` | 超过每秒100帧 |

数据库写入失败时，服务端冻结测试并断开客户端，不伪造一个“已保存”的ACK。客户端必须把没有ACK视为“是否送达待核实”，而不是直接认为服务端没有收到。

## 11. 参赛端实现检查

采图任务与轮次快照绑定；先应用目标后确认；双相机最终汇总在客户端完成；长耗时视觉任务不阻塞TCP心跳；收发按照LF边界处理；结果发送成功不等于ACK已收到；重试保留消息ID及内容；取消、断线、新场次切换后不泄漏旧任务结果；相机/推理异常不伪造NG。

客户端可在 `details` 带上自己的 `capture_id`、图像时间和诊断信息，但裁判端不会把客户端自报时间当作权威超时计时依据。

## v1.2.0-db 补充（不改变客户端消息格式）

数据库升级为schema 2，但TCP仍为v1。正式/练习用途是裁判创建场次的元数据，不要求客户端额外发送字段。旧客户端的hello、target_ack、result和result_ack等格式保持不变。

数据持久化、历史筛选、备份恢复和公共展板的mode/action字段详见 `DATABASE_GUIDE.md`。公共HTTP快照的mode/action不等同于允许客户端在严格校验的TCP消息中随意添加同名字段。
