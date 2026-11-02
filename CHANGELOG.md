# 更新日志

结构参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按 [SemVer](https://semver.org/lang/zh-CN/) 理解（0.x 阶段，本地 API 仍可能调整）。
用法请见 [README](README.md)。

## 未发布

### 新增

- **续跑队列**：只列 Switcher 发起的续跑，按会话合并为等待、执行中、需处理和最近结束；支持并行观察、取消本次排队、暂停后续续跑及批量暂停。
- **统一长期目标入口**：在会话详情查看、创建、继续或暂停目标；复用已有目标描述，普通续跑无需目标。自动恢复状态在会话与队列同步，总开关移入设置。
- **持久化暂停**：同时暂停长期目标，切号和服务重启尊重暂停意图；当前回合仍需在 Codex 停止时明确提示，不误报已停止。
- **队列看护（watchdog）**：任务结束后继续接管仍在队列里的「继续」——每 15 秒只读查询 `~/.codex/queue_1.sqlite`，行消失才标记 `delivered`；仍滞留时按 15/30/60/120/300/600 秒退避重发同一条 `codex://threads/wake/<会话>` 深链，覆盖「切号重启桌面端时唤醒落在启动窗口里被丢掉」和「桌面端正跑别的回合、队列暂时没人取」两种长期滞留。每分钟比对一次桌面端进程集合，整组换过（重启或应用更新）就立刻重发，不等退避。状态持久化到 `goal-resume-queue.json`，服务重启后继续；超过 6 小时标记 `queue_ttl_expired`。新设置项 `GOAL_RESUME_WATCHDOG` / `GOAL_RESUME_WATCH_INTERVAL` / `GOAL_RESUME_WATCH_TTL`。
- `/api/goals` 新增 `queue_watch`（`pending`、`entries[]`：重试次数、上次/下次唤醒、TTL），`last_resume[thread]` 新增 `queued_id`、`pending`、`delivered`、`delivered_at`、`queue_expired`、`watch`。
- **续跑后台唤醒**：入队后向已运行的桌面端发送 `codex://threads/wake/<会话>` 加载请求；发送成功不代表应用已收到或开始执行。桌面端未运行时保留队列，可用 `GOAL_RESUME_WAKE=0` 关闭唤醒。结果中的 `wake` 记录请求状态、URL 和进程。
- 唤醒前的存活检查改用 `tasklist`（约 0.7 s），不再为了一次检查去跑要解析 MSIX 的完整状态脚本（4–5 s）。

### 修复

- **启动窗口与服务隔离**：`start.ps1`、托盘启动改为独立隐藏服务进程，固定保存 stdout/stderr、PID 和退出码；关闭启动窗口不会连带结束服务。隔离测试覆盖 PowerShell 5 输出重定向导致普通 stderr 日志终止脚本的路径。
- **切号重启保护**：关闭桌面端前保护 Switcher 服务及其全部祖先进程；存在进程树包含关系时跳过关闭，避免杀掉启动 PowerShell。重启辅助进程使用独立隐藏控制台。
- **页面重复刷新**：相同 GET 合并进行中的请求，慢刷新不再重叠；隐藏页面暂停自动轮询，回到前台恢复。
- **后台唤醒未送达**：Windows PowerShell 在 `DETACHED_PROCESS` 下可能退出码为 0 却未交付协议 URI，导致消息需点开会话才执行。改用隐藏的 `CREATE_NO_WINDOW` 辅助进程；实机未打开会话验证，约 7 秒后记录到队列触发的新回合。
- **出队与执行分开核对**：`delivered` 仅表示队列行消失；通过对应队列回合确认开始、结束或失败。长期目标的后续目标回合继续归在同一会话行，单轮结束不会误报整个目标完成；缺少证据保留未确认。
- **重置券库存显示**：账号池与额度卡片统一区分剩余券数和当前适用数，修复有券但适用数为零时隐藏库存的问题。自动策略返回 `not_applicable`；手动用券可直接提交。
- **不再重复排队同一条「继续」**：切号可能在桌面端重启期间触发两次自动续跑，旧代码会往同一会话排两条「继续」，跑两遍回合、白烧额度。现在投递前先只读查 `queue_1.sqlite`：同线程已有文本恰为「继续」的行就直接复用（日志「队列里已有「继续」，不再重复排队」），不会多烧一轮。
- **已归档会话不再假装「等桌面端取走」**：归档会话的队列行永远不会被执行，旧显示会一直停在「已排队」直到 6 小时 TTL。现在投递前只读查 `state_5.sqlite` 的 `threads.archived`：已归档就直接跳过并在结果里标 `queue_abandoned`（页面「未续跑 · 会话已归档」）；看护对已归档会话的滞留行停止唤醒。这不算投递成功，队列行也不删。
- **不再把挂起的桌面端进程当成「在运行」**：框架包没就绪时 Windows 会留下单线程、约 2 MB 的 `ChatGPT.exe`，`tasklist` 照样列出来，但它执行不了任何东西。此前唤醒会照发、「重启桌面端」也会报成功；现在按工作集过滤（低于 20 MB 视为没启动），唤醒返回 `skipped` 且 `reason=desktop_not_ready`，重启返回 `launch_stuck` 并提示注销或重启系统。
- **唤醒深链改成 fire-and-forget**：避免将 ShellExecute 的等待超时当成唤醒失败。发出即返回；队列观察仅确认消息是否仍在排队，不确认执行。
- `tasklist` 的工作集字段带千分位引号（`"2,180 K"`），改为按 CSV 解析，避免内存值被截断。
- 切号响应返回自动续跑结果，显示未启动原因与回调错误；页面识别仍在运行的旧后端并提示重启。
- 会话列表在过滤内部线程后再限制普通会话数量，保留较旧的置顶和目标会话；数据库读取失败显示错误，不再当作空列表。
- 修正内部会话创建目标的判断、执行队列与目标状态展示，移除根据当前列表误删自动标记的入口。
- 自动恢复逐会话开启，总开关默认开启，已保存的全局暂停选择保留。
- 续跑校验通过后才执行桌面重启，预览不执行桌面操作；未启用续跑时禁止创建目标。
- 切号后结束旧账号的续跑任务，独立于是否开启自动续跑；保留当前及剩余队列的取消记录。
- 启动错误不再被宽泛的 `active` / `already` 匹配吞掉，失败时恢复原目标状态；任务结果区分成功、部分失败与全部失败。

### 变更

- 手动切换始终可用，移除页面和设置面板中的“允许切号”总开关；旧 `-EnableSwitch` 参数兼容接受但不再需要。自动切号仍独立控制。
- 手动用券不再按缓存库存、适用次数或自动策略禁用入口，也不依赖切号权限；提交后由上游判断实际结果。
- **续跑改成「排进会话队列，由桌面端执行」**：不再起后台 app-server 跑回合。服务只做 `thread/goal/set status=active`（已是 active 时跳过）和 `thread/queue/add "继续"`，两个调用都不需要线程写锁；回合在你打开着的 Codex 里跑，进度、中断和结果都在 Codex 界面里可见。桌面端没开或会话没打开时消息留在队列，打开时自动执行。
- **彻底移除桌面端接管**：删掉关窗口、重开窗口、抢写锁与回合监控的整条路径（`takeover_desktop` 仍被接受但已无效果，只在任务日志记一条说明）。「会话被占用」不再阻塞续跑，Dashboard 的「接管并重试」改为「重新排队」。
- **精简续跑配置**：移除 `GOAL_RESUME_MAX_RUNTIME`、`GOAL_RESUME_POLL`、`GOAL_RESUME_TAKEOVER`、`GOAL_RESUME_TAKEOVER_WAIT`、`GOAL_RESUME_TRACE`；`GOAL_RESUME_START_TIMEOUT` 语义改为「等桌面端取走排队消息的秒数」，超时只影响状态显示。
- 任务结果新增 `queued_id`、`delivered`、`pending`：`ok` 表示已投递，`delivered` 表示观察到出队，`pending` 表示还在队列里（投递任务仍算完成）。这些字段不确认模型回合已开始。子代理线程投喂失败单独标记，不污染其他目标。
- 状态条不再显示「回合进行中 / 最后活动」，改为「把『继续』排进会话队列，回合由 Codex 桌面端执行」；取消确认明确说明已排队消息不会撤回。
- 测试 364 个（另 33 个页面测试）。

## [0.6.0] - 2026-11-01

### 新增

- **额度重置券**（opt-in）：解析额度接口里的 `rate_limit_reset_credits`，Dashboard / 侧边栏显示可用张数，可一键手动使用（二次确认）；也可开启自动使用，但只在账号池换不到可用账号时兜底，默认阈值是 7 天窗口已用 ≥98% 且 3 天内不会自然重置，每 7 天最多 1 张。所有数值都在设置面板「切号」分组里。
- `POST /api/quota/reset`（幂等键 `redeem_request_id`，省略 `credit_id` 时自动选最早过期的一张）。
- `auto_reset.gate` 实时评估：`GET /api/auto-switch/status` 会给出没动手的具体原因（`disabled` / `below_threshold` / `window_resets_soon` / `no_credit` / `weekly_cap` / `unknown`），查询本身不消耗券。
- 设置面板新增 5 个自动用券项；`start.ps1` 新增 `-EnableAutoReset`。
- **按账号的重置券 UI**：账号池每行显示该账号自己的券数（`券 N`）并给出「用券」按钮（走 `POST /api/quota/reset` 带 `email`），不必先切成当前账号；「自动切号」卡片新增自动用券状态、兜底条件、本周已用次数，以及 gate 原因的中文解释（`disabled` / `below_threshold` / `window_resets_soon` / `no_credit` / `weekly_cap` / `unknown`）。

### 修复

- **续跑刚启动就被中断**：通知缓冲区里上一次会话残留的 `thread/goal/updated(usageLimited)` 会被当成「目标已结束」，导致回合在 31 ms 内 `turn_aborted`。现在进入等待前先丢弃任务开始前的历史通知，终止状态只认 `thread/goal/get`。新增 `CODEX_SWITCHER_GOAL_RESUME_TRACE=1` 打印等待到的通知名与剩余时间。
- **续跑状态看不出在不在动**：任务现在记录 `last_activity_at` / `turn_started_at`，状态条显示「回合进行中 N 分钟 · 最后活动 N 前」，超过 15 分钟没有新事件转为橙色警告；状态条同时给出 8 位线程 id，避免和桌面端打开的、标题相同的原线程混淆。
- **接管桌面端没说明**：续跑需要接管被桌面端占用的会话时，状态条现在直接写「桌面端已被关闭接管，回合在后台 app-server 里跑，任务结束后自动重开」，续跑和停止的确认框也会提前说明；可在设置里关掉这个接管。
- **服务重启后桌面端不会自己回来**：接管信息现在落进任务记录，服务启动时发现上次是「关闭桌面端接管」的未完成任务，会把桌面端重新打开。
- **会话行新增「桌面端占用」标记**：`list_sessions` 用共享冲突探测（`~/.codex/thread-writer-locks/<thread>.lock`，`ERROR_SHARING_VIOLATION`）判断该线程的写入锁当前是否被别人持有，Dashboard 直接标出来；锁是按线程的，桌面端开着 A 不会影响 B。
- **续跑行的紫色标记错位**：原来是 `box-shadow: inset 3px 0 0`，正好压在复选框下面，只剩一条悬空的竖线。改成行自己的 3px 左边框，高度与行一致、不再被内容遮住。
- **置顶会话在项目里消失**：置顶线程现在同时出现在「置顶」和它所属的「项目」里（与 Codex 一致），项目内那一行带「置顶」标记；`shown` 仍按线程去重计数。
- `restart-dev.bat` 在被管道调用时会因为继承 stdout 句柄而挂住；改用 `Start-Process -RedirectStandardOutput/Error`。

### 变更

- **接管桌面端改为默认关闭**（`goal_resume_takeover`，默认 `0`）：续跑拿不到线程锁时直接报「会话被 Codex 占用」，不再默认关你的窗口。改默认值时 `settings.py` 与 `config.py` 两处不一致（面板显示关闭、实际仍在接管），现已统一。
- **切号后自动续跑改为默认关闭**（`auto_resume_after_switch`，默认 `0`）：切号不再自动在后台起任务；要用就在会话行点「自动」并在设置里打开。
- 版本号统一到 0.6.0（服务 `/api/health`、扩展 manifest、扩展侧边栏标题、启动横幅一致）；`config.VERSION` 改为从包 `__version__` 派生，不再单独维护。
- 文档重排：README 只留「这是什么 / 怎么跑 / 常用四件事 / 三个必须知道的坑」，配置表、环境变量、API、已知限制移到 `docs/reference.md`，排障移到 `docs/troubleshooting.md`；版本历史在 `CHANGELOG.md`。
- 测试 289 个。

## [0.5.0] - 2026-10-31

### 新增

- **设置面板**：54 个配置项自动渲染成表单（登录 4 / 接码 21 / 切号 16 / 目标续跑 8 / 高级 5），保存即生效，密钥用 DPAPI 加密后落盘。优先级固定为「设置面板 > 环境变量 > 默认值」，单项可「清除」回落到环境变量。
- **切号开关热生效**：`POST /api/switching/toggle` 与设置面板双向同步，切换自动切号会立即启动/停止监控线程，不再需要重启。
- **账号池拖拽排序**：`POST /api/accounts/reorder` 落盘为 `priority` 100、99、98…，额度相同时按它决定先切谁。
- **目标续跑**（opt-in）：列出 `~/.codex/goals_1.sqlite` 的目标会话，勾选后先 dry-run 再确认续跑；通过独立 `codex app-server` 执行 `thread/resume` → `thread/goal/set active`，回合级监控，有最长运行时间，可随时取消。任务历史写 `goal-resume-history.jsonl`（200 条）。
- **切号后自动续跑**：会话级开关 `POST /api/goals/auto-resume`，每次切号成功（手动或自动）后自动唤醒已勾选的 `usage_limited` / `paused` / 假 `active` 目标。
- **接码面板**：平台余额、占用中的号码、最近验证码、事件日志；支持取号测试与释放退款。
- **手机短信自动接码**（opt-in）：GrizzlySMS / SMSBower / 本地 L、H 服务，检测到手机验证时自动取号、填号、等码；被改道 WhatsApp 会立刻失败并释放号码，不白等。
- **批量导入并行认证**：`login_concurrency` 默认 2（1–4），每个账号独立 Chrome profile + 独立 `CODEX_HOME`。
- **会话浏览**：按置顶 / 项目 / 最近分组，默认只列 `thread_source=user`，有 goal 的会话永远显示，支持搜索。
- 登录记录落盘 `login-history.json`（不含设备码与 CLI 输出），服务重启后仍在。

### 变更

- Dashboard 不再有独立的「登录助手」卡片，登录入口合并进账号行。
- 切号后的「额度耗尽」与「会话被占用」在 UI 上分开提示（`already has an active writer` 不是额度问题）。

## [0.4.0] - 2026-10-30

### 新增

- **额度看板**：当前账号 5 小时 / 7 天已用百分比、重置时间、剩余百分比，`/api/quota` 只读脱敏；`?force=1` 跳过缓存。
- **额度走势与预测**：每次探测按账号记一条采样（不含 token），画最近 6 小时走势并估算耗尽时间，采样保留 30 天。
- **自动切号**（opt-in）：额度用尽（`limit_reached=true` 或 `allowed=false`）时整池并行探测候选，按剩余额度排序选号，走正常备份 / 原子写 / 校验 / 回滚；带账号冷却、每小时上限、连续失败熔断、`no_candidate` 退避。
- **预测切换**（opt-in）：`CODEX_SWITCHER_PREDICTIVE_SWITCH=1` 时可在预测耗尽前提前切换。
- **Chrome 扩展**（MV3）：侧边栏 + 工具栏徽章两种形态合一，徽章显示 5h 剩余百分比并按颜色分档；`chrome.alarms` 每分钟刷新。
- **扩展安装助手**：`/api/extension/install` 把扩展目录写进剪贴板，并只读检测各 Chrome profile 是否已安装。
- **托盘 + 全局快捷键**：`start.ps1 -Tray`，`Ctrl+Alt+C` 打开 Dashboard。
- Dashboard 视觉改版（浅色卡片、统计卡、底部浮动 dock）。

## [0.3.0] - 2026-10-30

### 新增

- **手动切号**（opt-in，`CODEX_SWITCHER_ENABLE_SWITCH=1`）：切换前 DPAPI 加密备份当前 `auth.json` → 当前账号自动快照 → 原子替换 → 重新解析校验 → 失败自动回滚。
- `/api/switch` 要求 `confirm=true` 且目标必须有加密快照；未启用返回 `423 switching_disabled`。
- `/api/snapshots` 只返回公共元数据，不含 token。
- 真实 A→B→A 验收：切回后原 `auth.json` 的 SHA-256 逐字节恢复。

## [0.2.0] - 2026-10-30

### 新增

- **登录助手**（opt-in，`CODEX_SWITCHER_ENABLE_LOGIN=1`）：隔离的临时 `CODEX_HOME` 跑 `codex login --device-auth`，解析 9 位设备码与授权 URL，成功后把 `auth.json` 加密存进账号池，不碰当前 `auth.json`。
- Playwright 自动模式（可选）：系统 Chrome + 每账号独立 profile，自动填邮箱 / 密码 / TOTP / 设备码。
- 账号池：`email-pw-2fa` / `email|password|2fa` 导入、TOTP 生成、DPAPI 加密存储，API 不返回密码与 TOTP。
