# 排障

按现象查。用到的接口都能在浏览器里直接打开（`http://127.0.0.1:8787/api/...`）。

## 服务与 Dashboard

**侧边栏显示「先运行 python service.py」，徽章是 `!`**
服务没起来。`.\start.ps1 -EnableSwitch`，或者看 `http://127.0.0.1:8787/api/health` 是否有响应。端口被占用时先 `.\stop.ps1`。

**切号按钮点了没反应 / 返回 `423 switching_disabled`**
没开 `-EnableSwitch`。自动切号还要额外的 `-EnableAutoSwitch`；开了自动切号但关掉「允许切号」，监控会停着不动。

**改了环境变量但行为没变**
服务只在启动时读 env。`.\stop.ps1` 再启动。设置面板里保存的值优先于 env，想回落到 env 要先在面板里「清除」。

**Dashboard 显示 `token 已过期`**
指该账号快照里 access token 的 `exp` 已过。切号写的是整份 `auth.json`，Codex 会用 refresh token 自动续期，不影响切换；用登录助手重登一次就会刷新这个标记。

**徽章数字比 Dashboard 慢一拍**
徽章靠 `chrome.alarms` 每分钟轮询，最多 1 分钟延迟。点侧边栏「刷新」会立刻同步。

## 账号与切号

**「导入成功」但还是不能切**
导入只存邮箱 / 密码 / TOTP；能切的是登录时落下的加密 `auth.json` 快照。账号行右侧点「登录」跑一次设备码流程。

**切号报「没有可用快照」**
同一条：没有快照，或快照已被判定过期 / 撤销。重登一次。

**切号成功，桌面端还是旧账号**
桌面端只在启动时读 `auth.json`。要**完全退出**（含托盘图标、后台 `codex` 进程）再启动；只关窗口不够。验证：左下角头像 → 个人资料。

**自动切号一直 `no_candidate`**
池里其它账号的额度也用尽了，或它们的快照 token 过期导致探测 401。看 `GET /api/auto-switch/status` 的候选与冷却列表；用登录助手重登那些号。

**自动切号不触发**
必须同时开 `ENABLE_SWITCH` + `ENABLE_AUTO_SWITCH`，且只有 `limit_reached=true` 或 `allowed=false` 才触发。额度正常时它什么也不做，这是设计。

**选号没用我设的高优先级账号**
优先级只在**剩余额度相同**时决胜。有额度的号永远排在没额度的号前面；想让它先被用，就别让它先耗尽。

## 登录助手

**手动模式卡在「等待浏览器」**
设备码有时效。让弹窗重新生成一次，用新码在浏览器里走完「选账户 → 同意 → 输入设备码」。

**`device_code_login_disabled`**
账号没开「为 Codex、Excel、PowerPoint 和 Word 启用设备代码登录」。自动模式一般能自己打开 <https://chatgpt.com/settings/security> 里的开关；打不开就手动开一次再重试。切号器不会静默改你的账号安全设置。

**Playwright 模式总是停在「糟糕，出错了！」**
Chrome 窗口被遮挡时 Sentinel token 会卡住。保持窗口前台可见，别最小化；手动模式下没有这个问题。

**Playwright 找不到浏览器**
需要 `pip install playwright`；本项目用系统已装的 Chrome（`channel="chrome"`），不需要 `playwright install`。

**并发登录互相抢焦点**
`login_concurrency` 默认 2，>2 时多个 Chrome 会抢前台，Cloudflare / Sentinel 失败率上升。手动模式建议设 1。

## 接码

**`sms_not_configured`**
没配接码平台。设 `CODEX_SWITCHER_SMS_PROVIDER`（`grizzly` / `smsbower` / `l` / `h`）和对应 API key，或在设置面板「接码」分组里填。

**收不到短信，页面写着 WhatsApp**
OpenAI 把虚拟号静默改成 WhatsApp 发码了。驱动会立刻判失败并释放号码（不白等 120 秒），连续 2 次报 `sms_number_rejected`。换国家码，实测 **187（美国）** 能真收短信；越南、英国都被改道。

**余额显示 0 / 取号测试失败**
看「接码」卡片的事件日志（本机不脱敏，直接给原因）。Grizzly 前 2 分钟不能取消，取号测试会自动延后释放。

## 目标续跑

**`thread/resume: ... already has an active writer`**
桌面端或另一个 `codex app-server` 正持有该线程，**不是额度问题**。卡片顶部会提示，点它会先重启桌面端再续跑（`restart_desktop: true`）。

**续跑刚起就结束，状态还是 `usage_limited`**
先升级到 0.6.0：旧版本会把通知缓冲区里上一次会话残留的 `thread/goal/updated` 当成「目标结束」，回合刚起就被中断。0.6.0 在等待前清空历史通知，终止状态只认 `thread/goal/get`。

**续跑一直没动静**
`goal_resume_start_timeout`（默认 12 秒）内没有自动回合时，服务会自己发一句「继续」。仍然不动就把 `CODEX_SWITCHER_GOAL_RESUME_TRACE=1` 打开重启，任务事件里会打印每次等到的通知名与剩余时间。

**`complete` / `blocked` 打不了勾**
设计如此：已完成或被阻塞的目标不需要续跑。`active` 但实际没有回合在跑的「假 active」是可以勾的。

**余额够但还是失败**
检查 `~/.codex/auth.json` 当前是谁。续跑用的是实时 auth，切号后新任务才会用新账号。

## 浏览器扩展

**扩展装上了但侧边栏打不开**
点地址栏右侧的拼图图标 🧩 把它钉住，再点图标。`chrome.sidePanel` 需要 Chrome 114+。

**`--load-extension` 命令行无效**
Chromium 137+ 稳定版移除了这个开关，只影响脚本化加载；「加载已解压的扩展程序」不受影响。

**扩展 ID 会变吗**
不会。ID 由扩展目录路径派生，当前固定为 `ccpnehmimijbfhcjonkbgbimhjpohfkj`；挪动目录才会变。

**为什么不能一键帮我打开 `chrome://extensions`**
Chrome 会丢弃命令行传入的 `chrome://` 网址（实测 156 只开空白标签页）。安装卡片只把路径复制到剪贴板，不假装打开。

**改了扩展代码没生效**
`extension/` 是唯一源文件，改完在 `chrome://extensions` 点该扩展的刷新按钮。manifest 版本变了也要刷新一次。

## 其它

**托盘快捷键冲突**
`Ctrl+Alt+C` 写在 `tray.ps1` 里，改那一行。

**把数据目录拷到另一台机器后全空了**
DPAPI 绑定当前 Windows 用户，密文换机解不开。重新导入账号、重新登录。

**测试会不会动我的真实 auth.json**
不会。测试全程用临时路径，并在结束时校验真实 `auth.json` 的 SHA-256 未变。
