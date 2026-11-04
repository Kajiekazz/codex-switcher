# 配置与数据参考

[返回首页](../README.md) · [使用指南](usage.md) · [API](api.md) · [开发](development.md) · [常见问题](troubleshooting.md)

## 启动参数

安装版通过桌面图标启动；源码版在仓库根目录运行 `pythonw switcher.pyw`，使用相同的原生托盘与服务生命周期。

| 参数 | 安装版示例 | 源码版示例 |
|---|---|---|
| 无参数：启动托盘并打开管理页 | `CodexSwitcher.exe` | `pythonw switcher.pyw` |
| `--background`：仅后台驻留托盘 | `CodexSwitcher.exe --background` | `pythonw switcher.pyw --background` |
| `--background --startup`：Windows 自启入口，启动 Codex 并恢复任务 | `CodexSwitcher.exe --background --startup` | `pythonw switcher.pyw --background --startup` |
| `--exit`：请求当前数据目录对应的实例退出 | `CodexSwitcher.exe --exit` | `pythonw switcher.pyw --exit` |

重复启动会复用已有服务。开机自启由托盘或设置面板管理，登录 Windows 时使用 `--background --startup`。它启动 Codex 后，按自动恢复总开关和各会话的选择继续未完成任务；状态保存到数据目录的 `startup-recovery.json`。登录、自动切号、自动重启和自动用券在设置面板开启；手动切换和续跑始终可用。

环境变量用于源码或高级配置，修改后需退出并重新启动服务。前台调试可运行 `python service.py`，在原终端按 `Ctrl+C` 停止；前台服务不受托盘启动器管理。

## 设置优先级与生效方式

支持设置面板的配置按 **`settings.json` 保存值 > 环境变量 > 内置默认值** 读取。要恢复环境变量或默认值，在设置面板清除对应项。

- `host`、`port` 标记为需要重启；保存响应的 `restart_required` 列出这些项。
- 其他设置通常在下次操作或监控周期读取；修改自动切换开关时，API 会同步启停监控线程。
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
| `CODEX_EXE` | 自动查找 | 设置页/引导保存的 `codex_exe` 优先，其次环境变量，再查找 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`、`PATH` 与 WindowsApps 中桌面端随附的 CLI |
| `GOALS_DB` / `STATE_DB` | `~/.codex/goals_1.sqlite` / `~/.codex/state_5.sqlite` | Codex 目标与会话数据库路径 |
| `QUEUE_DB` | `~/.codex/queue_1.sqlite` | Codex 会话队列数据库（看护只读，不写） |

## 自动切换

自动切换需启用 `ENABLE_AUTO_SWITCH=1`。手动切换没有总开关，旧的 `ENABLE_SWITCH` 环境变量和 `enable_switch` 保存值不再生效。默认在额度结果为 `limit_reached=true` 或 `allowed=false` 时尝试自动切换；开启预测式切换后，也可根据本地采样提前触发。

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

`GET /api/auto-switch/status` 的 `auto_reset.gate` 包括 `disabled`、`no_secondary`、`below_threshold`、`window_resets_soon`、`unknown`、`no_credit`、`not_applicable`、`weekly_cap`、`ok`。`not_applicable` 表示账号仍有券，但上游当前报告适用数量为零；不等于券已用完。上述门槛只影响自动用券；手动用券可直接提交并显式确认，由上游判断结果。接口与上游路径见 [API 参考](api.md#额度与自动切换)。

## 登录助手

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOGIN_DRIVER` | `manual` | 运行时及启动脚本默认值；可选 `manual` / `playwright` |
| `LOGIN_CONCURRENCY` | `2` | 排队登录并发数，1–4 |
| `AUTO_LOGIN_ON_IMPORT` | 按驱动确定 | 未设置时，仅启用登录助手且使用 `playwright` 时自动排队登录 |

登录助手需启用 `ENABLE_LOGIN=1`。设置面板的驱动默认回显为 `playwright`；实际服务驱动以 `/api/health` 的 `login_driver` 为准。源码模式需要安装 Playwright，并使用系统 Chrome 或引导下载的浏览器；绑定二维码解析另需 Pillow、ZXing。安装版自带所需库。登录在隔离 `CODEX_HOME` 中执行，成功后保存加密快照，不直接替换当前 `auth.json`。

导入格式与授权步骤见[使用指南](usage.md)。

## 目标续跑

手动续跑始终可用，旧的 `ENABLE_GOAL_RESUME` 开关不再生效。「单次续跑」先准备暂停目标，只发送一轮；「持续续跑」补建或恢复激活目标，由目标持续推进。补建失败仍尝试发送普通消息，单次模式无法确认目标已暂停时不投递。自动恢复沿用原模式，执行始终由已运行的 Codex 桌面端负责。后台唤醒与看护由以下配置控制。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GOAL_RESUME_START_TIMEOUT` | `12` | 任务内观察消息是否出队的秒数；超时后消息仍保留 |
| `GOAL_RESUME_WAKE` | `1` | 入队后向已运行的桌面端发送后台加载请求 |
| `GOAL_RESUME_WATCHDOG` | `1` | 投递后继续观察队列并重试唤醒 |
| `GOAL_RESUME_WATCH_INTERVAL` | `15` | 看护检查间隔秒数，5–600 |
| `GOAL_RESUME_WATCH_TTL` | `21600` | 最长看护秒数，60–604800 |
| `AUTO_RESUME_AFTER_SWITCH` | `1` | 设置中的自动恢复总开关，保存后即时生效；持续续跑默认开启对应会话的恢复偏好 |
| `AUTO_RESUME_THREADS` | `[]` | 已开启自动恢复的会话 ID，由持续续跑和会话行按钮维护 |
| `AUTO_RESUME_DISABLED_THREADS` | `[]` | 手动关闭自动恢复的会话 ID；再次持续续跑时保留关闭，手动开启可清除此偏好 |

自动恢复只处理有明确额度受限证据，或切号前正在执行且被本次桌面端重启打断的已选会话。主动暂停、完成、受阻、预算受限、归档与空闲会话会跳过。实际规则和操作见[使用指南](usage.md#任务续跑)。

`ok` 表示投递成功；兼容字段 `delivered` 仅表示消息已出队，不能证明执行。看护按 15/30/60/120/300/600 秒退避重试唤醒，最长持续到 TTL，状态保存在 `goal-resume-queue.json`。独立的回合观察器每 5 秒跟踪本次队列消息的执行证据，页面打开时也会刷新；出队后继续观察，回执保存在 `resume-execution.json`，不保存消息和工具输出原文。未匹配消息的发现期为 24 小时；已匹配的长期回合持续观察到结束。进程重启会触发提前唤醒；约 2 MB 的挂起桌面占位进程不视为已就绪。

取消排队只通过 `thread/queue/delete` 撤回已确认属于 Switcher 的消息。暂停后续续跑会同时暂停长期目标，意图持久化到 `resume-pauses.json`；只有手动继续解除，切号不会覆盖它。用户自己添加的消息不会删除，已经开始的回合需在 Codex 内停止。执行状态读取有界的回合记录尾部并结合写锁判断，缺少证据时保留「未确认」。续跑队列按会话合并展示，目标后续回合持续观察，请求回执保留原回合结果。

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
| `accounts.sqlite3` 的 `pending_totp` 表 | 已读取但尚未确认绑定成功的密钥，使用 DPAPI 加密；不作为已配置凭据返回 |
| `onboarding.json` | 引导步骤、组件模式和本地账号勾选偏好，不保存密码或令牌 |
| `auto-switch.recovery.json` | 当前等待额度恢复的账号，服务重启后继续检查 |
| `resume-modes.json` | 各会话的单次／持续续跑模式，切号和重启后保留 |
| `model-access.json` | 续跑实际确认的模型权限结果，按凭据指纹区分，30 分钟内用于避免重复拒绝 |
| `backups\` | 切换前创建的加密凭据备份 |
| `settings.json` | 用户配置，密钥字段使用 DPAPI 加密 |
| `ui-prefs.json` | 控制台与扩展共用的主题设置 |
| `auto-switch.log` | 自动切换事件，可包含邮箱与操作结果 |
| `login-history.json` | 登录历史，不保存设备码与完整 CLI 输出 |
| `resume-execution.json` | 本次续跑消息与真实回合的关联、游标和执行回执 |
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
