# Codex Switcher

本地 **Codex 多账号助手**：本地服务管账号、加密快照与切号，Chrome 扩展（侧边栏 + 工具栏徽章）做浏览器侧面板。当前是 **v0.5 体验版**。

> **v0.4 的安全边界**
> - 切号默认 **禁用**；必须显式设置 `CODEX_SWITCHER_ENABLE_SWITCH=1`（或 `start.ps1 -EnableSwitch`）才会执行。
> - 自动切号默认 **禁用**；必须同时设置 `CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1` 和 `CODEX_SWITCHER_ENABLE_SWITCH=1`，并且只在额度接口明确返回 `limit_reached=true` 时触发。
> - 自动切号的候选账号必须通过只读额度探测；未知、失效或同样用尽的账号会被跳过。带账号冷却、每小时切换上限和连续失败熔断。
> - `/api/switch` 需要 `confirm=true`，并且只接受账号池里已有加密快照的账号。
> - 切换前强制 DPAPI 加密备份当前 `~/.codex/auth.json`；写入使用原子替换；写入后重新解析校验；失败自动回滚。
> - 当前账号在切换前会自动快照，因此切出去后仍可切回。
> - 登录助手默认 **禁用**；必须显式设置 `CODEX_SWITCHER_ENABLE_LOGIN=1` 才会启动。
> - 登录助手使用隔离的临时 `CODEX_HOME`，新 token 只保存为 DPAPI 加密快照，不直接覆盖当前 `auth.json`。
> - 额度探测只读：token 只用于请求头，不返回、不写日志、不落盘；API 只返回套餐、百分比和重置时间。
> - 密码 / TOTP / token 使用 Windows DPAPI 加密存储，API 不返回明文。
> - 服务只监听 `127.0.0.1`。

v0.3 已做真实 A→B→A 切换验收：原 auth 的 SHA-256 在切回后逐字节恢复；2026-10-30 再次真实 A→B，备份/原子写/校验通过。v0.4 已用真实账号验证额度接口（`plan=plus`、5h 100%、7d 32%、`limit_reached=true`），并完成自动切号策略/托盘/扩展的本地验证；自动切号本身保持 opt-in，未在当前额度用尽的账号上强制触发。切换仍会改动 `~/.codex/auth.json`，请先确认没有不能中断的会话。

---

## 当前能做什么

- 读取并展示当前 Codex 账号（邮箱、套餐、account id、过期时间）。
- **v0.3+ 切号**：选择账号池中的加密快照 → 二次确认 → 备份当前 auth → 原子替换 → 校验 → 失败回滚。
- 当前账号在切换前自动快照；切出后仍可切回。
- 导入账号到本地加密账号池：
  - `email-----password---2fa`
  - `email|password|2fa`
  - 2FA 支持 raw base32 或 `otpauth://` 链接。
- **账号管理**：账号池支持单个添加、编辑（标签 / 备注 / 优先级 / 停用 / 轮换密码与 2FA）、删除；删除只作用于账号池、加密快照和额度采样，不改 `~/.codex/auth.json`。优先级越大越先被自动切号选中（**额度相同的情况下**；有额度的号永远排在没额度的号前面），停用账号保留凭据但跳过候选。Dashboard 和面板会显示 OIDC `name` 用户名（旧快照启动时自动回填）。
- **账号池额度**：Dashboard 账号池每一行都直接显示「5 小时已用 / 7 天已用 / 套餐 / 更新时间」，用各账号自己的加密快照 token 并行查询实时接口，不需要先切成当前账号；账号池标题栏的「刷新额度」跳过缓存立即重查。默认缓存 60 秒（设置面板可改 `pool_quota_ttl`），避免轮询打爆额度接口；没有快照或 token 过期的账号会在该行显示不可用原因，不影响其它账号。
- 本地 Dashboard：<http://127.0.0.1:8787/>
- **v0.4 额度/健康状态**：Dashboard 和浏览器面板显示当前账号 5 小时/7 天额度百分比、重置时间；百分比明确标注为「已用」，统计卡同时给出「剩余 X%」。额度缓存默认 5 秒，与 Dashboard 的 5 秒轮询对齐，并显示「更新于 N 秒前」。`/api/quota` 只读返回脱敏字段。
- **额度走势与预测**：每次额度探测会按账号记录一条采样（百分比与重置时间，不含 token）；Dashboard 画最近 6 小时走势并估算按当前速度何时用尽。采样只存在本地 SQLite，保留 30 天。
- **预测切换（opt-in）**：设置 `CODEX_SWITCHER_PREDICTIVE_SWITCH=1` 后，自动切号可在预测耗尽前 `CODEX_SWITCHER_PREDICTIVE_THRESHOLD` 秒（默认 600）提前切换；不设置时仍然只在 `limit_reached=true` 时切换。
- **v0.4 自动切号（opt-in）**：额度用尽时从账号池快照中探测候选账号，确认候选可用后走正常备份/原子写/校验/回滚流程；带账号冷却、每小时上限和失败熔断。
- **额度重置券（opt-in）**：额度接口里的 `rate_limit_reset_credits`（「Full reset (Weekly + 5 hr)」）会显示在 Dashboard 和侧边栏，可一键手动使用（二次确认）；也可开启自动使用——只在**换不到可用账号**、7 天窗口已用 ≥98% 且 3 天内不会自然重置时兜底消耗一张，每 7 天上限 1 张。默认关闭，数值全部可在设置面板改。
- **切号开关与顺序（v0.5）**：「切换账号」卡片顶部两个开关与设置面板双向同步，`enable_auto_switch` 保存即启动/停止监控线程（无需重启）；账号池支持拖拽排序（→ `priority` 100、99、98…），卡片同时展示自动切号的候选顺序。
- **目标续跑（opt-in，v0.5）**：Dashboard 列出 `~/.codex/goals_1.sqlite` 里的目标会话（带 thread 名称/目标/累计 token/时长），可勾选 `usage_limited` 会话后先 dry-run 预览、再确认续跑。续跑通过独立 `codex app-server` 执行 `thread/resume` → `thread/goal/set active` → 等待自动续跑回合（无动静时发送「继续」），不直接写 goals SQLite；`paused` 目标不会被自动动。一次只跑一个任务、串行处理、有最长运行时间，可随时取消。每个会话显示上次续跑结果徽标，任务历史落盘 `goal-resume-history.jsonl`（200 条），勾选状态存在 `localStorage`。
- **接码面板（v0.5）**：Dashboard 新增「接码」卡片，显示平台余额、占用中的号码、最近验证码与事件日志（本机不脱敏），支持取号测试与释放退款。
- **v0.4 托盘 + 全局快捷键**：`start.ps1 -Tray` 启动后台服务并打开托盘；`Ctrl+Alt+C` 打开 Dashboard。
- **v0.5 浏览器侧面板 = Chrome 扩展**：`extension/` 是 Manifest V3 扩展，两种形态合一——
  - **侧边栏**（`chrome.sidePanel`）：常驻右侧，可拖宽/收起，不遮挡 chatgpt.com 正文；显示服务状态、当前账号、5h/7d 额度条与重置时间、账号池数量、切号与批量导入入口。
  - **工具栏徽章**：图标上直接显示当前账号 5h **剩余**百分比（`34%`），颜色分档（≥50% 绿 / 20–49% 琥珀 / <20% 红），悬停看邮箱、套餐与 7d 剩余；额度归零时可选系统通知。
  - 每 1 分钟由 `chrome.alarms` 唤醒 service worker 刷新徽章；侧边栏自己直连 `127.0.0.1:8787`，worker 休眠也能用。
  - **安装助手**：Dashboard 的「浏览器扩展」卡片一键把扩展目录（或 `chrome://extensions` 网址）复制到剪贴板，并列出扩展 ID、Chrome 路径、各 profile 里是否已装（装完点「重新检测」即变「已安装」）。Chrome 会丢弃命令行传入的 chrome:// 网址，所以「替你打开扩展页」在 156 上做不到——卡片只复制，不假装打开。
- **v0.4 UI**：Dashboard 参考 `workbuddy-manager` 的视觉语言（`#fafafa` 背景、`#f4f4f5` 20px 圆角卡片、5 张统计卡、额度/当前账号/自动切号三栏、账号池、底部浮动 dock）；扩展侧边栏/徽章同步浅色卡片主题。
- **登录助手（v0.2+）**：
  - 手动模式（默认）：Dashboard 显示设备码和授权链接，你在浏览器完成登录/同意，CLI 在隔离目录里写 `auth.json`。
  - Playwright 自动模式（可选）：有头系统 Chrome，专用 profile，自动完成邮箱/密码/TOTP/账户选择/同意/设备码输入。
  - 成功后将 `auth.json` 加密保存到账号池，供切号使用。
  - 不替换当前 `~/.codex/auth.json`，不影响正在跑的会话。

## 快速开始

```powershell
cd codex-switcher          # 仓库根目录（克隆后就是这个文件夹）
# 只启用切号：
.\start.ps1 -EnableSwitch
# 或者：切号 + 自动切号 + 托盘（先确保服务未运行；已运行先 .\stop.ps1）
.\start.ps1 -EnableSwitch -EnableAutoSwitch -Tray
# 或：$env:CODEX_SWITCHER_ENABLE_SWITCH="1"; python service.py
```

停止服务：

```powershell
.\stop.ps1
```

看到：

```text
Starting Codex Switcher v0.4...
Switching: ENABLED / backup + atomic write + verify + rollback
Auto switch: DISABLED (use -EnableAutoSwitch to opt in)
Login assistant: DISABLED (use -EnableLogin to opt in)
```

然后打开 <http://127.0.0.1:8787/>。Dashboard 的「切号」卡片会列出可切换快照；勾选确认后才会执行。额度卡片会显示当前账号的 5h/7d 用量。

### 安装 Chrome 扩展（浏览器侧面板）

最省事的方式：打开 Dashboard → 「浏览器扩展」卡片 → 点 **复制安装路径**。

> ⚠️ 没有任何程序能替你打开 `chrome://extensions`：Chrome 会**丢弃**命令行传进来的
> `chrome://` 网址（`chrome://extensions`、`chrome://settings`、`chrome://version` 都只开一个
> 空白标签页，`--app=` / `--new-window` / 转发到已运行实例同样无效，实测 Chrome 156.0.8078.76）。
> 卡片上另有「复制扩展页网址」按钮，方便你粘到地址栏。

1. 点卡片上的「复制安装路径」→ 目录路径进剪贴板；
2. 点地址栏右侧的**拼图图标 🧩** →「管理扩展程序」（不用手打网址；也可以粘 `chrome://extensions`）；
3. 打开右上角「开发者模式」→ 点「加载已解压的扩展程序」；
4. 在对话框里 `Ctrl+V` 粘贴路径 → 回车 →「选择文件夹」；
5. 回来点「重新检测」，卡片上的「未安装」变成「已安装」。

手动安装（等价）：`chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 → 选 `extension/` 目录 →
点地址栏右侧拼图图标把它钉住 → 点图标打开侧边栏。

> 为什么不做静默安装：Chrome 只允许 Web Store 和「企业策略」两条通路，后者会让浏览器显示
> 「由组织管理」并锁住扩展，所以这里不碰注册表。
> 扩展 ID 由目录路径派生（本项目固定为 `ccpnehmimijbfhcjonkbgbimhjpohfkj`），路径不变就不会变。

工具栏图标上的数字就是**当前账号 5h 的剩余额度**：

| 徽章 | 含义 |
|---|---|
| `62%` 绿 | 5h 还剩一半以上，不用管 |
| `34%` 琥珀 | 5h 剩 20–49%，留意一下 |
| `8%` 红 | 5h 剩不到 20%，准备切号 |
| `0%` 红 + 通知 | 额度用尽，点图标进侧边栏切到有额度的号 |
| `–` 灰 | `~/.codex/auth.json` 不存在（没登录） |
| `!` 灰 | 本地服务没起来（先跑 `python service.py`） |
| `?` 灰 | 服务在线但额度读取失败，悬停看原因 |

> 扩展只访问 `127.0.0.1:8787`，不需要账号密码；侧边栏里的切号仍受本地服务 env 开关约束，
> 并且必须勾选确认 + 二次弹窗。`extension/` 是面板的唯一源文件，改完直接点扩展页的刷新按钮即可。

### 托盘与全局快捷键

```powershell
.\start.ps1 -EnableSwitch -Tray
```

- 服务已在运行时直接打开托盘；未运行则先后台启动服务，等 `/api/health` 就绪后打开。
- 托盘菜单：打开 Dashboard、复制当前邮箱、刷新状态、启动/停止服务、退出。
- 全局快捷键 `Ctrl+Alt+C` 打开 Dashboard。
- 托盘每 10 秒刷新一次账号/额度 tooltip；额度请求失败不会影响托盘本身。

## 使用切号（v0.3+）

1. 先导入至少两个账号，并让它们各自拥有加密 `auth.json` 快照：
   - 当前账号：服务启动后、第一次切号前会自动快照；
   - 其它账号：通过登录助手成功登录后保存快照。
2. 打开 Dashboard，在「切号」卡片选择目标账号。
3. 勾选「确认切换并接受自动回滚」，再点「切换账号」。Dashboard 和扩展侧边栏都会要求二次确认。
4. 服务执行：
   - 读取当前 auth，写入 DPAPI 加密备份到 `%LOCALAPPDATA%\CodexSwitcher\backups`；
   - 自动快照当前账号；
   - 原子替换 `~/.codex/auth.json`；
   - 重新解析并校验目标账号；
   - 失败则用备份回滚，并返回错误。
5. 成功返回 `previous_email`、`target_email`、`backup_path`；不会返回任何 token。

> 切换后如果 Codex 桌面端仍显示旧账号，需**完全退出**桌面端/CLI（包括托盘、后台 `codex` 子进程），再重启；只关窗口不够。桌面端在启动时读取 `auth.json`，运行中不会热加载。验证：左下角头像 → 个人资料菜单，应显示目标账号（2026-10-30 已用 07 的 `Robert Johnson / Plus` 实测）。

### 真实验收记录

2026-10-30 在本机执行 A→B→A：

- 原始/切回 A 的 `auth.json` SHA-256：`e74490d52ec6fb3916342e1ef90ededfbdddf85bd01a744244dbe9dcb74f9f0a`
- 切到 B 后的 SHA-256：`5ca3a359d1705efad07e1e3efa307c1b3e0b1da3462ca9109a834753adb4ffc4`
- 切回 A 后哈希逐字节恢复；服务状态显示 A active、B inactive。

2026-10-30 再次真实切到新购账号（邮箱已脱敏）：

- 切换前 A 的 `auth.json` SHA-256：`e74490d52ec6fb3916342e1ef90ededfbdddf85bd01a744244dbe9dcb74f9f0a`
- 切换后新号 SHA-256：`bc03f94762b8ebb5bfe361e5b36d3afaff52070623d5c924ee183485d2606bae`
- 目标 account id：`cb6efe70-c374-40fb-939f-a79f4358cfb5`，plan `plus`，切换时未过期
- 切换前备份：`%LOCALAPPDATA%\CodexSwitcher\backups\auth-20261030-173959-152068-e74490d52ec6.bak.json`
- 桌面端重启后，`.codex-global-state.json` 的 `local-document-identity.accountId` 变为 `cb6efe70-c374-40fb-939f-a79f4358cfb5`

## 自动切号（v0.4，opt-in）

默认禁用。自动切号同时要求两个开关，缺一不可；修改 env 后必须先 `.\stop.ps1` 再启动，已运行的服务不会热更新：

```powershell
cd codex-switcher          # 仓库根目录（克隆后就是这个文件夹）
.\start.ps1 -EnableSwitch -EnableAutoSwitch
```

等价环境变量：

```powershell
$env:CODEX_SWITCHER_ENABLE_SWITCH = "1"
$env:CODEX_SWITCHER_ENABLE_AUTO_SWITCH = "1"
python service.py
```

触发逻辑：

1. 每 90 秒读取当前账号的只读额度接口（`/backend-api/wham/usage`）。
2. 只有 `limit_reached=true` 或 `allowed=false` 才进入候选选择；额度正常时不做任何切换。
3. 候选 = 账号池里有加密 auth 快照、不是当前账号、不在冷却期的账号。**默认整池探测**（不设上限；`CODEX_SWITCHER_AUTO_SWITCH_MAX_PROBES` 可设 0–500 收紧），探测**并行**执行（6 并发）并复用账号池额度缓存（`CODEX_SWITCHER_POOL_QUOTA_TTL`），所以 40 个号的一轮探测也只有几秒。候选必须自身额度可用（`allowed=true` 且未用尽）。
4. **选号是「额度优先」**：把探测通过的账号按剩余额度排序，5h 窗口剩余最多的排最前，其次比 7d 窗口，最后才用 `priority` 和保存时间决胜。所以高优先级但快用完的号不会挤掉刚重置的新号；优先级只在额度相同时起作用。
5. 选中的账号走正常切号路径：加密备份当前 auth → 原子替换 → 校验 → 失败自动回滚。
6. 用尽/被拒的候选账号会冷却到它自己的 5h 窗口重置时间（至少 15 分钟），不再每轮空探测。
7. 被切走的账号同样冷却到它的 5h 额度重置时间，防止马上切回去。全池都没额度时进入 `no_candidate`：如果最近的额度重置在 15 分钟内，就等到重置后 5 秒再试，否则每 5 分钟重试。
8. 每小时最多 3 次自动切换；连续 3 次切换失败后熔断 30 分钟。

Dashboard「切换账号」卡片下方的提示行就是这套顺序的可视化：只列出「有额度、且不是当前账号」的号，按剩余 5h/7d 额度从多到少排，用尽/停用/冷却中的号直接不出现；如果全都没额度，会明确写「现在没有有额度的账号」。

可在 Dashboard「自动切号」卡片或 `GET /api/auto-switch/status` 查看状态。审计日志在 `%LOCALAPPDATA%\CodexSwitcher\auto-switch.log`，只记录事件和邮箱，不记录 token。

可调环境变量（一般不用改）：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `CODEX_SWITCHER_AUTO_SWITCH_INTERVAL` | 90 | 检查间隔秒数（15–3600） |
| `CODEX_SWITCHER_AUTO_SWITCH_COOLDOWN` | 900 | 账号冷却秒数（60–86400） |
| `CODEX_SWITCHER_AUTO_SWITCH_MAX_PER_HOUR` | 3 | 每小时最多自动切换次数（1–24） |
| `CODEX_SWITCHER_AUTO_SWITCH_MAX_PROBES` | 0 | 每轮最多探测几个账号，`0` = 整池（0–500） |
| `CODEX_SWITCHER_AUTO_SWITCH_FAILURE_THRESHOLD` | 3 | 连续失败多少次后熔断（1–20） |
| `CODEX_SWITCHER_QUOTA_CACHE_TTL` | 30 | 额度缓存秒数（0–600） |
| `CODEX_SWITCHER_USAGE_URL` | `https://chatgpt.com/backend-api/wham/usage` | 额度接口地址（测试/接口变更时用） |

> 自动切号只切换本地 `auth.json`，不会购买额度、不会修改套餐/账单，也不会替你重启桌面端。切换后如果桌面端仍显示旧账号，重启桌面端。
>
> 选号不会「有额度的不用、偏用没额度的」：优先级只是额度相同时的次序。想让某个号优先被用，正确做法是把它留在池里并保证它有额度，而不是靠调高 `priority` 去压过一个满额度的号。
>
> 当前实现只把「额度探测通过」的账号当作候选。快照里的 access token 已过期时，额度探测会返回 401，该账号会被跳过；重新用登录助手登录一次即可恢复候选资格。

## 额度重置券（opt-in）

Codex 账号偶尔会收到 **「Full reset (Weekly + 5 hr)」** 重置券：用掉一张，5 小时窗口和 7 天窗口同时清零。
每张券一次性、不可撤回，通常一个月后过期，所以默认 **不自动使用**。

额度接口本来就带 `rate_limit_reset_credits`（`available_count` / `applicable_available_count`），服务会一并解析：
Dashboard 额度卡片显示「重置券 可用 N 张」并在有券时出现「使用重置券」按钮（要求切号功能已启用）；
侧边栏提示行也会带上「· 重置券 N 张」。手动使用需要二次确认，失败会回显上游原因。

上游接口（token 只出现在请求头，不写日志、不落盘）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/backend-api/wham/rate-limit-reset-credits` | 列出券：`id` / `status` / `reset_type` / `expires_at` |
| `POST` | `/backend-api/wham/rate-limit-reset-credits/consume` | 请求体 `{"credit_id","redeem_request_id"}`，成功返回 `{"code":"reset","windows_reset":2}` |

`redeem_request_id` 是幂等键（UUID）：重放同一个 id 不会二次扣券。上游失败码：`nothing_to_reset`（当前用量不需要重置）、`no_credit`、`already_redeemed`。

自动使用（默认 **关闭**）的判定是「换号优先，券只兜底」，必须同时满足：

1. 当前账号 `limit_reached=true`（5 小时窗口已耗尽）；
2. 账号池里**没有**可用候选（`no_candidate`）——还有别的号能换就先换号，别烧券；
3. 7 天窗口已用 ≥ `auto_reset_secondary_threshold`（默认 **98%**）；
4. 7 天窗口**不会**在 `auto_reset_min_wait_hours` 小时内自然重置（默认 **72 小时 / 3 天**）——等得及就等；
5. 账号还有 `applicable_available_count ≥ 1` 张券，且最近 7 天自动用券次数 < `auto_reset_max_per_week`（默认 **1**）。

| 变量 | 默认 | 说明 |
|---|---:|---|
| `CODEX_SWITCHER_ENABLE_AUTO_RESET` | `0` | 自动用券总开关 |
| `CODEX_SWITCHER_AUTO_RESET_SECONDARY_THRESHOLD` | 98 | 7 天窗口触发阈值（%） |
| `CODEX_SWITCHER_AUTO_RESET_MIN_WAIT_HOURS` | 72 | 7 天窗口剩余多少小时内会自然重置就不动用券 |
| `CODEX_SWITCHER_AUTO_RESET_MAX_PER_WEEK` | 1 | 每 7 天最多自动用几张 |
| `CODEX_SWITCHER_AUTO_RESET_BEFORE_SWITCH` | `0` | 先烧券再换号（不推荐） |

这些项都在设置面板「切号」分组里，保存即生效（监控线程每次 tick 重新读取）。
启动脚本对应 `.\start.ps1 -EnableSwitch -EnableAutoSwitch -EnableAutoReset`（自动用券还要求切号已启用）。触发后写 `auto-switch.log` 的
`reset_consumed` / `reset_failed` 事件；`GET /api/auto-switch/status` 的 `auto_reset.gate` 会给出没动手的原因
（`below_threshold` / `window_resets_soon` / `no_credit` / `unknown` / `weekly_cap`）。

真实使用记录（2026-11-01，某个 7 天窗口已打满的账号）：7 天已用 100%、5 小时 22% 但 `limit_reached=true`；消耗 1 张券后
`primary 0% / secondary 0%`、`limit_reached=false`、`windows_reset=2`，账号当场恢复可用。

## 目标续跑（opt-in，v0.5）

Codex 的目标（goal）在额度用尽后会进入 `usage_limited` 状态，等额度恢复后需要重新激活。
本功能把「选哪个会话续跑」交给你，而不是无脑全部拉起。

```powershell
# 先停掉旧服务
.\stop.ps1
# 切号 + 自动切号 + 目标续跑
.\start.ps1 -EnableSwitch -EnableAutoSwitch -EnableGoalResume
# 或者只开目标续跑
$env:CODEX_SWITCHER_ENABLE_GOAL_RESUME="1"; python service.py
```

Dashboard 的「会话」卡片把 `state_5.sqlite` 的线程按 Codex 的风格分三块展示：

- **置顶**：`is_pinned=1`，或线程所在 section 名为 `Pinned` / `置顶`；
- **项目**：按 `project_roots` 最长前缀匹配线程 `cwd`（`threads.project_id` 在真实库里全是 NULL，所以只用 `cwd`）；
- **最近**：其余线程。

默认只列 `thread_source=user` 的会话，子代理/guardian_review 默认折叠（卡片底部给出隐藏数量，勾选「子代理」全部展开）；**有 goal 的会话永远显示**，并标注目标状态与累计 token。每组先显示 6 条，「显示更多（还有 N 条）」逐组展开；顶部搜索框按 标题/预览/项目/路径/目标/thread_id 前端过滤，命中结果单列一节。

续跑流程：

1. 勾选可续跑的会话：`usage_limited`（额度用尽）、`paused`（已停止）、以及**没有回合在跑**的 `active`
   （切号重启桌面后最常见：目标卡在 `active`，实际没有任何 agent 在干活）。`complete` / `blocked` 不可选；
2. 点「预览（dry-run）」只调用 `thread/goal/get` 验证可续跑，**不改任何状态**；
3. 点「续跑选中」并确认后，服务会新起一个 `codex app-server` 子进程：
   `thread/resume` → `thread/goal/set`（`status=active`, `origin=automatic`）→ 等待 `turn/started`；
   若 `CODEX_SWITCHER_GOAL_RESUME_START_TIMEOUT`（默认 12s）内没有自动回合，则发送「继续」启动；
   等回合之前会先丢掉通知缓冲区里**本次任务之前**残留的 `thread/goal/updated`（否则会把旧的
   `usage_limited` 当成「目标已结束」，回合刚起就被中断），目标状态一律以 `thread/goal/get` 为准；
4. 每个回合结束后重新读取 goal 状态：变为 `complete` / `paused` / `blocked` / 再次 `usage_limited` 就停止；
   达到 `CODEX_SWITCHER_GOAL_RESUME_MAX_RUNTIME`（默认 7200s）会停下并保留 `active`。
5. 运行中顶部常显「● 正在续跑：会话名 · 任务 id · 已运行时长」，行上打「正在续跑 / 等待续跑」徽标，右上「停止续跑」随时可点（`POST /api/goals/cancel`）；任务日志默认折叠，运行时自动展开。

排障：设置 `CODEX_SWITCHER_GOAL_RESUME_TRACE=1` 后，任务事件里会多出每次等待到的通知名与剩余时间。

会话行会显示**上次续跑**徽标（预览 / 已续跑 · N 回合 / 已启动 · 进行中 / 续跑未完成 · 短原因），完整错误在鼠标悬停的 `title` 里，不会把行撑开。卡片底部列出最近 5 次任务。
每次任务结束（含 dry-run）都会追加一行到 `%LOCALAPPDATA%\CodexSwitcher\goal-resume-history.jsonl`（保留 200 条，原子写入），重启服务后徽标仍在；勾选的会话也存在浏览器 `localStorage`，Ctrl+F5 后仍保留。

### 切号后自动续跑（每个目标单独 opt-in）

会话行上的「自动」按钮把该 `thread_id` 存进服务端 `settings.auto_resume_threads`（`POST /api/goals/auto-resume`）。之后**每次切号成功**（手动或自动切号都算），`SwitchManager.on_switched` 会调用 `GoalResumeService.on_account_switched`：

- 只续跑 `usage_limited` / `paused` / `active` 的目标（`active` 用来唤醒「假 active」）；`complete` / `blocked` 跳过；
- 已经有续跑任务在跑时跳过（不叠加）；
- 没有勾选任何目标时什么也不做；
- 这个路径不需要再点一次 `confirm`：勾选「自动」本身就是逐目标的预先确认；
- 全局开关是设置里的「切号后自动续跑已勾选目标」（`auto_resume_after_switch`，默认开）。

### 「额度耗尽 / 续跑失败」的两种真实原因

- `thread/resume: ... already has an active writer`：Codex 桌面端（或另一个 app-server）正持有该线程，**不是额度问题**。行上显示「续跑未完成 · 会话被 Codex 占用」，卡片顶部出现「上次续跑被 Codex 占用（N 个会话），不是额度问题：重启桌面并重试」，点它会先重启桌面端再续跑（`restart_desktop: true`）。
- 真的额度受限：当前 `auth.json` 账号的 5h/7d 窗口已满，续跑会立刻再进 `usage_limited`，先切号或等窗口重置。

要点：

- 只操作 `goals_1.sqlite` 对应的 app-server 状态，**从不直接写 SQLite**；app-server 读取的是实时 `auth.json`，所以切号后新任务会用新账号。
- 一次只有一个续跑任务；勾选的会话串行执行。
- 真实续跑需要 `confirm=true`，会消耗当前账号额度；dry-run 不消耗、不写入。

## 启用登录助手（v0.2+）

默认禁用。先停掉当前服务，再用新的 PowerShell 窗口启动：

```powershell
cd codex-switcher          # 仓库根目录（克隆后就是这个文件夹）
.\start.ps1 -EnableLogin -Driver manual    # 自动模式用 -Driver playwright
```

等价的环境变量方式：

```powershell
$env:CODEX_SWITCHER_ENABLE_LOGIN = "1"
$env:CODEX_SWITCHER_LOGIN_DRIVER = "manual"
python service.py
```

Dashboard 不再有单独的「登录助手」卡片：每个账号行右侧有「登录 / 重新登录 / 查看登录」按钮，点开弹窗显示设备码、URL、进度和取消。导入新凭证时如果开启了自动登录（`playwright` 模式默认开启），会自动排队取 auth。

### 手动模式（默认，推荐先跑通）

1. 服务端启动 `codex login --device-auth`，`CODEX_HOME` 指向 `%LOCALAPPDATA%\CodexSwitcher\login-sessions\<id>`。
2. Dashboard 显示 9 位设备码，例如 `PBAH-TBU2Y`。
3. 你在浏览器完成登录、账户选择、同意、输入设备码。
4. CLI 把 `auth.json` 写到临时目录，服务读取后加密保存快照，然后结束 CLI。
5. 当前 `~/.codex/auth.json` 完全没变。

### Playwright 自动模式（可选）

```powershell
$env:CODEX_SWITCHER_ENABLE_LOGIN = "1"
$env:CODEX_SWITCHER_LOGIN_DRIVER = "playwright"
python service.py
```

自动模式的行为：

- 用系统 Chrome（`channel="chrome"`）+ 每个账号独立的专用 profile，不碰你日常 Chrome profile。
- 自动填邮箱、密码、TOTP，选择目标账户，点击同意，填写设备码。
- 页面必须保持 **前台可见**：`document.visibilityState=hidden` 时 Sentinel token 会卡住，页面会显示「糟糕，出错了！」。驱动会把窗口拉到前台重试；如果窗口被遮挡，可能仍失败，这时改用手动模式。
- 新账号默认没开「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」。同意页出现该提示时，驱动会自己打开 <https://chatgpt.com/settings/security>、找到 `role="switch"` 的「设备代码登录」开关并打开，然后重新进入设备流程（`driver.log` 记 `device_code_setting_enabled`）。只有自动打开失败时才报 `device_code_login_disabled`，让你手动处理。
- 设备码逐格填写并回读校验（React 会在输入时重渲染，单次 `fill` 可能吞掉一个字符导致「继续」禁用）；只有页面出现「已登录 Codex / 你现在可以关闭此页面」才判定授权完成。
- 失败时会在 `login-sessions/<id>/failure.png` 留截图，`cli.log` 留 CLI 输出，`driver.log` 留驱动每一步的 URL/页面摘要（查询串已脱敏）；API 只返回本地可见的 `log_tail`。

> 自动模式依赖 ChatGPT 页面结构，页面改版后选择器可能需要调整。手动模式永远可用。

### 批量导入的并行认证（v0.5+）

批量导入时会按 `login_concurrency`（默认 **2**，范围 1–4）并行认证：每个账号一个独立 Chrome 窗口 + 独立 `CODEX_HOME`，同时最多跑 N 个，其余按导入顺序排队。

| 变量 | 说明 |
| --- | --- |
| `CODEX_SWITCHER_LOGIN_CONCURRENCY` | 同时登录的账号数，默认 `2`，1–4；也可以在设置页「同时登录几个账号」里改（立即生效，无需重启） |

- 实测两个账号的 `started_at` 相差 3 ms，两个 `codex login --device-auth` 进程和两个 Playwright profile 同时在跑。
- 每个登录都会把 Chrome 抢到前台，所以 `>2` 容易互相抢焦点、Cloudflare/Sentinel 失败率上升；手动模式（`driver=manual`）下每个账号都需要人工操作，建议设成 1。
- 队列与并发数会通过 `GET /api/login/status` 的 `concurrency` 字段、`GET /api/accounts` 的 `login_concurrency`、导入响应里的 `login_concurrency` 暴露给 Dashboard，弹窗里会显示「有空位时自动开始（同时最多 N 个 Chrome 窗口）」。

### 登录记录落盘

登录记录在内存里之外还会写一份到 `%LOCALAPPDATA%\CodexSwitcher\login-history.json`（原子写，保留最近 50 条），服务重启后历史仍在：

- 记录字段：`id / email / email_masked / status / message / driver / error / started_at / updated_at / codex_home`。
- **不写** `device_code`（一次性设备码）和 `log_tail`（CLI 输出），避免敏感信息落盘。
- 服务重启时，磁盘上仍处于 `starting / waiting_for_browser / waiting_for_cli` 的记录会被标成 `failed` + `error="interrupted"`（进程和浏览器窗口已随服务一起没了），不会被自动重跑。
- Dashboard 的登录弹窗底部会列出该邮箱最近 5 条记录（状态 + 时间 + 错误码）。

### 手机短信验证自动接码（可选，v0.5+）

Codex OAuth 在部分账号上会要求手机号验证。驱动检测到 `/phone-verification` 或「verify your phone / 手机号」页面时，会自动取号、填号、等短信、填验证码，成功后才算完成授权。

接码平台与 `chatgpt-autoregister` 完全一致（环境变量名也兼容）：

| 变量 | 说明 |
| --- | --- |
| `CODEX_SWITCHER_SMS_PROVIDER` 或 `SMS_PROVIDER` | `grizzly` / `smsbower` / `l` / `h`；不设置 = 接码关闭 |
| `CODEX_SWITCHER_SMS_SERVICE` 或 `SMS_SERVICE` | 服务代码，默认 `dr`（OpenAI/ChatGPT） |
| `CODEX_SWITCHER_SMS_COUNTRY` 或 `SMS_COUNTRY` | 国家代码，默认 `10` |
| `CODEX_SWITCHER_SMS_MAX_PRICE` 或 `SMS_MAX_PRICE` | 单号最高价，留空不限 |
| `CODEX_SWITCHER_SMS_CODE_WAIT` / `SMS_CODE_WAIT` | 等短信秒数，默认 120 |
| `CODEX_SWITCHER_SMS_POLL_INTERVAL` / `SMS_POLL_INTERVAL` | 轮询间隔秒，默认 5 |
| `CODEX_SWITCHER_SMS_MAX_RETRIES` / `SMS_MAX_RETRIES` | 最多换几个号，默认 10 |
| `CODEX_SWITCHER_SMS_API_BASE` / `SMS_API_BASE` + `..._API_KEY` / `SMS_API_KEY` | GrizzlySMS handler_api |
| `CODEX_SWITCHER_SMSBOWER_API_BASE` / `SMSBOWER_API_BASE` + `..._API_KEY` / `SMSBOWER_API_KEY` | SMSBower handler_api（`SMSBOWER_USE_V2=1` 用 getNumberV2） |
| `CODEX_SWITCHER_L_API_BASE` / `L_API_BASE` + `..._ADMIN_AUTH_CODE` / `L_ADMIN_AUTH_CODE` | 本地 L 取号服务，默认 `http://localhost:8788` |
| `CODEX_SWITCHER_H_API_BASE` / `H_API_BASE` + `..._ADMIN_AUTH_CODE` / `H_ADMIN_AUTH_CODE` | 本地 H 取号服务；`H_PHONE_ACQUIRE_MODE=new` 用一次性号 |

行为约定：

- 取号后如果手机号被拒或等码超时，会 `cancel` 释放，避免白扣钱，再换下一个号。
- 拿到验证码并提交成功后 `complete` 正式结束激活。
- 轮询期间取消登录会立即停止等待并释放号码。
- 未配置接码平台时，遇到手机验证会直接报 `sms_not_configured`，不会静默卡住。
- OpenAI 会把大部分虚拟号静默改成 WhatsApp 发码（`无法向该电话号码发送短信…已切换为 WhatsApp`，或验证码页写着「通过 WhatsApp 发送到…」）。驱动会**立刻**判失败并释放号码，不会白等 120 秒；连续 2 次被改道会直接报 `sms_number_rejected`，提示换国家/平台。
- 实测：越南（10，$0.86/号）和英国（16，$0.045/号）都被改成 WhatsApp；**美国（187，$0.13/号）能真实收到短信**，完成手机验证。
- 密钥优先读设置面板保存的值（DPAPI 加密），其次环境变量；`/api/*` 只返回 `****后四位`。

Dashboard 的「接码」卡片是这块的观测台（本地只读为主）：

- 顶部显示平台、余额（Grizzly/SMSBower `getBalance`，缓存 30s，右侧「取号测试」可强制刷新）、服务/国家/最高价/等码与轮询参数；
- 中间列出**当前占用中的号码**（完整号码 + 激活 id + 已等待秒数），每行可单独「释放」；「取号测试」会取一个号再立即释放退款（Grizzly 前 2 分钟不能取消，会自动延后）；
- 「最近验证码」和事件日志**不做脱敏**（本机仪表盘，排查时需要直接看到验证码/号码）。平台 API key 仍然只返回 `****后四位`。

> 关键：**国家代码选 187（美国）** 才走短信；号码被拒的激活在 `cancel` 后会退款。


```powershell
$env:CODEX_SWITCHER_SMS_PROVIDER = "l"
$env:CODEX_SWITCHER_L_API_BASE = "http://localhost:8788"
$env:CODEX_SWITCHER_L_ADMIN_AUTH_CODE = "你的后台授权码"
```

> 也可以直接把 `chatgpt-autoregister` 的 `SMS_PROVIDER` / `SMS_API_KEY` / `L_API_BASE` 等变量原样带过来，代码会优先读 `CODEX_SWITCHER_` 前缀，其次读无前缀名。

## 设置面板（v0.5，所有配置项）

仪表盘顶栏的 **⚙ 设置** 会把服务端 `settings.SPEC` 里的全部 54 个配置项（登录 4 / 接码 21 / 切号 16 / 目标续跑 8 / 高级 5）自动渲染成表单，不需要再改环境变量或重启。

优先级固定为：**设置面板保存的值 > 环境变量 > 内置默认值**。想恢复某几项的环境变量/默认行为，勾选该项的「清除」并保存即可。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/settings` | 返回 `items`（每项含 `key/group/label/type/value/source/is_set/secret/restart/choices`）与 `groups` |
| `POST` | `/api/settings` | 请求体 `{"confirm": true, "values": {...}, "clear": ["key", ...]}`；返回 `changed` / `cleared` / `restart_required` |

约定：

- 请求体必须带 `confirm: true`，否则返回 `400 confirmation_required`；未知键或类型不合法返回 `400 invalid_settings`。
- 密钥类配置（`sms_api_key`、`smsbower_api_key`、`l_admin_auth_code`、`h_admin_auth_code`）用 Windows DPAPI 加密后写入 `%LOCALAPPDATA%\CodexSwitcher\settings.json`，磁盘上没有明文，`GET` 只返回 `****后四位`；表单里留空表示不改，勾「清除」才删除。
- 少数需要重启进程才生效的项（如 `port`）在表单里带「需重启」标记，保存后 `restart_required` 会列出这些键。
- 大部分项保存后立刻生效：`login_driver` 是属性读取，`sms_*` 每次取号前重新读取；`enable_switch` / `enable_auto_switch` 保存后会**立即启动或停止**后台监控线程，不再需要重启。

「切换账号」卡片顶部还有两个和设置面板双向同步的开关：

- **允许切号**（`enable_switch`）：关掉后不会写 `~/.codex/auth.json`；
- **自动切号**（`enable_auto_switch`）：关掉后监控线程立即停止；打开需要先「允许切号」，否则监控保持停止。

开关走 `POST /api/switching/toggle`，点击后需确认。卡片下方还会显示自动切号的目标顺序：只列「有额度、且不是当前账号」的号，按剩余 5h/7d 额度从多到少排，用尽/停用/冷却中直接隐藏；全都没额度时明确提示，不会盲切。账号池列表支持**拖拽排序**，松手即通过 `POST /api/accounts/reorder` 落盘（`priority` 重排为 100、99、98…），该顺序在**额度相同**时决定先切谁。
- 文件写入是原子的（临时文件 + `os.replace`），写坏不会留下半个 JSON。

## 导入格式

推荐用 `|` 分隔，密码里出现连续短横线时最稳：

```text
account@example.com|your-password|JBSWY3DPEHPK3PXP
```

也支持短横线格式，**分隔用的短横线数量不固定**（1 个到任意多个都行）：

```text
account@example.com-----your-password---JBSWY3DPEHPK3PXP
account@example.com--your-password--JBSWY3DPEHPK3PXP
account@example.com-your-password-JBSWY3DPEHPK3PXP
```

说明：

- `#` 开头的行会被忽略。
- 没有 2FA 的账号可以只写两段（结尾多写几个短横线也可以）。
- 短横线格式的密码/2FA 边界从右往左判断：最后一段如果像 base32 的 TOTP 种子（≥16 位），就当作 2FA，前面的整段是密码。所以密码里的 `-`、`--` 不会被误切。
- 如果密码本身长得像 base32 种子、或密码里出现连续短横线且无法区分，请改用 `|` 格式，避免歧义。
- 2FA 不是 base32（例如恢复码）也能导入，但会标记为「非标准」，后续自动登录时可能需要人工处理。
- 同一个邮箱重复导入会更新，不会重复插入。

## 目录结构

```text
codex-switcher/
  service.py                  # 启动入口
  codex_switcher/
    api.py                    # 本地 HTTP API（读取 dashboard.html）
    dashboard.html            # Dashboard 前端（参考 workbuddy-manager 视觉）
    accounts.py               # 账号解析、脱敏、TOTP
    auth_state.py             # 只读解析 ~/.codex/auth.json
    config.py                 # 路径、端口、安全开关
    dpapi.py                  # Windows DPAPI 加解密
    login.py                  # 设备码登录助手（隔离 CODEX_HOME）
    quota.py                  # v0.4 只读额度探测 + 脱敏 + 缓存
    extension_install.py      # 扩展安装助手（派生 ID + 剪贴板 + 只读安装检测）
    auto_switch.py            # v0.4 自动切号策略（冷却/上限/熔断）
    store.py                  # SQLite + DPAPI 账号池 + auth 快照
    switcher.py               # v0.3+ 备份/原子写/校验/回滚
  extension/
    manifest.json             # v0.5 Chrome MV3 扩展（sidePanel + action 徽章）
    background.js             # service worker：alarms 轮询额度 → 徽章/通知
    sidepanel.html            # 侧边栏骨架 + 样式
    sidepanel.js              # 侧边栏逻辑（直连 127.0.0.1:8787）
    icons/                    # 16/32/48/128 图标
  legacy/
    codex-switcher.user.js    # 已退役的 Tampermonkey 面板（仅留档）
  tests/
    test_core.py
    test_api.py
    test_login.py
    test_switch.py
    test_quota.py
    test_quota_reset.py        # 重置券：解析 / consume 报文 / 5 条 gate / 周上限
    test_auto_switch.py
    test_goal_resume.py        # 目标续跑（含「残留 goal/updated」回归）
  README.md
  start.ps1
  stop.ps1
  tray.ps1
  restart-dev.bat              # 停服 + 带 dev 环境变量重启 + 健康检查（本地开发用）
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 版本 + 切号/自动切号开关 + 额度接口地址 |
| GET | `/api/status` | 当前 auth 状态 + 账号池统计 + 自动切号状态 |
| GET | `/api/quota` | 当前账号只读额度；`?force=1` 绕过 5s 缓存 |
| GET | `/api/quota?email=...` | 指定账号（auth 快照）的只读额度；快照不存在返回 404 |
| GET | `/api/quota/history?email=&hours=` | 额度采样序列 + 燃烧速率/耗尽预测（不含 token） |
| POST | `/api/quota/reset` | `{"confirm":true,"email":"?","credit_id":"?"}` 消耗 1 张重置券（**不可撤回**）；省略 `credit_id` 时自动选最早过期的一张；成功返回 `result` + 刷新后的 `quota`，失败回 `400` 与上游原因（`no_credit` / `nothing_to_reset` / `already_redeemed`） |
| GET | `/api/accounts/quotas` | 账号池每个账号的额度（并行、按快照 token 查询、默认缓存 60s）；`?force=1` 跳过缓存 |
| GET | `/api/auto-switch/status` | 自动切号策略、最近结果、冷却、熔断、本小时切换次数、预测状态 |
| GET | `/api/goals` | 目标 + 会话浏览（`?subagents=1` 全显示）：`goals`（状态/累计 token/可续跑）、`sessions`（置顶/项目/最近 分组 + 计数 + 隐藏子代理数）、`job`、`history`（最近 20 次任务，落盘）、`last_resume`（每个会话最近一次续跑结果）、`auto_resume`（`{enabled, thread_ids}`）；只读 |
| POST | `/api/goals/resume` | `{"thread_ids":["..."],"dry_run":true}`；真实续跑需 `dry_run:false` + `confirm:true`；加 `restart_desktop:true` 会先重启 Codex 桌面端再续跑（解 `active writer`） |
| POST | `/api/goals/auto-resume` | `{"thread_id":"...","enabled":true}` 把这个目标加入/移出「切号后自动续跑」，返回最新 `thread_ids` |
| POST | `/api/goals/cancel` | `{"confirm":true}` 取消运行中的续跑任务 |
| GET | `/api/sms/status` | 接码面板状态：平台/服务/国家/等待参数、余额、占用中的号码、最近验证码、最近事件；`?balance=1` 跳过 30s 缓存 |
| POST | `/api/sms/test` | `{"confirm":true}` 取一个号做连通性测试，随后自动释放退款 |
| POST | `/api/sms/cancel` | `{"confirm":true,"activation_id":"?"}` 释放指定/全部占用中的号码并退款 |
| GET | `/api/extension/status` | 扩展目录/manifest 版本/派生 ID/Chrome 路径，以及 Chrome 各 profile 里是否已有该扩展的安装记录（只读） |
| POST | `/api/extension/install` | `{"copy":true,"target":"path"}`：把扩展目录写入剪贴板；`target:"url"` 改为复制 `chrome://extensions`；`copy:false` 为 dry-run。**不启动浏览器**——Chrome 156 丢弃命令行传入的 chrome:// 网址（实测只开空白标签页），接口明确返回 `browser_opened:false`、`browser_blocked:true` 和剩余人工步骤 |
| GET | `/api/accounts` | 账号列表（不含密码/TOTP；含状态、标签、备注、优先级、停用、登录中覆盖状态）+ `login_concurrency` |
| GET | `/api/snapshots` | 当前 auth 公共信息 + 加密快照列表（不含 token） |
| GET | `/api/config` | 版本、路径、端口、切号/自动切号开关、登录助手开关、CLI 路径 |
| GET | `/api/login/status` | 登录助手开关、驱动、`concurrency`、会话列表（含落盘历史，不含密码/TOTP/设备码/CLI 输出） |
| GET | `/api/login/session/<id>` | 单个登录会话状态 |
| POST | `/api/accounts/import` | `{"text":"..."}` 批量导入账号 |
| POST | `/api/accounts/add` | 单个添加：`{"email","password?","totp?","tags?","priority?","note?","disabled?"}` |
| POST | `/api/accounts/update` | 编辑元数据 / 轮换密码或 2FA / 停用；空密码或空 2FA 表示不改 |
| POST | `/api/accounts/delete` | `{"email":"...","confirm":true}` 删除账号 + 快照 + 采样；不触碰 auth.json |
| POST | `/api/accounts/reorder` | `{"emails":["从上到下"]}` 拖拽排序落盘（priority 依次 100、99、98…，最低 1）；校验重复/未知邮箱 |
| POST | `/api/switching/toggle` | `{"key":"enable_switch"\|"enable_auto_switch","value":true,"confirm":true}` 切换开关并即时生效 |
| POST | `/api/switch` | `{"email":"...","confirm":true}`；v0.3+ 需显式启用，执行备份/原子写/校验/回滚 |
| POST | `/api/login/start` | `{"email":"..."}` 启动登录助手；默认禁用时返回 `423 login_disabled` |
| POST | `/api/login/cancel` | `{"session_id":"..."}` 取消登录会话 |

`/api/switch` 在未启用时返回 `423 switching_disabled`；缺少 `confirm=true` 或目标没有快照时返回 `4xx`，不会改动 auth。`/api/quota` 只返回脱敏额度字段，绝不返回 access/refresh token。所有响应都是 JSON（Dashboard 除外）。服务只监听 `127.0.0.1`，并带 `Access-Control-Allow-Origin: *` 方便扩展与本地页面调用。

## 测试

```powershell
cd codex-switcher          # 仓库根目录（克隆后就是这个文件夹）
python -m unittest discover -s tests -v
```

测试覆盖：

- DPAPI 加解密往返；
- 两种账号格式解析、无效行报错；
- TOTP RFC 6238 测试向量；
- 只读解析 auth.json；
- API 健康检查、导入、列表、切号默认禁用、登录助手默认禁用；
- 账号管理：单个添加、标签/优先级/停用、凭据轮换（不回显）、删除确认、旧库原地迁移；
- 额度采样历史、燃烧速率与耗尽预测（含窗口重置分段）；
- 只读额度接口的脱敏、401 映射、token 不落盘/不返回、缓存与 auth hash 失效；
- 自动切号策略：额度触发、候选探测、冷却、每小时上限、连续失败熔断、禁用开关；
- 切号开关热生效（`POST /api/switching/toggle`，含 `confirmation_required` / `bad_key`）、账号池拖拽排序（priority 100/99/98、重复/未知邮箱拒绝）；
- 接码面板（余额解析与错误映射、占用号码、最近验证码、事件日志、取号测试与释放的确认）；
- 设备码解析、登录助手编排（假 CLI + 假浏览器驱动）、取消、加密快照保存；
- 登录记录落盘（跨重启可见、`waiting_*` 重启后标 `interrupted`、磁盘不写设备码/CLI 输出）、并发队列（`concurrency=2` 两个账号同时进驱动、`concurrency=1` 保持串行）；
- 切号引擎：备份、原子替换、校验、回滚、自动快照、过期/撤销快照拒绝；
- 目标续跑：dry-run 不改状态、真实续跑需确认、`paused` 跳过、历史落盘与 `last_resume` 回读；
- 续跑回归：通知缓冲区里残留的旧 `thread/goal/updated` 不会中断刚启动的回合，终止状态只认 `thread/goal/get`；
- 重置券：券列表解析、`consume` 报文与幂等键、上游错误码映射、最早过期优先、5 条自动用券条件、周上限、失败回落、先换号优先；
- 自动用券面板：`auto_reset.gate` 在 `status()` 里按最近额度实时计算（关掉是 `disabled`、条件不足是 `below_threshold`、全满足是 `ok`），查询本身不消耗券；
- **测试前后正式 `auth.json` 的 SHA-256 不变**。

当前全量 **285 tests pass**。另外用 `node --check` 校验了扩展（`sidepanel.js` / `background.js`）以及 `codex_switcher/dashboard.html` 的 JS，并用 Playwright 在 1440×1100 下检查了 Dashboard（账号管理、编辑弹窗、走势图、额度重置券与设置面板，无 console 错误）。

## 后续路线

### v0.2 登录助手（已实现）

- [x] 隔离 `CODEX_HOME` 跑 `codex login --device-auth`。
- [x] 解析 9 位设备码与授权 URL。
- [x] 手动模式 + 可选 Playwright 自动模式。
- [x] 每个账号保存 DPAPI 加密的 `auth.json` 快照。
- [x] 不替换当前 `auth.json`。
- [x] 用真实账号跑一次自动模式（2026-10-30，账号 B，Playwright，session `4d1d130fac9e`，`success`，加密快照已保存）。

### v0.3 切号（已实现并真实 A→B→A 验收）

- [x] `CODEX_SWITCHER_ENABLE_SWITCH=1` 显式启用。
- [x] 切换前 DPAPI 加密备份当前 auth。
- [x] 当前账号自动快照，切出后仍可切回。
- [x] 原子替换、写入后校验、失败自动回滚。
- [x] Dashboard / 扩展侧边栏二次确认。
- [x] `/api/snapshots` 只返回公共元数据，不返回 token。
- [x] 真实 A→B→A 切换后原 auth 哈希逐字节恢复。
- [x] 2026-10-30 真实 A→B 切换：备份、原子写、校验通过；桌面端重启后身份切到新号。

### v0.4 体验（已实现；自动切号保持 opt-in）

- [x] 托盘图标 + 全局快捷键（`tray.ps1`，`Ctrl+Alt+C` 打开 Dashboard）。
- [x] 额度/健康状态展示（`/api/quota`，Dashboard / 扩展侧边栏 / 工具栏徽章）。
- [x] 额度用完自动切下一个账号（`auto_switch.py`，冷却 + 每小时上限 + 失败熔断 + 候选额度探测）。
- [x] Chrome 扩展版本（`extension/`，MV3，侧边栏 + 工具栏徽章）。
- [x] 56 个单元测试通过；`node --check` 和 `tray.ps1 -SelfTest` 通过；真实额度接口验证通过。
- [ ] 用两个「额度可用」的真实账号做一次自动切号端到端验收（当前账号额度已用尽；2026-10-30 20:23 第二个账号登录成功但 5h 窗口也 100%，auto-switch 返回 `no_candidate`，未强制触发）。

## 安全与风控

- 账号密码/TOTP 属于高敏感信息，**不要提交到 git、不要贴到聊天里**。导入一次后由 DPAPI 保护。
- DPAPI 绑定当前 Windows 用户；把 `accounts.sqlite3` 拷到别的用户/机器上无法解密，这是预期行为。
- `data/`、`accounts.sqlite3`、`auth_snapshots`、日志都不要提交。
- 切号会改动 `~/.codex/auth.json`；不要在跑不可中断会话时切换。
- 登录助手默认关闭，避免在你有会话运行时误操作；即使启用，也只写临时 `CODEX_HOME`。
- 自动登录/切号可能触发 OpenAI 的风控和设备限制，建议：
  - 控制登录频率；
  - 保留人工确认；
  - 不要并发登录多个账号；
  - 失败时不要无限重试。
- 共享账号建议在所有设置完成后轮换密码和 TOTP secret。
- 本项目不绕过任何服务端限制；它只是把合法的登录/设备码流程自动化，并管理本地 token。

## 已知限制

- 切号默认禁用；未启用时 `/api/switch` 返回 `423 switching_disabled`。
- 自动切号默认禁用；未启用时不会触发。即使启用，候选账号的 access token 已过期或额度也用尽时不会切换，避免盲目切号。
- 自动切号可能在你有会话时替换 `auth.json`；只建议在可中断的场景开启。
- 额度接口是 ChatGPT 网页/Codex 使用的非公开接口，后续字段或路径可能变化；变化时 `/api/quota` 会返回错误而不是崩溃。
- 托盘快捷键固定为 `Ctrl+Alt+C`；和其它软件冲突时可改 `tray.ps1` 里的键值。
- 扩展是唯一的浏览器侧面板；旧油猴脚本已退役到 `legacy/`，如果 Tampermonkey 里还留着旧脚本请关掉，否则会多出一个浮窗。
- 扩展只支持 Chromium（`chrome.sidePanel` 需 Chrome 114+），Firefox 没有等价侧边栏 API；徽章靠 `chrome.alarms` 每分钟轮询，额度刷新最多有 1 分钟延迟（手动点侧边栏「刷新」会立刻同步徽章）。Chromium 137+ 稳定版移除了 `--load-extension` 命令行开关，只影响脚本化加载，手动「加载已解压的扩展程序」不受影响。
- 切号只替换本地 `auth.json`；Codex 桌面端在启动时读取，运行中不会热加载。切号后应完全退出并重启桌面端/CLI（含后台进程），只关窗口不够。
- 快照里的 access token 可能已过期；切号后依赖 refresh token 续期。refresh token 被撤销或账号额度用尽时，切过去也会在 Codex 侧失败。
- 登录助手默认禁用；需要显式环境变量并重启服务。
- Playwright 自动模式需要页面保持前台可见；窗口被遮挡时 Sentinel 可能超时。
- 设备码登录要求账号在 <https://chatgpt.com/settings/security> 打开「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」。
- 切号器**不会自动修改**这个开关；登录助手检测到未开启时只报错并提示手动打开。保持手动是刻意的安全边界，避免切号器静默改动账号安全设置。
- 2FA 只支持 TOTP；邮箱/短信验证码需要人工回退。
- 扩展依赖本地服务：服务没启动时侧边栏显示「先运行 python service.py」，徽章显示 `!`。

## 许可

MIT，见 [LICENSE](LICENSE)。

