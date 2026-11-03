# API 参考

[返回首页](../README.md) · [使用指南](usage.md) · [配置与数据](reference.md) · [开发](development.md) · [常见问题](troubleshooting.md)

## 请求约定

默认地址为 `http://127.0.0.1:8787`。请求体使用 JSON 对象，最大 1 MiB；普通 API 响应为 JSON，`GET /api/theme/events` 返回 SSE。服务允许 `GET`、`POST`、`OPTIONS` 跨域请求，未配置独立 API 身份认证，使用范围见[安全边界](reference.md#数据目录与安全边界)。

需要确认的接口必须传入 JSON 布尔值 `"confirm":true`，字符串 `"true"` 不等价。各 POST 的确认要求见下表。除 HTTP 状态外，还应检查 `ok`、嵌套结果和 `error`，部分上游失败仍以 HTTP 200 返回。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8787/api/health'
Invoke-RestMethod 'http://127.0.0.1:8787/api/quota?force=1'
```

## 状态与配置

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/health` | — | 版本、功能开关、监控状态、额度地址 |
| GET | `/api/status` | — | 当前凭据状态、账号池统计、自动切换状态 |
| GET | `/api/config` | — | 路径、端口、功能开关、CLI 路径 |
| GET | `/api/settings` | — | 设置分组、字段、值、来源、掩码密钥、重启标记 |
| POST | `/api/settings` | `{"confirm":true,"values":{"enable_login":true},"clear":["login_driver"]}` | 需确认；返回 `changed`、`cleared`、`restart_required` |
| POST | `/api/switching/toggle` | `{"key":"enable_auto_switch","value":true,"confirm":true}` | 需确认；仅设置自动切换，手动切换没有总开关 |
| GET | `/api/theme` | — | 当前主题、更新时间、支持主题 |
| POST | `/api/theme` | `{"theme":"dark"}` | 无需确认；支持 `light`、`dark`、`auto` |
| GET | `/api/theme/events` | — | SSE；先发送当前主题，后续推送更新 |

`clear` 删除持久设置，使其回退到环境变量或默认值。直接调用设置 API 时，保留原密钥应省略对应字段，空字符串并不表示“不修改”。

## 首次引导与安装版

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/setup/status` | — | 是否显示引导、Codex 检测、账号授权数、浏览器组件及自启状态；不会触发下载 |
| POST | `/api/setup/codex` | `{"confirm":true}` | 打开本机文件选择器；可传 `path` 指定有效 `codex.exe`，取消不清除旧路径 |
| POST | `/api/setup/browser` | `{"confirm":true}` | 开始/重试专用浏览器下载，复用进行中任务与已安装版本 |
| POST | `/api/setup/finish` | `{"confirm":true,"action":"later"}` | `complete` 要求已检测到 Codex 且至少一个账号完成授权；`later` 保存稍后设置 |
| GET | `/api/app/startup` | — | 当前用户 Windows 自启注册状态 |
| POST | `/api/app/startup` | `{"confirm":true,"enabled":true}` | 立即更新当前用户自启项；`enabled` 必须为布尔值，默认关闭 |

新写接口只接受本机页面 Origin 或没有 Origin 的本机客户端。维护命令 `--exit` 使用 `/api/app/exit` 请求当前安装版自身退出，除 `confirm=true` 外还需匹配当前实例的私有令牌；普通网页不能直接调用。安装器直接通知正在运行的原生托盘退出，无需执行可能已损坏的旧文件。健康检查含 `desktop_launcher` 与 `service_pid`，用于识别原生启动器和已有服务。

## 账号与凭据快照

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/accounts` | — | 账号公开信息、登录状态、登录并发数 |
| GET | `/api/snapshots` | — | 当前凭据公共信息、快照列表、切换状态 |
| POST | `/api/accounts/import` | `{"text":"account@example.com\|your-password"}` | 无需确认；批量新增或更新，返回逐行错误与排队结果 |
| POST | `/api/accounts/add` | `{"email":"account@example.com","password":"your-password","priority":100}` | 无需确认；可附带 `totp`、`tags`、`note`、`disabled` |
| POST | `/api/accounts/update` | `{"email":"account@example.com","note":"备用账号","disabled":false}` | 无需确认；更新已有账号的凭据或元数据 |
| POST | `/api/accounts/delete` | `{"email":"account@example.com","confirm":true}` | 需确认；删除账号、快照、采样，保留当前 `auth.json` |
| POST | `/api/accounts/reorder` | `{"emails":["first@example.com","second@example.com"]}` | 无需确认；依次赋值 `priority` 为 100、99…，最低 1 |

账号查询不返回密码或 TOTP 密钥。`add` 中密码可省略，默认为空；提供密码且启用自动登录时，会尝试排队登录。`update` 中空密码和空 `totp` 保留原凭据，删除 TOTP 使用 `"clear_totp":true`；空 `tags`、`note` 可清空对应元数据。`totp` 接受 base32 或 `otpauth://`，备注最多保留 200 字。

`reorder` 要求邮箱列表非空、无重复且均已存在。自动选择账号时仍优先比较剩余额度。

## 登录

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/login/status` | — | 登录开关、驱动、并发数、会话公开信息 |
| GET | `/api/login/session/<id>` | `<id>` 为返回的会话 ID | 单个登录会话，不存在时 404 |
| POST | `/api/accounts/login` | `{"email":"account@example.com"}` | 无需确认；加入受并发数限制的登录队列 |
| POST | `/api/login/start` | `{"email":"account@example.com"}` | 无需确认；直接创建或复用登录会话 |
| POST | `/api/login/cancel` | `{"session_id":"session-id"}` | 无需确认；取消会话，不存在时 404 |

导入响应的 `login_tasks[]` 返回与手动登录相同的任务信息；`session.id` 在排队阶段就存在。重复请求会复用排队或登录中的任务，查看 `/api/login/session/<id>` 不会启动登录。公开历史记录包括 `source`、`phase`、`attempt` 和 `attempt_errors`；设备码生成失败也会落盘。新导入账号首次认证串行执行，已有凭据的登录按并发设置调度。

开始登录需要启用登录助手、账号存在且已保存密码，并能找到 Codex CLI。默认禁用时返回 `423 login_disabled`。登录在隔离目录生成凭据，成功后保存加密快照，不自动切换当前账号。

## 额度与自动切换

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/quota` | `?force=1`、`?email=account%40example.com` | 默认查当前账号；`force=1` 跳过缓存，`email` 指定账号 |
| GET | `/api/accounts/quotas` | 可选 `?force=1` | 并行读取账号池额度，默认缓存 60 秒，无快照账号列入 `skipped` |
| GET | `/api/quota/history` | `?email=account%40example.com&hours=6` | 采样与预测；未指定邮箱时使用当前账号，小时数默认 24，限制 1–168 |
| GET | `/api/auto-switch/status` | — | 策略、最近结果、冷却、熔断、频率计数、`auto_reset.gate` |
| POST | `/api/quota/reset` | `{"confirm":true}` | 需确认；手动提交用券，由上游判断实际结果 |
| POST | `/api/switch` | `{"email":"account@example.com","confirm":true,"restart_desktop":true}` | 始终可用，需确认；目标需有可解析且身份匹配的快照 |

`/api/quota/reset` 可选 `email`、`credit_id`；不传时使用当前账号并选择最早到期的可用券。成功后尝试刷新额度，响应含 `result` 和 `quota`。手动消费不要求启用自动用券，也不应用其自动策略阈值。

上游查询路径默认 `/backend-api/wham/rate-limit-reset-credits`，消费路径追加 `/consume`。消费使用 `credit_id` 和程序生成的幂等 UUID `redeem_request_id`；本地接口不接受自定义幂等 ID。上游错误可能包括 `nothing_to_reset`、`no_credit`、`already_redeemed`。

`/api/switch` 省略 `restart_desktop` 时使用服务配置，明确布尔值可覆盖本次行为。凭据切换成功但重启失败时，响应仍可能成功，应检查 `switch.desktop_restart`。`switch.auto_resume` 返回切号后的续跑结果：`job` 表示已提交任务，`skipped` 说明未启动原因，`ok:false` 表示续跑失败；这些结果不改变已完成的账号切换。

## 目标与续跑

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/goals` | 可选 `?subagents=1` | 目标、会话、任务、历史与自动续跑状态；内部会话默认隐藏，置顶或已有目标的会话保留 |
| POST | `/api/goals/resume` | `{"thread_ids":["thread-id"],"dry_run":true}` | 默认预览，无需确认；无需额外启用续跑功能 |
| POST | `/api/goals/auto-resume` | `{"thread_id":"thread-id","enabled":true}` | 无需 `confirm`；保存或移除切换后自动续跑目标 |
| POST | `/api/goals/cancel-queued` | `{"confirm":true,"thread_ids":["thread-id"]}` | 撤回 Switcher 创建的待执行消息，返回逐会话结果；不会停止正在执行的回合 |
| POST | `/api/goals/pause` | `{"confirm":true,"thread_ids":["thread-id"]}` | 暂停后续恢复、撤回自有排队消息，并将已有长期目标暂停；当前回合可能仍需手动停止 |
| POST | `/api/goals/remove` | `{"confirm":true,"thread_ids":["thread-id"]}` | 只处理已管理的续跑记录；撤回、暂停目标并通过桌面端精准中断当前回合，再移出列表 |
| POST | `/api/goals/cancel` | `{"confirm":true,"thread_ids":["thread-id"]}` | 需确认；停止指定线程的投递，省略或传空数组时取消当前续跑任务；已排进队列的消息不会撤回 |
| POST | `/api/goals/create` | `{"thread_ids":["thread-id"],"objective":"完成当前任务","status":"paused","confirm":true}` | 需确认；通过 app-server 创建目标，已有目标会跳过 |

`/api/goals/cancel-queued` 的逐会话 `state` 为 `cancelled`、`cancelling`、`already_removed`、`no_owned_message` 或 `failed`。`already_removed` 不证明执行，也不表示停止；`owned_queue:true` 仅授予撤回该条 Switcher 消息的能力。

实际投递需同时设置 `dry_run:false`、`confirm:true`；一次最多选择 10 个会话，同一时间只处理一批投递，各会话的实际回合允许并行执行：

```json
{
  "thread_ids": ["thread-id"],
  "dry_run": false,
  "confirm": true,
  "continue_once": false,
  "restart_desktop": false
}
```

页面默认使用 `continue_once:false`：缺少目标时先创建并激活目标；已有暂停或额度受限目标恢复为激活，保留原描述。已完成、受阻和预算受限目标不重新激活。兼容参数 `continue_once:true` 仍保留已有目标状态，但缺少目标时同样尝试补建。自动目标只要求完成本会话已授权、尚未完成的任务，不扩展范围。

补建失败仍调用 `thread/queue/add` 发送「继续」，`goal_creation_error` 独立记录警告，不将正常入队误报为失败；`goal_created:true` 表示本次已确认补建成功。补建成功但入队失败时，新目标暂停保留，重试时复用。真实执行仍由已运行的 Codex 桌面端负责，`active` 或目标创建成功均不作为回合执行证据。

`restart_desktop:true` 在请求校验通过且任务预留后，由工作线程重启桌面端；重启失败时任务失败，不投递队列消息。`takeover_desktop` 为旧参数，仍接受布尔值但已无效果（不会再关闭或接管桌面端），传入时任务日志会记录一条说明。预览不会重启桌面端，也不会写队列。这些选项均须使用 JSON 布尔值。

逐目标结果里，`ok:true` 表示消息已进入队列；兼容字段 `delivered:true` 仅表示观察到队列行消失（属于投递状态，不作为执行结果），`pending:true` 表示仍留在队列中（任务整体仍算 `done`）。执行、编辑和删除都可能导致出队，不能据此确认回合开始。`owned_queue` 表示该条消息由 Switcher 创建，`queued_id` 是队列行 ID，`delivered_at` 是观察到出队的时间。`start_timeout_seconds` 是任务内观察等待的秒数。`wake.ok:true` 仅表示已发出后台唤醒请求，不是桌面端接收或执行确认。

`last_resume[thread_id].execution` 与任务逐会话结果中的 `execution` 是本次续跑的回合回执，独立于 `ok` / `delivered`。状态为 `queued`、`confirming`、`running`、`started`（确认开始但写锁未确认）、`completed`、`failed`、`interrupted`、`unconfirmed` 或 `cancelled`。匹配后提供 `turn_id`、`message_id`、`started_at`、`completed_at` 与可选 `error_code`。

回执使用投递前文件位置、投递时间、新队列回合和初始消息摘要关联；出现多个候选时保留未确认，手动回合、旧回合和其他内容不会冒充本次执行。后台观察独立于投递任务存活，出队后继续跟踪到回合结束，重启后恢复。`execution_watch.active` 让页面在队列清空后仍更新结果。出队超过 90 秒仍缺少匹配证据时转为 `unconfirmed`，不会重新投递；迟到的有效证据仍可更新结果。

任务结束后，若 `pending` 仍为真，队列看护会继续只读检查 `~/.codex/queue_1.sqlite`，按 15/30/60/120/300/600 秒退避重发唤醒；行消失时把 `pending` 改成 `false`、`delivered` 改成 `true` 并写回历史。`/api/goals` 的 `queue_watch` 返回 `{enabled, pending, entries[]}`，每条 entry 含 `thread_id`、`queued_id`、`attempts`、`last_wake_at`、`next_wake_at`、`expires_at`、`pending` / `delivered`、`error`；`last_resume[thread_id]` 同时带上 `queued_id`、`pending`、`delivered`、`delivered_at`、`queue_expired`、`queue_abandoned`、`wake` 与 `watch`。超过 TTL 仍未取走时标记 `error:"queue_ttl_expired"`、`queue_expired:true`，不再重试。

会话被归档时，`threads.archived` 为真，归档会话不会执行任何消息，因此投递会直接跳过：结果带 `queue_abandoned:true`、`pending:false`、`delivered:false`（页面显示「未续跑 · 会话已归档」）。看护也会对已归档会话的滞留行停止唤醒并标 `error:"thread_archived"`。这既不是投递成功也不是已续跑，看护不会删除该队列行；用户可显式撤回由 Switcher 创建的消息。`state_5.sqlite` 读不到或没有 `archived` 列时按「无法判断」处理，行为与普通会话一致。

任务状态为 `running`、`done`、`partial`、`failed`、`cancelled`；`done` 表示本次操作成功执行，目标最终状态仍需查看 `status_after`。部分目标失败返回 `partial`，全部失败返回 `failed`。取消结果以 `cancelled:true` 和 `error:"cancelled"` 保存在逐目标记录中；用户跳过队列项使用 `skipped_by_user:true`。

`pending_resume` 单独保留 Switcher 的待执行消息，因此预览或后续失败不会丢失撤回能力。`execution[thread_id]` 返回会话的回合证据，与投递结果分开判断。

会话列表的 `sessions.total` 为过滤后符合条件的去重会话数，`shown` 为实际加载数，`raw_total` 包含隐藏内部会话，`truncated` 表示普通会话超过加载上限。置顶和已有目标的会话不占普通会话的加载限额。数据库读取失败通过 `error` / `sessions_error` 返回，不能据此认定会话已被删除。

自动恢复逐会话开启；设置中的总开关默认开启，已保存的关闭选择保留。切号前保存回合证据，仅恢复明确额度受限或被本次重启打断的会话；主动暂停、完成和空闲会话跳过。切号期间已结束或已自行恢复的回合也会跳过。创建目标一次最多 50 个，`status` 为 `paused`（默认）或 `active`；省略目标描述时使用「直到完成任务」，描述最多 400 字。创建目标不会启动回合，部分失败返回 HTTP 207，详见 `created`、`skipped`、`errors`。

`resume_queue` 为按会话合并的运行看板，返回 `items[]`、分组 `counts`、活动会话数 `active` 和 `parallel:true`。只包含实际续跑请求，排除只读预览。各项包含 `thread_id`、标题、项目、`source`（`manual` / `automatic`）、`group`（`waiting` / `running` / `attention` / `recent`）、`state`、`label`、当前目标与最近 10 次 `attempts`，以及 `can_cancel`、`can_pause`、`can_retry`、`can_resume`、`needs_manual_stop`。保留全部待处理会话及最近 20 条已结束会话。请求回执仍只对应原回合；看板可另外关联时间晚于它的目标触发回合，避免把目标的一轮结束误报成任务完成。

`/api/goals/pause` 一次最多处理 50 个明确 ID，返回逐会话 `goal_paused`、`queue`、`needs_manual_stop`、`state` 及可选 `error`。部分失败返回 HTTP 207。暂停意图先保存到 `resume-pauses.json`，切号和服务重启后继续生效，即使目标 RPC 失败也阻止 Switcher 自动恢复该会话；手动实际继续会解除该意图，预览不会解除。暂停不修改逐会话自动恢复偏好，也不声称已中断桌面端回合。`paused_threads` 返回这些意图及暂停结果。

`can_remove` 表示支持强制停止并移除记录。`/api/goals/remove` 一次最多 50 个会话，返回逐项 `removed`、`stopped`、`turn_id`、`evidence` 与可选 `error`；部分失败返回 207。先保存暂停意图，再撤回自有消息、暂停目标，通过 `\\.\pipe\codex-ipc` 查找实际会话 owner 并发送 `thread-follower-interrupt-turn`。请求包含 `expectedTurnId`，确认返回的 `interruptedTurnId` 相同才算中断成功；通道不可用、版本不匹配或回合已变化时保留记录并报错。移出标记存于 `resume-pauses.json` 的 `removed_job_id`，不删除原会话或投递历史；新的手动续跑会解除它。

`/api/health` 和 `/api/goals` 的 `session_api_version:4` 用于页面兼容检查。版本不足表示后台仍在运行旧代码；更新文件后须重启服务，单独刷新页面不会重新加载 Python 后端。

## 桌面端、短信与扩展

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/desktop/status` | — | 桌面客户端状态 |
| GET | `/api/desktop/processes` | — | 桌面端、app-server、独立 CLI 等相关进程 |
| POST | `/api/desktop/restart` | `{"confirm":true}` | 需确认；强制请求重启桌面端，独立于自动重启配置 |
| GET | `/api/sms/status` | 可选 `?balance=1` | 平台、余额、号码、最近短信与事件；`balance=1` 强制刷新余额 |
| POST | `/api/sms/test` | `{"confirm":true}` | 需确认；取号后安排释放，暂时占用平台额度 |
| POST | `/api/sms/cancel` | `{"confirm":true,"activation_id":"activation-id"}` | 需确认；释放指定号码，省略 ID 时处理当前全部号码 |
| GET | `/api/extension/status` | — | 扩展版本、派生 ID、Chrome 路径、配置目录检测结果 |
| POST | `/api/extension/install` | `{"target":"path","copy":true}` | 无需确认；默认复制扩展路径，`target:"url"` 复制扩展管理页地址 |

桌面端重启只处理 `ChatGPT` 进程树，不关闭独立终端中的 Codex CLI。短信接口成功表示已安排释放，退款结果及等待时间取决于平台。扩展安装接口不启动浏览器，也不执行静默安装；`copy:false` 可只获取信息。

## 常见错误

| HTTP 状态 | 示例错误码 | 含义 |
|---|---|---|
| `400` | `bad_request`、`confirmation_required`、`invalid_settings` | JSON、参数或确认条件不满足 |
| `403` | `goal_resume_disabled` | 续跑未启用 |
| `404` | `account_not_found`、`snapshot_not_found`、`session_not_found` | 对象不存在 |
| `409` | `already_active`、`job_running` | 目标账号已生效，或已有续跑任务 |
| `422` | `target_snapshot_invalid`、`snapshot_email_mismatch` | 目标凭据不可用或身份不匹配 |
| `423` | `switching_disabled`、`login_disabled` | 切换执行器不可用，或登录助手未启用 |
| `500` | `backup_failed`、`write_failed`、`verify_failed`、`internal_error` | 文件操作或服务内部错误 |

错误状态按具体接口区分，例如 `/api/login/start` 对部分登录错误统一返回 400，`/api/accounts/login` 对账号不存在返回 404。额度查询还需检查嵌套 `quota.ok` 和 `quota.error`。
