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

`/api/switch` 省略 `restart_desktop` 时使用服务配置，明确布尔值可覆盖本次行为。凭据切换成功但重启失败时，响应仍可能成功，应检查 `switch.desktop_restart`。切换成功还可能触发已配置的目标自动续跑。

## 目标与续跑

| 方法 | 路径 | 参数或请求体示例 | 返回内容 / 条件 |
|---|---|---|---|
| GET | `/api/goals` | 可选 `?subagents=1` | 目标、会话、任务、历史与自动续跑状态，默认不包含子代理会话 |
| POST | `/api/goals/resume` | `{"thread_ids":["thread-id"],"dry_run":true}` | 默认预览，无需确认；预览也需要启用目标续跑 |
| POST | `/api/goals/auto-resume` | `{"thread_id":"thread-id","enabled":true}` | 无需 `confirm`；保存或移除切换后自动续跑目标 |
| POST | `/api/goals/cancel` | `{"confirm":true,"thread_ids":["thread-id"]}` | 需确认；停止指定线程，省略或传空数组时取消当前续跑任务 |
| POST | `/api/goals/create` | `{"thread_ids":["thread-id"],"objective":"完成当前任务","status":"paused","confirm":true}` | 需确认；通过 app-server 创建目标，已有目标会跳过 |

实际续跑需要启用目标续跑，并同时设置 `dry_run:false`、`confirm:true`；一次最多选择 10 个目标，同一时间只运行一个续跑任务：

```json
{
  "thread_ids": ["thread-id"],
  "dry_run": false,
  "confirm": true,
  "restart_desktop": false
}
```

`restart_desktop:true` 在实际续跑前请求重启桌面端。当前实现先重启，再校验续跑开关与确认，因此校验失败时桌面端仍可能已重启；仅需预览时保留 `dry_run:true`。

自动续跑在切换成功后处理已指定目标，无需逐次确认，仍依赖目标续跑和全局自动续跑开关。创建目标一次最多 50 个，`status` 为 `paused`（默认）或 `active`；省略目标描述时使用「直到完成任务」，描述最多 400 字。创建目标不会启动回合，部分失败返回 HTTP 207，详见 `created`、`skipped`、`errors`。

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
