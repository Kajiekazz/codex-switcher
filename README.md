# Codex Switcher

本地 **Codex 多账号助手**：一个跑在 `127.0.0.1:8787` 的本地服务管理账号池、额度、切号和登录，一个 Chrome 侧边栏随时显示当前账号还剩多少额度。

只在这台机器上跑，只监听 `127.0.0.1`；**所有会改动 `~/.codex/auth.json` 的动作默认关闭**，要显式打开才会执行。

| 你想要的 | 这个工具怎么给 |
|---|---|
| 好几个号轮着用 | 导入账号 → 自动取好每个号的 `auth.json` 快照 → 一键切换（备份 / 原子写 / 校验 / 回滚） |
| 额度用完自动换号 | 自动切号：整池探测候选，按剩余额度排序挑号，带冷却和熔断 |
| 不想一直盯额度 | 扩展工具栏徽章直接显示 5 小时剩余百分比，颜色分档 |
| 换号后接着干活 | 勾选目标会话，切号成功后自动唤醒被额度打断的 Codex 目标 |
| 有重置券但怕浪费 | 重置券默认不动，只在换不到号时兜底，阈值自己定 |

---

## 环境要求

| 项 | 说明 |
|---|---|
| 系统 | Windows 10 / 11（DPAPI、托盘、`start.ps1` 都依赖 Windows） |
| Python | 3.10+，**纯标准库，不需要 `pip install` 任何东西** |
| Codex | 桌面端或 CLI 至少成功登录过一次（需要 `~/.codex/auth.json`） |
| 浏览器 | Chrome 114+ 才支持侧边栏扩展（可选，不装也能用 Dashboard） |
| 可选依赖 | 只有「自动登录」需要 Playwright（懒加载，不装不影响其它功能） |

## 安装

```powershell
git clone https://github.com/Kajiekazz/codex-switcher.git
cd codex-switcher
```

没有构建步骤，没有 `requirements.txt`，`python service.py` 就能跑。

## 快速开始

```powershell
# 1. 启动（-EnableSwitch 才会真正执行切号）
.\start.ps1 -EnableSwitch

# 2. 打开 Dashboard
start http://127.0.0.1:8787/
```

看到这些就说明起来了：

```text
Starting Codex Switcher v0.6...
Switching: ENABLED / backup + atomic write + verify + rollback
Auto switch: DISABLED (use -EnableAutoSwitch to opt in)
Login assistant: DISABLED (use -EnableLogin to opt in)
```

3. **导入账号**：Dashboard「账号池」→ 粘贴账号（见[导入格式](#导入账号)）→ 导入。
4. **取 auth 快照**：账号行右侧点「登录」→ 启动登录助手 → 完成设备码授权。只有拿到快照的账号才能被切过去。
5. **切号**：在「切换账号」卡片选目标 → 勾选确认 → 切换。

停止服务：`.\stop.ps1`。

> 改环境变量后必须 `.\stop.ps1` 再启动，已运行的服务不会热更新 env。设置面板里改的项例外（保存即生效）。

---

## 导入账号

推荐 `|` 分隔，密码里有连续短横线时最稳：

```text
account@example.com|your-password|JBSWY3DPEHPK3PXP
```

短横线格式也支持，**分隔用的短横线数量不固定**：

```text
account@example.com-----your-password---JBSWY3DPEHPK3PXP
account@example.com--your-password--JBSWY3DPEHPK3PXP
account@example.com-your-password-JBSWY3DPEHPK3PXP
```

- `#` 开头的行忽略；没有 2FA 就只写两段。
- 短横线格式从右往左判断边界：最后一段像 base32 TOTP 种子（≥16 位）就当 2FA，前面整段是密码，所以密码里的 `-` / `--` 不会被误切。
- 密码本身像 base32、或密码里连续短横线无法区分时，改用 `|` 格式。
- 非 base32 的 2FA（如恢复码）也能导入，但标记「非标准」，自动登录时可能要人工处理。
- 同一邮箱重复导入是更新，不会重复插入；API 从不返回密码和 TOTP 明文。

2FA 支持 raw base32 或 `otpauth://` 链接。除了批量粘贴，账号池还支持单个添加、编辑（标签 / 备注 / 优先级 / 停用 / 轮换密码与 2FA）、删除——删除只影响账号池、快照和额度采样，**不动** `~/.codex/auth.json`。

---

## 让账号拿到 auth 快照（登录助手）

切号依赖「加密 auth 快照」。拿快照有两种方式：

1. 当前正在用的账号：服务启动后第一次切号前会自动快照，所以切出去还能切回来；
2. 其它账号：用登录助手登录一次。

登录助手默认禁用，需要重启服务时打开：

```powershell
.\stop.ps1
.\start.ps1 -EnableLogin -Driver manual      # 自动模式用 -Driver playwright
```

等价环境变量：

```powershell
$env:CODEX_SWITCHER_ENABLE_LOGIN = "1"
$env:CODEX_SWITCHER_LOGIN_DRIVER = "manual"
python service.py
```

Dashboard 没有单独的登录卡片：每个账号行右侧有「登录 / 重新登录 / 查看登录」按钮，弹窗里显示设备码、URL、进度和取消。

### 手动模式（默认，建议先跑通）

1. 服务端在隔离的 `CODEX_HOME`（`%LOCALAPPDATA%\CodexSwitcher\login-sessions\<id>`）里启动 `codex login --device-auth`；
2. Dashboard 显示 9 位设备码，例如 `PBAH-TBU2Y`；
3. 你在浏览器完成登录、选账户、同意、输入设备码；
4. CLI 把 `auth.json` 写进临时目录，服务读取后加密存为快照，CLI 结束；
5. 当前 `~/.codex/auth.json` 全程没变。

### Playwright 自动模式（可选）

```powershell
.\start.ps1 -EnableLogin -Driver playwright
```

先 `pip install playwright`（用系统 Chrome，不需要 `playwright install`）。行为：

- 用系统 Chrome + 每个账号独立的专用 profile，不碰你日常的 Chrome profile；
- 自动填邮箱 / 密码 / TOTP、选账户、点同意、填设备码；
- 页面必须**前台可见**：窗口被遮挡时 Sentinel 会卡住，驱动会拉到前台重试，仍失败就改手动模式；
- 账号没开「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」时，驱动会自己打开设置页把这个开关打开（`driver.log` 记 `device_code_setting_enabled`）；只有打不开时才报 `device_code_login_disabled` 让你手动处理；
- 设备码逐格填写并回读校验，只有页面出现「已登录 Codex」才判定成功；
- 失败留 `login-sessions/<id>/failure.png`、`cli.log`、`driver.log`（查询串已脱敏）。

> 自动模式依赖 ChatGPT 页面结构，改版后选择器可能要修。手动模式永远可用。

### 批量导入时并发认证

`login_concurrency` 默认 **2**（1–4），每个账号一个独立 Chrome 窗口 + 独立 `CODEX_HOME`，其余排队。Dashboard 导入弹窗和设置面板都能改，保存立即生效。

并发高会互相抢焦点、拉高 Cloudflare/Sentinel 失败率；手动模式下每个号都要人操作，建议设 1。单次实测两个账号 `started_at` 相差 3 ms，两个 CLI 和两个 profile 同时在跑。

### 手机短信接码（可选）

部分账号 OAuth 会要求手机号验证。配置接码平台后，驱动检测到 `/phone-verification` 会自己取号、填号、等短信、填码：

```powershell
$env:CODEX_SWITCHER_SMS_PROVIDER = "l"          # grizzly / smsbower / l / h；不设 = 关闭
$env:CODEX_SWITCHER_L_API_BASE = "http://localhost:8788"
$env:CODEX_SWITCHER_L_ADMIN_AUTH_CODE = "你的后台授权码"
```

- 平台和变量名与 `chatgpt-autoregister` 兼容（`SMS_PROVIDER`、`SMS_API_KEY`、`L_API_BASE` 等可直接沿用，代码先读 `CODEX_SWITCHER_` 前缀再读无前缀名）；
- 号码被拒或等码超时会 `cancel` 退款，再换下一个号；轮询期间取消登录会立刻释放号码；
- 没配平台时遇到手机验证直接报 `sms_not_configured`，不会静默卡住；
- OpenAI 常把虚拟号静默改成 WhatsApp 发码，驱动会**立刻**判失败并释放号码，连续 2 次报 `sms_number_rejected`；
- 实测越南 / 英国的号都被改成 WhatsApp，**美国（国家码 187）能真收短信**；
- 密钥优先读设置面板里保存的值（DPAPI 加密），其次环境变量；`/api/*` 只返回 `****后四位`。

Dashboard 的「接码」卡片是观测台：平台余额、占用中的号码（可单个释放）、最近验证码、事件日志。取号测试会取一个号再立即释放退款。**验证码和号码在这里不脱敏**（本机排查需要），平台 API key 仍然只给后四位。

---

## 手动切号

前提：目标账号有加密快照（见上一节）。

1. 打开 Dashboard →「切换账号」卡片选目标；
2. 勾选「确认切换并接受自动回滚」→ 点「切换账号」（扩展侧边栏同样要二次确认）；
3. 服务依次做：DPAPI 加密备份当前 auth 到 `backups\` → 自动快照当前账号 → 原子替换 `~/.codex/auth.json` → 重新解析校验 → 失败用备份回滚；
4. 返回 `previous_email`、`target_email`、`backup_path`，不含任何 token。

> **切换后桌面端还显示旧账号？** Codex 桌面端只在启动时读 `auth.json`，运行中不热加载。要**完全退出**桌面端 / CLI（含托盘、后台 `codex` 子进程）再启动，只关窗口不够。

## 自动切号

默认禁用，两个开关缺一不可：

```powershell
.\start.ps1 -EnableSwitch -EnableAutoSwitch
```

触发逻辑：

1. 每 90 秒读一次当前账号的只读额度接口（`/backend-api/wham/usage`）；
2. 只有 `limit_reached=true` 或 `allowed=false` 才进入选号，额度正常时什么都不做；
3. 候选 = 池里有加密快照、不是当前账号、不在冷却期；**默认整池探测**，6 并发并复用账号池额度缓存，40 个号一轮也就几秒；
4. **额度优先**：候选按剩余额度排序，5 小时剩余最多的排最前，其次比 7 天，最后才用 `priority` 和保存时间决胜——高优先级但快用完的号不会挤掉刚重置的新号；
5. 选中后走正常切号路径（备份 / 原子写 / 校验 / 回滚）；
6. 用尽或被拒的候选冷却到它自己的 5 小时重置时间（至少 15 分钟）；被切走的号同样冷却，防止马上切回去；
7. 全池没额度时进入 `no_candidate`：最近的额度重置在 15 分钟内就等重置后 5 秒再试，否则每 5 分钟重试；
8. 每小时最多 3 次，连续 3 次失败熔断 30 分钟。

「切换账号」卡片下方会实时列出候选顺序（只列有额度且不是当前账号的号，按剩余额度从多到少），全都没额度时明确写「现在没有有额度的账号」，不会盲切。

审计日志 `auto-switch.log` 只记事件和邮箱，不记 token。

> 自动切号只改本地 `auth.json`，不会买额度、不改套餐/账单、也不会替你重启桌面端。
> 快照里 access token 过期时额度探测会 401，该号被跳过——用登录助手重登一次即可恢复候选资格。

可选预测切换：`CODEX_SWITCHER_PREDICTIVE_SWITCH=1` 后可在预测耗尽前 `CODEX_SWITCHER_PREDICTIVE_THRESHOLD` 秒（默认 600）提前切。

## 额度与重置券

Dashboard 和侧边栏显示当前账号的 5 小时 / 7 天**已用**百分比和重置时间，`/api/quota` 只返回脱敏字段（`?force=1` 跳过 5 秒缓存）。每次探测按账号记一条采样，Dashboard 画最近 6 小时走势并估算耗尽时间，采样保留 30 天。

账号池每一行也直接显示该号的 5h / 7d / 套餐，用各账号自己的快照 token 并行查询，不用先切成当前账号；缓存默认 60 秒（`pool_quota_ttl` 可改）。

Codex 偶尔发 **「Full reset (Weekly + 5 hr)」重置券**：一张同时清零 5 小时和 7 天窗口，一次性、不可撤回、通常一个月过期。

- 手动：额度卡片显示「重置券 可用 N 张」，点「使用重置券」并二次确认（要求切号已启用）。省略 `credit_id` 时自动选最早过期的一张。
- 自动（默认**关闭**）：定位是最后兜底，必须同时满足——
  1. 当前账号 `limit_reached=true`；
  2. 账号池里**没有**可用候选（还有号能换就先换号，别烧券）；
  3. 7 天窗口已用 ≥ `auto_reset_secondary_threshold`（默认 98%）；
  4. 7 天窗口不会在 `auto_reset_min_wait_hours`（默认 72 小时）内自然重置；
  5. 还有可用券，且最近 7 天自动用券次数 < `auto_reset_max_per_week`（默认 1）。

打开方式：`.\start.ps1 -EnableSwitch -EnableAutoSwitch -EnableAutoReset`，或设置面板「切号」分组里逐项改。

`GET /api/auto-switch/status` 的 `auto_reset.gate` 会实时告诉你没动手的原因（`disabled` / `below_threshold` / `window_resets_soon` / `no_credit` / `weekly_cap` / `unknown`），查询本身不消耗券。

> `auto_reset_before_switch=1` 可以改成「先烧券再换号」，一般不建议。

## 目标续跑

Codex 的 goal 在额度用尽后进入 `usage_limited`，额度恢复后要重新激活才会继续。这功能把「续跑哪个」交给你，而不是无脑全拉起。

```powershell
.\stop.ps1
.\start.ps1 -EnableSwitch -EnableAutoSwitch -EnableGoalResume
```

Dashboard「会话」卡片按 Codex 的风格分组（置顶 / 项目 / 最近），默认只列 `thread_source=user`，子代理折叠，**有 goal 的会话永远显示**并标注目标状态与累计 token；顶部搜索按标题 / 项目 / 路径 / 目标 / thread_id 过滤。

续跑流程：

1. 勾选可续跑的会话：`usage_limited`、`paused`、以及**没有回合在跑**的 `active`（切号重启桌面后最常见）；`complete` / `blocked` 不可选；
2. 点「预览（dry-run）」只调 `thread/goal/get` 验证，**不改任何状态**；
3. 点「续跑选中」并确认后，服务起一个独立 `codex app-server`：`thread/resume` → `thread/goal/set`（`status=active`）→ 等 `turn/started`；`goal_resume_start_timeout`（默认 12s）内没有自动回合就发「继续」；
4. 每个回合结束重读 goal 状态，变 `complete` / `paused` / `blocked` / 再次 `usage_limited` 就停；超过 `goal_resume_max_runtime`（默认 7200s）停下并保留 `active`；
5. 运行中顶部常显「● 正在续跑」和已运行时长，右上「停止续跑」随时可点。

只操作 app-server，**从不直接写 goals SQLite**；一次只跑一个任务，勾选的会话串行处理。

### 切号后自动续跑

会话行上的「自动」按钮把 `thread_id` 存进 `settings.auto_resume_threads`。之后每次切号成功（手动或自动）都会唤醒已勾选的目标：

- 只续跑 `usage_limited` / `paused` / 假 `active`，`complete` / `blocked` 跳过；
- 已有续跑任务在跑就跳过，不叠加；
- 不需要再点确认——勾「自动」本身就是逐目标的预先确认；
- 总开关是设置里的 `auto_resume_after_switch`（默认开）。

### 续跑卡住的两种真实原因

- `thread/resume: ... already has an active writer`：桌面端或另一个 app-server 占着该线程，**不是额度问题**。行上显示「会话被 Codex 占用」，卡片顶部提示点它可以先重启桌面端再续跑（`restart_desktop: true`）。
- 真额度受限：当前号窗口已满，续跑会立刻再进 `usage_limited`，先切号或等窗口重置。

排障：`CODEX_SWITCHER_GOAL_RESUME_TRACE=1` 会打印每次等到的通知名和剩余时间。

## 浏览器扩展（侧边栏 + 徽章）

`extension/` 是 Manifest V3 扩展，两种形态合一：常驻右侧的**侧边栏**（显示服务状态、当前账号、额度条、账号池数量、切号与批量导入入口）和工具栏**徽章**（5 小时剩余百分比，颜色分档）。

安装（最省事）：

1. Dashboard →「浏览器扩展」卡片 → 点「复制安装路径」；
2. 点地址栏右侧的**拼图图标 🧩** →「管理扩展程序」（也可以粘 `chrome://extensions`）；
3. 打开「开发者模式」→「加载已解压的扩展程序」→ 粘贴路径 → 选择文件夹；
4. 回 Dashboard 点「重新检测」，「未安装」变「已安装」。

> 没有任何程序能替你打开 `chrome://extensions`——Chrome 会丢弃命令行传入的 `chrome://` 网址。卡片只复制路径/网址，不假装打开。
> 不做静默安装：Chrome 只认 Web Store 和企业策略两条通路，后者会让浏览器显示「由组织管理」，所以不碰注册表。扩展 ID 由目录路径派生（`ccpnehmimijbfhcjonkbgbimhjpohfkj`），路径不变就不会变。

徽章含义：

| 徽章 | 含义 |
|---|---|
| `62%` 绿 | 5h 还剩一半以上 |
| `34%` 琥珀 | 剩 20–49%，留意 |
| `8%` 红 | 剩不到 20%，准备切号 |
| `0%` 红 + 通知 | 用尽，点图标进侧边栏切号 |
| `–` 灰 | 没有 `auth.json`（没登录） |
| `!` 灰 | 本地服务没起来 |
| `?` 灰 | 服务在线但额度读取失败 |

扩展只访问 `127.0.0.1:8787`，不需要账号密码；侧边栏里的切号仍受服务端开关约束，且必须确认。`extension/` 是面板唯一源文件，改完在扩展页点刷新即可。

## 托盘与快捷键

```powershell
.\start.ps1 -EnableSwitch -Tray
```

- 服务未运行会先后台启动并等 `/api/health` 就绪，再打开托盘；已在运行则直接复用（env 开关不会被改）。
- 托盘菜单：打开 Dashboard、复制当前邮箱、刷新状态、启停服务、退出。
- 全局快捷键 `Ctrl+Alt+C` 打开 Dashboard；每 10 秒刷新 tooltip，额度失败不影响托盘。

## 设置面板

Dashboard 顶栏 **⚙ 设置** 把 54 个配置项渲染成表单（登录 4 / 接码 21 / 切号 16 / 目标续跑 8 / 高级 5），不用改 env、不用重启。

优先级固定：**设置面板保存的值 > 环境变量 > 内置默认值**。想恢复某项的环境变量行为，勾选该项「清除」并保存。

- 密钥类（`sms_api_key`、`smsbower_api_key`、`l_admin_auth_code`、`h_admin_auth_code`）用 DPAPI 加密写进 `settings.json`，磁盘无明文，`GET` 只给 `****后四位`；表单留空 = 不改；
- 少数项（如 `port`）标「需重启」，保存后 `restart_required` 会列出来；
- 大部分项保存即生效：`enable_switch` / `enable_auto_switch` 会**立即启动或停止**监控线程，`sms_*` 每次取号前重读。

「切换账号」卡片顶部两个开关与设置面板双向同步（`POST /api/switching/toggle`，需确认）；账号池支持拖拽排序（`POST /api/accounts/reorder`，落盘为 `priority` 100、99、98…），这个顺序只在**额度相同**时决定先切谁。

### 常用环境变量

| 变量 | 默认 | 说明 |
|---|---:|---|
| `CODEX_SWITCHER_ENABLE_SWITCH` | `0` | 手动切号总开关 |
| `CODEX_SWITCHER_ENABLE_AUTO_SWITCH` | `0` | 自动切号（还需上一条） |
| `CODEX_SWITCHER_ENABLE_LOGIN` | `0` | 登录助手 |
| `CODEX_SWITCHER_LOGIN_DRIVER` | `manual` | `manual` / `playwright` |
| `CODEX_SWITCHER_LOGIN_CONCURRENCY` | `2` | 同时登录数，1–4 |
| `CODEX_SWITCHER_ENABLE_AUTO_RESET` | `0` | 自动用重置券 |
| `CODEX_SWITCHER_ENABLE_GOAL_RESUME` | `0` | 目标续跑 |
| `CODEX_SWITCHER_PORT` | `8787` | 监听端口 |
| `CODEX_SWITCHER_DATA` | `%LOCALAPPDATA%\CodexSwitcher` | 数据目录 |
| `CODEX_SWITCHER_AUTH` | `~/.codex/auth.json` | 被切换的 auth 文件 |

- 自动切号：`CODEX_SWITCHER_AUTO_SWITCH_INTERVAL`（90s）、`_COOLDOWN`（900s）、`_MAX_PER_HOUR`（3）、`_MAX_PROBES`（0 = 整池）、`_FAILURE_THRESHOLD`（3）
- 额度缓存：`CODEX_SWITCHER_QUOTA_CACHE_TTL`（30s）、`CODEX_SWITCHER_POOL_QUOTA_TTL`（60s）、`CODEX_SWITCHER_USAGE_URL`
- 重置券：`_SECONDARY_THRESHOLD`（98）、`_MIN_WAIT_HOURS`（72）、`_MAX_PER_WEEK`（1）、`_BEFORE_SWITCH`（0）
- 续跑：`CODEX_SWITCHER_GOAL_RESUME_MAX_RUNTIME`（7200s）、`_START_TIMEOUT`（12s）、`CODEX_SWITCHER_GOAL_RESUME_TRACE`

名称统一带 `CODEX_SWITCHER_` 前缀；接码相关变量见[手机短信接码](#手机短信接码可选)（也兼容无前缀的 `SMS_PROVIDER` 等）。

---

## API

服务只监听 `127.0.0.1`，所有响应都是 JSON（Dashboard 除外），带 `Access-Control-Allow-Origin: *` 方便扩展调用。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 版本 + 各开关 + 额度接口地址 |
| GET | `/api/status` | 当前 auth 状态 + 账号池统计 + 自动切号状态 |
| GET | `/api/quota` | 当前账号额度；`?force=1` 跳过缓存；`?email=` 查指定账号 |
| GET | `/api/quota/history?email=&hours=` | 采样序列 + 燃烧速率 / 耗尽预测 |
| POST | `/api/quota/reset` | `{"confirm":true,"credit_id":"?"}` 消耗 1 张重置券（不可撤回） |
| GET | `/api/accounts/quotas` | 账号池每号额度（并行、60s 缓存） |
| GET | `/api/auto-switch/status` | 策略、最近结果、冷却、熔断、本小时次数、`auto_reset.gate` |
| GET | `/api/goals` | 目标 / 会话 / 任务 / 历史 / `auto_resume`；只读 |
| POST | `/api/goals/resume` | `{"thread_ids":[...],"dry_run":true}`；真实续跑需 `dry_run:false` + `confirm:true` |
| POST | `/api/goals/auto-resume` | `{"thread_id":"...","enabled":true}` 加入/移出切号后自动续跑 |
| POST | `/api/goals/cancel` | `{"confirm":true}` 取消运行中的续跑 |
| GET | `/api/sms/status` | 接码面板状态；`?balance=1` 跳过缓存 |
| POST | `/api/sms/test` | 取一个号做连通性测试，随后释放退款 |
| POST | `/api/sms/cancel` | `{"confirm":true,"activation_id":"?"}` 释放号码并退款 |
| GET | `/api/extension/status` | 扩展版本 / 派生 ID / Chrome 路径 / 各 profile 是否已装 |
| POST | `/api/extension/install` | 把扩展目录写进剪贴板（`target:"url"` 复制扩展页网址）；不启动浏览器 |
| GET | `/api/settings` | 54 项的 `key/group/label/type/value/source/secret/restart` |
| POST | `/api/settings` | `{"confirm":true,"values":{...},"clear":[...]}` |
| GET | `/api/accounts` | 账号列表（无密码 / TOTP）+ `login_concurrency` |
| GET | `/api/snapshots` | 当前 auth 公共信息 + 快照列表 |
| GET | `/api/config` | 版本、路径、端口、各开关、CLI 路径 |
| GET | `/api/login/status` | 登录开关、驱动、`concurrency`、会话（无设备码 / CLI 输出） |
| GET | `/api/login/session/<id>` | 单个登录会话 |
| POST | `/api/accounts/import` | `{"text":"..."}` 批量导入 |
| POST | `/api/accounts/add` | 单个添加：`{"email","password?","totp?","tags?","priority?","note?","disabled?"}` |
| POST | `/api/accounts/update` | 改元数据 / 轮换密码或 2FA / 停用（空值表示不改） |
| POST | `/api/accounts/delete` | `{"email":"...","confirm":true}` 删账号 + 快照 + 采样 |
| POST | `/api/accounts/reorder` | `{"emails":["从上到下"]}` 拖拽排序落盘 |
| POST | `/api/switching/toggle` | `{"key":"enable_switch"\|"enable_auto_switch","value":true,"confirm":true}` |
| POST | `/api/switch` | `{"email":"...","confirm":true}`；未启用返回 `423 switching_disabled` |
| POST | `/api/login/start` | `{"email":"..."}`；默认禁用时返回 `423 login_disabled` |
| POST | `/api/login/cancel` | `{"session_id":"..."}` |

缺少 `confirm=true` 或目标没有快照时返回 `4xx`，不会改动 auth。

---

## 数据与安全

数据都在 `%LOCALAPPDATA%\CodexSwitcher\`：

| 文件 | 内容 |
|---|---|
| `accounts.sqlite3` | 账号池 + 加密的 `auth_snapshots`（密码 / TOTP / token 都是 DPAPI 密文） |
| `backups\` | 每次切号前的 `auth.json` 加密备份 |
| `settings.json` | 设置面板的值（密钥字段 DPAPI 加密） |
| `auto-switch.log` | 自动切号事件（只记事件和邮箱，不记 token） |
| `login-history.json` | 登录记录（最近 50 条；**不写**设备码和 CLI 输出，重启后仍在） |
| `goal-resume-history.jsonl` | 续跑历史（200 条） |
| `login-sessions\` | 每次登录的隔离 `CODEX_HOME`、`cli.log`、`driver.log`、失败截图 |

安全边界：

- DPAPI 绑定当前 Windows 用户——把数据库拷到别的机器/用户下解不开，这是预期行为；
- 切号默认禁用，未启用时 `/api/switch` 返回 `423`；写入用原子替换 + 写后校验 + 失败回滚；
- 自动切号默认禁用，候选必须自身额度可用，且只走正常切号路径；
- 登录助手默认禁用，即使启用也只写隔离的临时 `CODEX_HOME`，不碰当前 `auth.json`；
- 额度探测只读：token 只进请求头，不返回、不写日志、不落盘；
- 面板里的重置券 / 切号 / 删除 / 释放号码等动作都要 `confirm`。

**不要提交** `accounts.sqlite3`、`*.log`、`settings.json`、`auth.json` 或任何密码/TOTP 到 git（仓库 `.gitignore` 已覆盖 `data/`、`work/`、`*.log`、`*.sqlite3`、`*.jsonl`）。

风控建议：控制登录频率、保留人工确认、别并发登录太多号、失败别无限重试；共享账号在全部设置完后轮换密码和 TOTP。本项目不绕过任何服务端限制，只是把合法的登录 / 设备码流程自动化并管理本地 token。

---

## 常见问题

**切号成功后桌面端还是旧账号？**
桌面端只在启动时读 `auth.json`。完全退出（含托盘和后台 `codex` 进程）再启动。

**侧边栏一直显示「先运行 python service.py」/ 徽章是 `!`？**
服务没起来。`.\start.ps1 -EnableSwitch`，或检查 `127.0.0.1:8787/api/health`。

**徽章数字和 Dashboard 差一点？**
徽章靠 `chrome.alarms` 每分钟轮询，最多 1 分钟延迟；点侧边栏「刷新」会立刻同步。

**切号报「没有可用快照」/ 自动切号一直 `no_candidate`？**
账号没有加密快照，或快照里的 access token 过期（额度探测 401）。用登录助手重新登录一次。

**自动切号不触发？**
两个开关都要开（`ENABLE_SWITCH` + `ENABLE_AUTO_SWITCH`），且只有 `limit_reached=true` / `allowed=false` 才触发；改 env 后要 `.\stop.ps1` 重启。

**登录助手报 `device_code_login_disabled`？**
账号没开「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」。自动模式通常能自己打开；打不开就手动去 <https://chatgpt.com/settings/security> 打开（切号器不会替你静默改账号安全设置）。

**Playwright 登录总是失败在「糟糕，出错了」？**
Chrome 窗口被遮挡导致 Sentinel 卡住。保持窗口前台，或改 `-Driver manual`。

**手机验证收不到短信？**
虚拟号常被静默改成 WhatsApp 发码；换国家码 **187（美国）**，或换平台。

**扩展装不上 / Chrome 说 `--load-extension` 无效？**
Chromium 137+ 移除了命令行开关，只影响脚本化加载；手动「加载已解压的扩展程序」不受影响。

**托盘快捷键冲突？**
`Ctrl+Alt+C` 写死在 `tray.ps1`，改那里即可。

## 已知限制

- 只支持 Windows；DPAPI 是硬依赖。
- 额度接口是 ChatGPT / Codex 用的非公开接口，字段或路径可能变；变化时 `/api/quota` 返回错误而不是崩。
- 切号只换本地 `auth.json`；refresh token 被撤销或号已用尽时，切过去在 Codex 侧也会失败。
- 2FA 只支持 TOTP；邮箱/短信验证码需要人工回退（短信可由接码平台代劳，但仍是外部服务）。
- 扩展只支持 Chromium（`chrome.sidePanel` 需 114+），Firefox 没有等价侧边栏 API。
- 旧油猴脚本已退役到 `legacy/`；Tampermonkey 里还留着旧脚本请关掉，否则多一个浮窗。
- 自动切号可能在你有会话时替换 `auth.json`，只建议在可中断的场景开。

## 开发

```powershell
# 全量测试（285 个）
python -m unittest discover -s tests

# 单个模块
python -m unittest discover -s tests -p "test_quota_reset.py" -v
```

测试会临时改写 auth 路径并在结束时校验正式 `auth.json` 的 SHA-256 不变。

```text
codex-switcher/
  service.py              # 入口
  start.ps1 / stop.ps1    # 起停（-EnableSwitch / -EnableAutoSwitch / -EnableAutoReset / -EnableLogin / -EnableGoalResume / -Tray）
  tray.ps1                # 托盘 + Ctrl+Alt+C
  restart-dev.bat         # 本地开发：停服 + 带 env 重启 + 健康检查
  codex_switcher/
    api.py                # 本地 HTTP API
    dashboard.html        # Dashboard 前端（单文件）
    accounts.py           # 账号解析、脱敏、TOTP
    auth_state.py         # 只读解析 auth.json
    config.py             # 路径、端口、开关
    dpapi.py              # Windows DPAPI
    store.py              # SQLite 账号池 + 加密快照
    switcher.py           # 备份 / 原子写 / 校验 / 回滚
    quota.py              # 只读额度 + 重置券
    auto_switch.py        # 自动切号策略
    login.py              # 设备码登录 + Playwright 驱动 + 接码
    goal_resume.py        # 目标续跑
    settings.py           # 54 项配置
    extension_install.py  # 扩展安装助手
  extension/              # MV3 扩展（侧边栏 + 徽章）
  legacy/                 # 已退役的油猴面板
  tests/                  # unittest
```

版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
