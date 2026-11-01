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
| POST | `/api/switching/toggle` | `{"key":"enable_auto_switch","value":true,"confirm":true}` | 需确认；`key` 仅支持 `enable_switch`、`enable_auto_switch` |
| GET | `/api/theme` | — | 当前主题、更新时间、支持主题 |
| POST | `/api/theme` | `{"theme":"dark"}` | 无需确认；支持 `light`、`dark`、`auto` |
| GET | `/api/theme/events` | — | SSE；先发送当前主题，后续推送更新 |

`clear` 删除持久设置，使其回退到环境变量或默认值。直接调用设置 API 时，保留原密钥应省略对应字段，空字符串并不表示“不修改”。

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

开始登录需要启用登录助手、账号存在且已保存密码，并能找到 Codex CLI。默认禁用时返回 `423 login_disabled`。登录在隔离目录生成凭据，成功后保存加密快照，不自动切换当前账号。

## 额度与自动切换

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/quota` | `?force=1`、`?email=account%40example.com` | 默认查当前账号；`force=1` 跳过缓存，`email` 指定账号 |
| GET | `/api/accounts/quotas` | 可选 `?force=1` | 并行读取账号池额度，默认缓存 60 秒，无快照账号列入 `skipped` |
| GET | `/api/quota/history` | `?email=account%40example.com&hours=6` | 采样与预测；未指定邮箱时使用当前账号，小时数默认 24，限制 1–168 |
| GET | `/api/auto-switch/status` | — | 策略、最近结果、冷却、熔断、频率计数、`auto_reset.gate` |
| POST | `/api/quota/reset` | `{"confirm":true}` | 需允许切换且确认；消耗一张适用重置券 |
| POST | `/api/switch` | `{"email":"account@example.com","confirm":true,"restart_desktop":true}` | 需允许切换且确认；目标需有可解析且身份匹配的快照 |

`/api/quota/reset` 可选 `email`、`credit_id`；不传时使用当前账号并选择最早到期的可用券。成功后尝试刷新额度，响应含 `result` 和 `quota`。手动消费不要求启用自动用券，也不应用其自动策略阈值。

上游查询路径默认 `/backend-api/wham/rate-limit-reset-credits`，消费路径追加 `/consume`。消费使用 `credit_id` 和程序生成的幂等 UUID `redeem_request_id`；本地接口不接受自定义幂等 ID。上游错误可能包括 `nothing_to_reset`、`no_credit`、`already_redeemed`。

`/api/switch` 省略 `restart_desktop` 时使用服务配置，明确布尔值可覆盖本次行为。凭据切换成功但重启失败时，响应仍可能成功，应检查 `switch.desktop_restart`。`switch.auto_resume` 返回切号后的续跑结果：`job` 表示已提交任务，`skipped` 说明未启动原因，`ok:false` 表示续跑失败；这些结果不改变已完成的账号切换。

## 目标与续跑

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/goals` | 可选 `?subagents=1` | 目标、会话、任务、历史与自动续跑状态；内部会话默认隐藏，置顶或已有目标的会话保留 |
| POST | `/api/goals/resume` | `{"thread_ids":["thread-id"],"dry_run":true}` | 默认预览，无需确认；预览也需要启用目标续跑 |
| POST | `/api/goals/auto-resume` | `{"thread_id":"thread-id","enabled":true}` | 无需 `confirm`；保存或移除切换后自动续跑目标 |
| POST | `/api/goals/cancel` | `{"confirm":true,"thread_ids":["thread-id"]}` | 需确认；停止指定线程的投递，省略或传空数组时取消当前续跑任务；已排进队列的消息不会撤回 |
| POST | `/api/goals/create` | `{"thread_ids":["thread-id"],"objective":"完成当前任务","status":"paused","confirm":true}` | 需启用目标续跑并确认；通过 app-server 创建目标，已有目标会跳过 |

实际续跑需要启用目标续跑，并同时设置 `dry_run:false`、`confirm:true`；一次最多选择 10 个目标，同一时间只运行一个续跑任务：

```json
{
  "thread_ids": ["thread-id"],
  "dry_run": false,
  "confirm": true,
  "restart_desktop": false
}
```

续跑是投递而不是后台执行：服务调用 `thread/goal/set` 把目标置为 `active`（已是 `active` 时跳过），再调用 `thread/queue/add` 把一条「继续」放进该会话队列。回合由打开该会话的 Codex 桌面端执行，服务不持有线程写锁，也不会关闭桌面端。

`restart_desktop:true` 在请求校验通过且任务预留后，由工作线程重启桌面端；重启失败时任务失败，不投递队列消息。`takeover_desktop` 为旧参数，仍接受布尔值但已无效果（不会再关闭或接管桌面端），传入时任务日志会记录一条说明。预览不会重启桌面端，也不会写队列。这些选项均须使用 JSON 布尔值。

逐目标结果里，`ok:true` 表示消息已进入队列；`delivered:true` 表示桌面端已在等待超时前取走它，`pending:true` 表示仍留在队列中（任务整体仍算 `done`）。`start_timeout_seconds` 是这次确认等待的秒数。

任务状态为 `running`、`done`、`partial`、`failed`、`cancelled`；`done` 表示本次操作成功执行，目标最终状态仍需查看 `status_after`。部分目标失败返回 `partial`，全部失败返回 `failed`。取消结果以 `cancelled:true` 和 `error:"cancelled"` 保存在逐目标记录中；用户跳过队列项使用 `skipped_by_user:true`。

会话列表的 `sessions.total` 为过滤后符合条件的去重会话数，`shown` 为实际加载数，`raw_total` 包含隐藏内部会话，`truncated` 表示普通会话超过加载上限。置顶和已有目标的会话不占普通会话的加载限额。数据库读取失败通过 `error` / `sessions_error` 返回，不能据此认定会话已被删除。

自动续跑在切换成功后处理已指定目标，无需逐次确认，仍依赖目标续跑和全局自动续跑开关。创建目标一次最多 50 个，`status` 为 `paused`（默认）或 `active`；省略目标描述时使用「直到完成任务」，描述最多 400 字。创建目标不会启动回合，部分失败返回 HTTP 207，详见 `created`、`skipped`、`errors`。

`/api/health` 和 `/api/goals` 的 `session_api_version` 用于页面兼容检查。缺少该字段表示后台仍在运行旧代码；更新文件后须重启服务，单独刷新页面不会重新加载 Python 后端。

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
| `403` | `goal_resume_disabled`、`switching_disabled` | 续跑未启用，或消费重置券时未允许切换 |
| `404` | `account_not_found`、`snapshot_not_found`、`session_not_found` | 对象不存在 |
| `409` | `already_active`、`job_running` | 目标账号已生效，或已有续跑任务 |
| `422` | `target_snapshot_invalid`、`snapshot_email_mismatch` | 目标凭据不可用或身份不匹配 |
| `423` | `switching_disabled`、`login_disabled` | 切换或登录未启用 |
| `500` | `backup_failed`、`write_failed`、`verify_failed`、`internal_error` | 文件操作或服务内部错误 |

错误状态按具体接口区分，例如 `/api/login/start` 对部分登录错误统一返回 400，`/api/accounts/login` 对账号不存在返回 404。额度查询还需检查嵌套 `quota.ok` 和 `quota.error`。
