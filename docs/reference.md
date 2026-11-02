# 配置与数据参考

[返回首页](../README.md) · [使用指南](usage.md) · [API](api.md) · [开发](development.md) · [常见问题](troubleshooting.md)

## 启动参数

在仓库根目录运行 `start.ps1`，按需组合以下参数。未指定的功能开关会被脚本写为关闭；已保存的设置仍可覆盖脚本参数。

| 参数 | 作用 | 对应环境变量 |
|---|---|---|
| `-EnableSwitch` | 允许替换本机 `auth.json` | `CODEX_SWITCHER_ENABLE_SWITCH=1` |
| `-EnableAutoSwitch` | 启用自动切换，还需允许切换账号 | `CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1` |
| `-EnableAutoReset` | 允许按策略消耗重置券 | `CODEX_SWITCHER_ENABLE_AUTO_RESET=1` |
| `-EnableLogin` | 启用设备码登录助手 | `CODEX_SWITCHER_ENABLE_LOGIN=1` |
| `-Driver manual` / `-Driver playwright` | 登录驱动，脚本默认 `manual` | `CODEX_SWITCHER_LOGIN_DRIVER` |
| `-EnableGoalResume` | 启用目标续跑 | `CODEX_SWITCHER_ENABLE_GOAL_RESUME=1` |
| `-RestartDesktop` | 切换账号后重启桌面端 | `CODEX_SWITCHER_RESTART_DESKTOP=1` |
| `-Tray` | 后台运行服务并显示托盘，快捷键 `Ctrl+Alt+C` | — |

更改启动参数或环境变量后，先停止已有服务，再重新启动：

```powershell
.\stop.ps1
.\start.ps1 -EnableLogin -EnableSwitch -EnableAutoSwitch -RestartDesktop
```

`-Tray` 检测到已有服务时会复用该进程，不会修改它的配置。`stop.ps1` 和托盘启动检测使用固定端口 `8787`；自定义端口时需另行停止或检查对应进程。

## 设置优先级与生效方式

支持设置面板的配置按 **`settings.json` 保存值 > 环境变量 > 内置默认值** 读取。启动参数通过环境变量生效，也会被持久设置覆盖。要恢复环境变量或默认值，在设置面板清除对应项。

- `host`、`port`、`enable_goal_resume` 标记为需要重启；保存响应的 `restart_required` 列出这些项。
- 其他设置通常在下次操作或监控周期读取；修改允许切换和自动切换开关时，API 会同步启停监控线程。
- 环境变量需在启动服务前设置，其他终端中的修改不会改变已有服务进程。
- 密钥字段使用 DPAPI 加密保存，查询时掩码显示。设置面板留空表示不修改；直接调用 API 时应省略不修改的字段。

## 环境变量约定

以下表格中的变量均需加 `CODEX_SWITCHER_` 前缀，例如 `AUTO_SWITCH_INTERVAL` 表示 `CODEX_SWITCHER_AUTO_SWITCH_INTERVAL`。默认值以运行时配置为准，布尔值使用 `1` / `0`。设置面板提供字段范围、可选值和重启标记，可通过 `GET /api/settings` 查询。

### 基础路径

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8787` | 监听地址与端口 |
| `DATA` | `%LOCALAPPDATA%\CodexSwitcher` | 数据目录 |
| `AUTH` | `~/.codex/auth.json` | 当前凭据文件，也是切换写入目标 |
| `CODEX_EXE` | 自动查找 | 优先使用此路径，其次查找 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`，再查找 `PATH` 中的 `codex` |
| `GOALS_DB` / `STATE_DB` | `~/.codex/goals_1.sqlite` / `~/.codex/state_5.sqlite` | Codex 目标与会话数据库路径 |

## 自动切换

自动切换需同时启用 `ENABLE_SWITCH=1` 和 `ENABLE_AUTO_SWITCH=1`。默认在额度结果为 `limit_reached=true` 或 `allowed=false` 时尝试切换；开启预测式切换后，也可根据本地采样提前触发。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTO_SWITCH_INTERVAL` | `90` | 检查间隔秒数，15–3600 |
| `AUTO_SWITCH_COOLDOWN` | `900` | 基础冷却秒数，60–86400 |
| `AUTO_SWITCH_MAX_PER_HOUR` | `3` | 滚动一小时最多成功切换次数，1–24 |
| `AUTO_SWITCH_MAX_PROBES` | `0` | 每轮探测上限，0–500；`0` 表示全部候选 |
| `AUTO_SWITCH_FAILURE_THRESHOLD` | `3` | 连续切换失败触发熔断的次数，1–20 |
| `PREDICTIVE_SWITCH` / `PREDICTIVE_THRESHOLD` | `0` / `600` | 预测式切换开关与耗尽阈值秒数 |
| `RESTART_DESKTOP` / `RESTART_DESKTOP_TIMEOUT` | `0` / `30` | 切换后重启桌面端及超时秒数；手动请求可覆盖重启开关 |
| `QUOTA_CACHE_TTL` | `5` | 当前账号额度缓存秒数 |
| `POOL_QUOTA_TTL` | `60` | 账号池额度缓存秒数 |
| `USAGE_URL` | `https://chatgpt.com/backend-api/wham/usage` | 上游额度查询地址 |

候选必须有凭据快照，且未停用、不在冷却期、额度查询成功并允许使用。探测并行执行，按剩余 5 小时额度、剩余 7 天额度、`priority`、快照保存时间依次降序选择。设置探测上限后，先按优先级、令牌未过期、快照保存时间确定探测范围。

已用尽的候选和被切出的账号会结合重置时间进入冷却。无候选时默认等待 5 分钟；已知候选将在 15 分钟内重置时，安排在重置后约 5 秒重试。连续切换失败达到阈值后熔断 30 分钟。冷却、频率计数、熔断和自动用券计数保存在内存中，服务重启后重新计数。

## 额度重置券

自动用券需启用 `ENABLE_AUTO_RESET=1`，默认关闭。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTO_RESET_SECONDARY_THRESHOLD` | `98` | 7 天窗口已用比例阈值（%） |
| `AUTO_RESET_MIN_WAIT_HOURS` | `72` | 已知自然重置等待时间低于此值时不用券（小时） |
| `AUTO_RESET_MAX_PER_WEEK` | `1` | 滚动 7 天自动消耗上限；重启服务会清空计数 |
| `AUTO_RESET_BEFORE_SWITCH` | `0` | 改为先尝试消耗重置券，再选择其他账号 |
| `RESET_CREDITS_URL` | 空 | 覆盖重置券查询地址；未设置时由额度接口地址推导 |

默认策略在自动切换被触发且无可用候选时，检查以下条件：7 天窗口已用比例达到阈值、已知重置等待时间不低于门槛、存在适用券、未达到自动消耗上限。未知或为零的重置等待时间不会单独阻止用券；开启 `AUTO_RESET_BEFORE_SWITCH` 后不要求先确认账号池无候选。

`GET /api/auto-switch/status` 的 `auto_reset.gate` 包括 `disabled`、`no_secondary`、`below_threshold`、`window_resets_soon`、`unknown`、`no_credit`、`weekly_cap`、`ok`。手动消耗不受自动策略阈值约束，需要允许切换和显式确认；每张券只能使用一次。接口与上游路径见 [API 参考](api.md#额度与自动切换)。

## 登录助手

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOGIN_DRIVER` | `manual` | 运行时及启动脚本默认值；可选 `manual` / `playwright` |
| `LOGIN_CONCURRENCY` | `2` | 排队登录并发数，1–4 |
| `AUTO_LOGIN_ON_IMPORT` | 按驱动确定 | 未设置时，仅启用登录助手且使用 `playwright` 时自动排队登录 |

登录助手需启用 `ENABLE_LOGIN=1`。设置面板的驱动默认回显为 `playwright`；实际服务驱动以 `/api/health` 的 `login_driver` 为准。Playwright 模式需要额外安装 Playwright 和 Chrome。登录在隔离 `CODEX_HOME` 中执行，成功后保存加密快照，不直接替换当前 `auth.json`。

导入格式与授权步骤见[使用指南](usage.md)。

## 目标续跑

目标续跑需启用 `ENABLE_GOAL_RESUME=1` 并重启服务。

续跑不是后台跑回合：服务做三件事——把目标置为 `active`（`thread/goal/set`），把一条「继续」放进该会话的队列（`thread/queue/add`），再用 `codex://threads/wake/<会话>` 深链让**已在运行**的 Codex 桌面端加载该会话。前两个调用不需要线程写锁，第三个只发给已经开着的桌面端（不会替你启动或关闭它）；桌面端自己的主进程看到队列非空才会恢复该会话，回合从头到尾都在 Codex 里跑。桌面端没运行时不会发深链，消息留在队列里，等下次打开该会话时执行。

「在运行」按进程工作集判断：Windows 在框架包没就绪时会留下单线程、约 2 MB 的 `ChatGPT.exe` 挂起进程，`tasklist` 照样列出，但它永远不会取走队列。这种进程不算运行，唤醒返回 `skipped` 且 `reason=desktop_not_ready`，也不会发深链；深链发出后是否真的送达，只看队列里那条消息有没有被取走。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GOAL_RESUME_START_TIMEOUT` | `12` | 等桌面端取走排队消息的秒数；超时只影响状态显示，消息仍会送达 |
| `GOAL_RESUME_WAKE` | `1` | 排进队列后用 `codex://threads/wake/<会话>` 让已运行的桌面端加载该会话；设为 `0` 则只排队，等你手动打开 |
| `GOAL_RESUME_WATCHDOG` | `1` | 任务结束后继续只读检查队列，直到桌面端真的取走消息；设为 `0` 则只在任务内确认一次 |
| `GOAL_RESUME_WATCH_INTERVAL` | `15` | 看护读取 `queue_1.sqlite` 的间隔秒数，5–600 |
| `GOAL_RESUME_WATCH_TTL` | `21600` | 看护最长重试秒数（6 小时），60–604800 |
| `AUTO_RESUME_AFTER_SWITCH` | `0` | 切号后自动续跑已标记的目标（需先在会话行点「自动」）；默认关闭 |
| `AUTO_RESUME_THREADS` | `[]` | JSON 数组形式的目标 ID，通常由会话行的「自动」按钮维护 |

「已排队」和「已续跑」是两件事：`ok` 只代表「继续」已经写进队列，`delivered` 才代表桌面端把这一行取走了。切号会重启桌面端，落在启动窗口里的唤醒会被丢掉，桌面端也可能正在跑别的回合；所以任务结束后由队列看护接管——每 15 秒只读查询一次 `~/.codex/queue_1.sqlite`，行消失才标记 `delivered`（页面显示「已续跑」），仍滞留则按 15/30/60/120/300/600 秒退避重发同一条深链，超过 TTL 标记 `queue_ttl_expired`（页面显示「已排队 · 桌面端未取走」）。看护不写队列、不启动也不关闭桌面端；数据库读不到时按「无法判断」处理，绝不会凭一次读失败就宣称已续跑。状态保存在 `goal-resume-queue.json`，服务重启后继续看护。

续跑请求默认仅预览，实际执行需要 `dry_run:false` 和 `confirm:true`。自动续跑只处理用户指定且状态符合条件的目标。曾经用于抢写锁的 `GOAL_RESUME_MAX_RUNTIME`、`GOAL_RESUME_POLL`、`GOAL_RESUME_TAKEOVER`、`GOAL_RESUME_TAKEOVER_WAIT`、`GOAL_RESUME_TRACE` 已随队列模式移除，设置后不再生效。

## 接码

本组兼容不带 `CODEX_SWITCHER_` 前缀的名称，例如 `SMS_PROVIDER`、`L_API_BASE`。运行时优先读取带前缀的变量；设置面板回显依据登记的兼容变量，实际状态可查询 `/api/sms/status`。

| 变量 | 默认值或作用 |
|---|---|
| `SMS_PROVIDER` | `grizzly` / `smsbower` / `l` / `h`；空表示关闭 |
| `SMS_SERVICE` / `SMS_COUNTRY` / `SMS_MAX_PRICE` | 服务码 `dr`、国家 ID `10`、最高价默认不指定 |
| `SMS_CODE_WAIT` / `SMS_POLL_INTERVAL` / `SMS_MAX_RETRIES` | 等待短信 120 秒、轮询 5 秒、最多换号 10 次 |
| `SMS_REQUEST_TIMEOUT` | 平台请求超时，默认 30 秒 |
| `SMS_API_BASE` / `SMS_API_KEY` | GrizzlySMS 地址默认 `https://api.grizzlysms.com/stubs/handler_api.php`，密钥默认空 |
| `SMSBOWER_API_BASE` / `SMSBOWER_API_KEY` | SMSBower 地址默认 `https://smsbower.page/stubs/handler_api.php`，密钥默认空 |
| `SMSBOWER_USE_V2` / `SMSBOWER_PROVIDER_IDS` | 默认 `0`、空；可启用 `getNumberV2` 并指定逗号分隔的运营商 ID |
| `L_API_BASE` / `H_API_BASE` | 本地服务地址，均默认 `http://localhost:8788` |
| `L_ADMIN_AUTH_CODE` / `H_ADMIN_AUTH_CODE` | 本地服务授权码，默认空 |
| `L_PHONE_PREFIX` / `H_PHONE_PREFIX` | 号码国家前缀，默认空 |
| `H_PHONE_ACQUIRE_MODE` | 默认 `reusable`；`new` / `fresh` / `always_new` 使用一次性号码 |

取号测试先占用平台额度，再安排释放；释放时间和退款结果取决于平台响应。

## 数据目录与安全边界

默认数据目录为 `%LOCALAPPDATA%\CodexSwitcher\`，可通过 `CODEX_SWITCHER_DATA` 修改。

| 文件或目录 | 内容 |
|---|---|
| `accounts.sqlite3` | 账号与额度采样；密码、TOTP 和凭据快照使用 DPAPI 加密，邮箱、标签等元数据未加密 |
| `backups\` | 切换前创建的加密凭据备份 |
| `settings.json` | 用户配置，密钥字段使用 DPAPI 加密 |
| `ui-prefs.json` | 控制台与扩展共用的主题设置 |
| `auto-switch.log` | 自动切换事件，可包含邮箱与操作结果 |
| `login-history.json` | 登录历史，不保存设备码与完整 CLI 输出 |
| `goal-resume-history.jsonl` | 目标续跑历史（含队列行 ID、`pending` / `delivered`） |
| `goal-resume-queue.json` | 队列看护状态：仍在等桌面端取走的「继续」、重试次数与 TTL |
| `login-sessions\` | 隔离登录目录、`cli.log`、`driver.log` 和失败截图 |
| `browser-profile\` | 登录自动化使用的浏览器数据 |

DPAPI 使用当前 Windows 用户上下文，数据不能通过简单复制目录直接迁移到其他用户或机器。登录流程会短暂生成明文 `auth.json`，结束时清理临时凭据；当前 `~/.codex/auth.json` 仍是 Codex 使用的凭据文件，不属于账号库的加密存储。

服务默认监听本机回环地址，API 未配置独立身份认证且允许跨域请求，不应直接暴露到公网。API 不返回账号密码、TOTP 密钥或凭据令牌，但会返回邮箱、路径及运行状态。日志、截图和浏览器数据也应按敏感本地数据处理。

切换流程包括当前凭据快照、加密备份、原子替换和写后校验，失败时尝试回滚并报告结果。桌面端重启是独立步骤；重启失败不代表凭据切换失败，应检查 `desktop_restart`。自动重启只处理桌面端 `ChatGPT` 进程树，保留独立终端中的 Codex CLI 会话。

## 适用范围与限制

- 依赖 Windows DPAPI、PowerShell 和桌面功能。
- 额度与重置券使用非公开上游接口，路径和字段可能变化。
- 切换本地凭据不会改变服务端套餐或额度；令牌撤销、登录失效仍需重新授权。
- 自动填写的 2FA 支持 TOTP，邮箱验证码等步骤可能需要人工操作。
- 扩展需要 `chrome.sidePanel`，最低版本由扩展清单规定；旧 Tampermonkey 面板已归档至 `legacy/`。
- 自动切换和重启可能中断运行中的回合，应配置需要恢复的目标。
