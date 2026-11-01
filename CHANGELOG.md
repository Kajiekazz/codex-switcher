# 更新日志

结构参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按 [SemVer](https://semver.org/lang/zh-CN/) 理解（0.x 阶段，本地 API 仍可能调整）。
用法请见 [README](README.md)。

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
- **续跑行的紫色标记错位**：原来是 `box-shadow: inset 3px 0 0`，正好压在复选框下面，只剩一条悬空的竖线。改成行自己的 3px 左边框，高度与行一致、不再被内容遮住。
- **置顶会话在项目里消失**：置顶线程现在同时出现在「置顶」和它所属的「项目」里（与 Codex 一致），项目内那一行带「置顶」标记；`shown` 仍按线程去重计数。
- `restart-dev.bat` 在被管道调用时会因为继承 stdout 句柄而挂住；改用 `Start-Process -RedirectStandardOutput/Error`。

### 变更

- 切号遇到排队的自动续跑时，不再「重启桌面端 → 立刻为拿锁再关掉它」：`SwitchManager` 直接关闭桌面端（`desktop_restart.reason = deferred_to_goal_resume`），续跑在后台跑完后重开；没有目标可续跑时立即重开。`SwitchManager` 新增 `defer_restart` 钩子，`config`/测试同步。
- 版本号统一到 0.6.0（服务 `/api/health`、扩展 manifest、扩展侧边栏标题、启动横幅一致）；`config.VERSION` 改为从包 `__version__` 派生，不再单独维护。
- 文档重排：README 只留「这是什么 / 怎么跑 / 常用四件事 / 三个必须知道的坑」，配置表、环境变量、API、已知限制移到 `docs/reference.md`，排障移到 `docs/troubleshooting.md`；版本历史在 `CHANGELOG.md`。
- 测试 293 个。

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
