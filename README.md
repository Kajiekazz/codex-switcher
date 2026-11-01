# Codex Switcher

**本地 Codex 多账号助手**：一个跑在 `127.0.0.1:8787` 的小服务 + 一个 Chrome 侧边栏。管账号池、看额度、额度用完自动换号、换完接着跑。凭据用 Windows DPAPI 加密，只留在本机。

![Dashboard](docs/dashboard.png)

<p align="center"><sub>Dashboard：额度、账号池、自动切号状态，5 秒刷新</sub></p>

<p align="center"><img src="docs/sidepanel.png" width="330" alt="Chrome 侧边栏"><br><sub>Chrome 侧边栏：切号、导入、额度一眼看完</sub></p>

## 30 秒判断

适合你，如果：

- 手上有 2 个以上 Codex / ChatGPT 账号，轮流被 5 小时额度打断；
- 想让「额度用完 → 换号 → 重启桌面端」这套动作自动完成；
- 不想把密码交给云端面板或来路不明的插件。

它核心只碰一样东西：本机的 `~/.codex/auth.json`。不买额度、不改套餐、不绕过任何服务端限制。

## 快速开始

Windows 10/11 + Python 3.10+，**不需要 `pip install` 任何东西**（只有自动登录要 Playwright）。

```powershell
git clone https://github.com/Kajiekazz/codex-switcher.git
cd codex-switcher
.\start.ps1 -EnableSwitch
```

被执行策略挡住时用 `powershell -ExecutionPolicy Bypass -File .\start.ps1 -EnableSwitch`。

打开 <http://127.0.0.1:8787/>，然后三步：

1. **导入账号** —— 账号池 → 粘贴 `邮箱|密码|2FA`（短横线分隔也行）
2. **取 auth 快照** —— 账号行右侧「登录」→ 完成设备码授权
3. **切号** —— 选目标 → 勾选确认 → 切换

> 导入 ≠ 能切。切号用的是登录时存下的加密 `auth.json` 快照，没有快照的号会被跳过。

## 常用四件事

### 1. 额度用完自动换号

```powershell
.\start.ps1 -EnableSwitch -EnableAutoSwitch
```

每 90 秒查一次额度，只有 `limit_reached` 才动手。整池并行探测候选，**按剩余额度挑号**（5h 优先、其次 7d；`priority` 只在额度相同时决胜）。切走的号和用尽的号各自冷却到自己的重置时间，每小时最多 3 次，连续 3 次失败熔断 30 分钟。

### 2. 重置券（默认不动）

额度接口里的「Full reset (Weekly + 5 hr)」券会显示在额度卡片，可以手动用（二次确认，不可撤回）。

自动用券默认关闭，开了也只当**兜底**：池里换不到可用号、7 天窗口已用 ≥98%、且 3 天内不会自然重置时，才烧一张，每 7 天最多 1 张。四个数值都在设置面板里改。

```powershell
.\start.ps1 -EnableSwitch -EnableAutoSwitch -EnableAutoReset
```

### 3. 额度恢复后接着干活

会话卡片里勾选被额度打断的 `usage_limited` 目标 → 预览（不改状态）→ 续跑。行上的「自动」按钮把这个目标设成**每次切号后自动续跑**。一次只跑一个任务，随时能停。

### 4. 浏览器侧边栏

Dashboard →「浏览器扩展」→ 复制路径 → `chrome://extensions` → 开发者模式 → 加载已解压的扩展程序。

工具栏图标上的数字就是当前账号 5 小时**剩余**百分比：绿 ≥50% / 琥珀 20–49% / 红 <20% / `!` 服务没起 / `?` 额度读取失败。

## 三个必须知道的坑

1. **切号后要完全重启 Codex 桌面端**（含托盘和后台 `codex` 进程）。桌面端只在启动时读 `auth.json`，只关窗口会继续显示旧账号。
2. **写操作默认全关**。没加 `-EnableSwitch` 时切号返回 `423 switching_disabled`；自动切号、登录助手、目标续跑、自动用券各要一个开关。
3. **改环境变量后要重启服务**（`.\stop.ps1` 再启动）。设置面板里改的项是例外，保存即生效。

## 还内置了

- **登录助手**：手动设备码，或 Playwright 自动填邮箱 / 密码 / TOTP / 设备码；批量导入可并发 2 个。
- **手机接码**：Grizzly / SMSBower / 本地 L、H 服务；虚拟号被静默改成 WhatsApp 会立刻释放退款。
- **额度走势与耗尽预测**：最近 6 小时曲线 + 按当前速度估算用完时间。
- **托盘 + `Ctrl+Alt+C`**：不用开浏览器也能看状态。

## 数据与安全

- 账号、密码、TOTP、`auth.json` 快照全部 DPAPI 加密存在 `%LOCALAPPDATA%\CodexSwitcher\`，HTTP API 不返回明文；
- 服务只监听 `127.0.0.1`；切号前强制加密备份 → 原子替换 → 写后校验 → 失败回滚；
- DPAPI 绑定当前 Windows 用户：把数据目录拷到别的机器或别的用户下解不开，这是预期行为；
- `.gitignore` 已排除 `data/`、`work/`、`*.log`、`*.sqlite3`、`*.jsonl`。别把账号库和日志提交上来。

## 更多

| 想知道 | 看 |
|---|---|
| 54 个设置项、环境变量、数据目录、完整 API | [docs/reference.md](docs/reference.md) |
| 排障：徽章、续跑、接码、扩展装不上 | [docs/troubleshooting.md](docs/troubleshooting.md) |
| 每个版本改了什么 | [CHANGELOG.md](CHANGELOG.md) |

## 许可

MIT，见 [LICENSE](LICENSE)。
