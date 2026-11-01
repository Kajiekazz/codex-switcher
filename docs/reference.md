# 参考：配置、数据与 API

README 只讲怎么用；这里是查表用的细节。所有内容对应当前版本（`/api/health` 里的 `version`）。

## 启动开关

```powershell
.\start.ps1 [-EnableSwitch] [-EnableAutoSwitch] [-EnableAutoReset] [-EnableLogin] [-Driver manual|playwright] [-EnableGoalResume] [-Tray] [-RestartDesktop]
.\stop.ps1
```

| 参数 | 作用 | 对应环境变量 |
|---|---|---|
| `-EnableSwitch` | 允许真正写 `~/.codex/auth.json` | `CODEX_SWITCHER_ENABLE_SWITCH=1` |
| `-EnableAutoSwitch` | 额度用尽时自动切号（还需上一条） | `CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1` |
| `-EnableAutoReset` | 换不到号时允许自动烧重置券 | `CODEX_SWITCHER_ENABLE_AUTO_RESET=1` |
| `-EnableLogin` / `-Driver` | 登录助手 + 驱动 | `CODEX_SWITCHER_ENABLE_LOGIN=1`、`CODEX_SWITCHER_LOGIN_DRIVER` |
| `-EnableGoalResume` | 目标续跑 | `CODEX_SWITCHER_ENABLE_GOAL_RESUME=1` |
| `-Tray` | 后台起服务 + 托盘（`Ctrl+Alt+C`） | — |
| `-RestartDesktop` | 自动切号后重启桌面端进程树 | `CODEX_SWITCHER_RESTART_DESKTOP=1` |

改 env 后必须 `.\stop.ps1` 再启动；`-Tray` 发现服务已在跑时不会改已有开关。

## 设置面板

Dashboard 顶栏 **⚙ 设置** 把服务端 `settings.SPEC` 的 54 项渲染成表单：登录 4 / 接码 21 / 切号 16 / 目标续跑 8 / 高级 5。

优先级固定：**设置面板保存的值 > 环境变量 > 内置默认值**。想恢复某项的环境变量行为，勾该项的「清除」并保存。

- 密钥类（`sms_api_key`、`smsbower_api_key`、`l_admin_auth_code`、`h_admin_auth_code`）用 DPAPI 加密写入 `settings.json`，磁盘上没有明文，`GET` 只返回 `****后四位`；表单留空表示不改。
- 少数项（如 `port`、`host`）标「需重启」，保存后响应的 `restart_required` 会列出来。
- 其余保存即生效：`enable_switch` / `enable_auto_switch` 会立即启动或停止监控线程，`sms_*` 每次取号前重读，`login_*` 走属性读取。

「切换账号」卡片顶部两个开关与设置面板双向同步（`POST /api/switching/toggle`，需确认）。

## 环境变量

名称统一带 `CODEX_SWITCHER_` 前缀，括号内是默认值。

**基础**

| 变量 | 默认 | 说明 |
|---|---:|---|
| `CODEX_SWITCHER_PORT` | `8787` | 监听端口 |
| `CODEX_SWITCHER_DATA` | `%LOCALAPPDATA%\CodexSwitcher` | 数据目录 |
| `CODEX_SWITCHER_AUTH` | `~/.codex/auth.json` | 被切换的 auth 文件 |
| `CODEX_SWITCHER_CODEX_EXE` | 自动查找 | `codex` 可执行文件 |
| `CODEX_SWITCHER_GOALS_DB` / `_STATE_DB` | `~/.codex/goals_1.sqlite` / `state_5.sqlite` | 只读来源 |

**自动切号**

| 变量 | 默认 | 说明 |
|---|---:|---|
| `AUTO_SWITCH_INTERVAL` | 90 | 检查间隔秒（15–3600） |
| `AUTO_SWITCH_COOLDOWN` | 900 | 账号冷却秒（60–86400） |
| `AUTO_SWITCH_MAX_PER_HOUR` | 3 | 每小时最多切换次数（1–24） |
| `AUTO_SWITCH_MAX_PROBES` | 0 | 每轮探测上限，`0` = 整池（0–500） |
| `AUTO_SWITCH_FAILURE_THRESHOLD` | 3 | 连续失败熔断阈值（1–20） |
| `PREDICTIVE_SWITCH` / `PREDICTIVE_THRESHOLD` | 关 / 600 | 预测耗尽前提前切换（秒） |
| `QUOTA_CACHE_TTL` | 30 | 当前账号额度缓存秒 |
| `POOL_QUOTA_TTL` | 60 | 账号池额度缓存秒 |
| `USAGE_URL` | `https://chatgpt.com/backend-api/wham/usage` | 额度接口（接口变更时用） |

选号规则：候选必须自身额度可用（`allowed=true` 且未用尽），按剩余 5h → 剩余 7d → `priority` → 保存时间排序。全池没额度进入 `no_candidate`：最近重置在 15 分钟内则等重置后 5 秒重试，否则每 5 分钟重试。

**重置券**

| 变量 | 默认 | 说明 |
|---|---:|---|
| `AUTO_RESET_SECONDARY_THRESHOLD` | 98 | 7 天窗口触发阈值（%） |
| `AUTO_RESET_MIN_WAIT_HOURS` | 72 | 7 天窗口剩余多少小时内会自然重置就不烧券 |
| `AUTO_RESET_MAX_PER_WEEK` | 1 | 每 7 天最多自动用几张 |
| `AUTO_RESET_BEFORE_SWITCH` | 0 | 先烧券再换号（不推荐） |

自动用券的条件：`limit_reached=true` + 池内无候选 + 7d ≥98% + 3 天内不自然重置 + 有券且未超周上限。`GET /api/auto-switch/status` 的 `auto_reset.gate` 会给出没动手的原因：`disabled` / `below_threshold` / `window_resets_soon` / `no_credit` / `weekly_cap` / `unknown`。

上游：`GET /backend-api/wham/rate-limit-reset-credits` 列券，`POST .../consume` 带 `{"credit_id","redeem_request_id"}`（后者是幂等 UUID）。失败码 `nothing_to_reset` / `no_credit` / `already_redeemed`。

**登录助手**

| 变量 | 默认 | 说明 |
|---|---:|---|
| `LOGIN_DRIVER` | `manual` | `manual` / `playwright` |
| `LOGIN_CONCURRENCY` | 2 | 同时登录数（1–4） |
| `AUTO_LOGIN_ON_IMPORT` | 仅 playwright 默认开 | 导入后自动排队取 auth |

**目标续跑**

| 变量 | 默认 | 说明 |
|---|---:|---|
| `GOAL_RESUME_MAX_RUNTIME` | 7200 | 单任务最长运行秒数 |
| `GOAL_RESUME_START_TIMEOUT` | 12 | 等自动回合的秒数，超时发「继续」 |
| `GOAL_RESUME_TRACE` | 关 | `1` = 打印每次等到的通知名与剩余时间 |

**接码**（也兼容无前缀的 `SMS_PROVIDER`、`SMS_API_KEY`、`L_API_BASE` 等）

| 变量 | 说明 |
|---|---|
| `SMS_PROVIDER` | `grizzly` / `smsbower` / `l` / `h`；不设 = 关闭 |
| `SMS_SERVICE` / `SMS_COUNTRY` / `SMS_MAX_PRICE` | 服务码（默认 `dr`）/ 国家码（默认 `10`）/ 单号最高价 |
| `SMS_CODE_WAIT` / `SMS_POLL_INTERVAL` / `SMS_MAX_RETRIES` | 等码 120s / 轮询 5s / 最多换 10 个号 |
| `SMS_API_BASE` + `SMS_API_KEY` | GrizzlySMS handler_api |
| `SMSBOWER_API_BASE` + `SMSBOWER_API_KEY` | SMSBower（`SMSBOWER_USE_V2=1` 用 getNumberV2） |
| `L_API_BASE` + `L_ADMIN_AUTH_CODE` | 本地 L 服务，默认 `http://localhost:8788` |
| `H_API_BASE` + `H_ADMIN_AUTH_CODE` | 本地 H 服务（`H_PHONE_ACQUIRE_MODE=new` 用一次性号） |

## 导入格式

```text
account@example.com|your-password|JBSWY3DPEHPK3PXP
account@example.com-----your-password---JBSWY3DPEHPK3PXP
```

- `#` 开头忽略；无 2FA 只写两段。
- 短横线数量不固定：从右往左判断，最后一段像 base32 种子（≥16 位）就当 2FA，前面整段是密码。
- 密码像 base32、或连续短横线无法区分时，改用 `|`。
- 非 base32 的 2FA（恢复码）能导入但标「非标准」，自动登录可能要人工处理。
- 同邮箱重复导入是更新；API 从不返回密码和 TOTP 明文。

## 数据目录

`%LOCALAPPDATA%\CodexSwitcher\`：

| 文件 | 内容 |
|---|---|
| `accounts.sqlite3` | 账号池 + 加密的 `auth_snapshots`（密码 / TOTP / token 全是 DPAPI 密文） |
| `backups\` | 每次切号前的加密备份 |
| `settings.json` | 设置面板的值（密钥字段 DPAPI 加密） |
| `auto-switch.log` | 自动切号事件（只记事件和邮箱，不记 token） |
| `login-history.json` | 登录记录（最近 50 条，不写设备码和 CLI 输出） |
| `goal-resume-history.jsonl` | 续跑历史（200 条） |
| `login-sessions\` | 每次登录的隔离 `CODEX_HOME`、`cli.log`、`driver.log`、失败截图 |

## API

服务只监听 `127.0.0.1`，全部 JSON，带 `Access-Control-Allow-Origin: *` 供扩展调用。

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
| POST | `/api/goals/resume` | `{"thread_ids":[...],"dry_run":true}`；真实续跑需 `dry_run:false` + `confirm:true`；`restart_desktop:true` 先重启桌面端 |
| POST | `/api/goals/auto-resume` | `{"thread_id":"...","enabled":true}` 加入/移出切号后自动续跑 |
| POST | `/api/goals/cancel` | `{"confirm":true}` 取消运行中的续跑 |
| GET | `/api/sms/status` | 接码面板状态；`?balance=1` 跳过 30s 缓存 |
| POST | `/api/sms/test` | 取一个号做连通性测试，随后释放退款 |
| POST | `/api/sms/cancel` | `{"confirm":true,"activation_id":"?"}` 释放号码并退款 |
| GET | `/api/extension/status` | 扩展版本 / 派生 ID / Chrome 路径 / 各 profile 是否已装 |
| POST | `/api/extension/install` | 把扩展目录写进剪贴板（`target:"url"` 复制扩展页网址）；不启动浏览器 |
| GET | `/api/settings` | 54 项的 `key/group/label/type/value/source/secret/restart` |
| POST | `/api/settings` | `{"confirm":true,"values":{...},"clear":[...]}` → `changed` / `cleared` / `restart_required` |
| GET | `/api/accounts` | 账号列表（无密码 / TOTP）+ `login_concurrency` |
| GET | `/api/snapshots` | 当前 auth 公共信息 + 快照列表 |
| GET | `/api/config` | 版本、路径、端口、各开关、CLI 路径 |
| GET | `/api/login/status` | 登录开关、驱动、`concurrency`、会话（无设备码 / CLI 输出） |
| GET | `/api/login/session/<id>` | 单个登录会话 |
| POST | `/api/accounts/import` | `{"text":"..."}` 批量导入 |
| POST | `/api/accounts/add` | `{"email","password?","totp?","tags?","priority?","note?","disabled?"}` |
| POST | `/api/accounts/update` | 改元数据 / 轮换密码或 2FA / 停用（空值表示不改） |
| POST | `/api/accounts/delete` | `{"email":"...","confirm":true}` 删账号 + 快照 + 采样，不动 auth.json |
| POST | `/api/accounts/reorder` | `{"emails":["从上到下"]}` → `priority` 100、99、98… |
| POST | `/api/switching/toggle` | `{"key":"enable_switch"\|"enable_auto_switch","value":true,"confirm":true}` |
| POST | `/api/switch` | `{"email":"...","confirm":true}`；未启用返回 `423 switching_disabled` |
| POST | `/api/login/start` | `{"email":"..."}`；默认禁用时返回 `423 login_disabled` |
| POST | `/api/login/cancel` | `{"session_id":"..."}` |

缺 `confirm=true` 或目标没有快照时返回 `4xx`，不会改动 auth。

## 已知限制

- 只支持 Windows：DPAPI、托盘、`start.ps1` 都依赖它。
- 额度接口是 ChatGPT / Codex 用的非公开接口，字段或路径可能变；变化时 `/api/quota` 返回错误而不是崩。
- 切号只换本地 `auth.json`。refresh token 被撤销或账号额度用尽时，切过去在 Codex 侧也会失败。
- 2FA 只支持 TOTP；邮箱验证码需要人工。
- 扩展只支持 Chromium（`chrome.sidePanel` 需 114+），Firefox 没有等价 API；徽章靠 `chrome.alarms` 每分钟轮询。
- 旧油猴面板已退役到 `legacy/`，Tampermonkey 里还留着请关掉，否则多一个浮窗。
- 自动切号可能在你有会话时替换 `auth.json`，只建议在可中断的场景开。

## 开发

```powershell
python -m unittest discover -s tests            # 全量（285 个）
python -m unittest discover -s tests -p "test_quota_reset.py" -v
```

测试会临时改写 auth 路径，并在结束时校验正式 `auth.json` 的 SHA-256 不变。

```text
service.py              # 入口
start.ps1 / stop.ps1    # 起停
tray.ps1                # 托盘 + Ctrl+Alt+C
restart-dev.bat         # 本地开发：停服 + 带 env 重启 + 健康检查
codex_switcher/
  api.py                # 本地 HTTP API
  dashboard.html        # Dashboard（单文件）
  accounts.py           # 账号解析 / 脱敏 / TOTP
  auth_state.py         # 只读解析 auth.json
  config.py             # 路径、端口、开关
  dpapi.py              # Windows DPAPI
  store.py              # SQLite 账号池 + 加密快照
  switcher.py           # 备份 / 原子写 / 校验 / 回滚
  quota.py              # 只读额度 + 重置券
  auto_switch.py        # 自动切号策略
  login.py              # 设备码登录 + Playwright + 接码
  goal_resume.py        # 目标续跑
  settings.py           # 54 项配置
  extension_install.py  # 扩展安装助手
extension/              # MV3 扩展（侧边栏 + 徽章）
legacy/                 # 已退役的油猴面板
tests/                  # unittest
```
