# Codex Switcher

Windows 本地 Codex 多账号管理工具，支持额度监控、自动切换账号和中断任务续跑。

- **账号管理**：批量导入、设备码登录、加密保存凭据快照。
- **额度监控**：查看 5 小时与 7 天额度、历史走势和耗尽预测。
- **自动切换**：额度用尽后优先选择剩余额度最多的可用账号。
- **任务续跑**：手动恢复因额度中断的任务，或为指定任务启用切换后自动续跑。
- **操作入口**：Web 控制台、Chrome 侧边栏和 Windows 托盘。

![Dashboard](docs/dashboard.png)

## 快速开始

环境要求：Windows 10/11、Python 3.10+。登录与任务续跑需要可用的 Codex CLI；基础服务无需安装第三方 Python 依赖。

```powershell
git clone https://github.com/Kajiekazz/codex-switcher.git
cd codex-switcher
.\start.ps1 -EnableLogin -EnableSwitch
```

打开 [本地控制台](http://127.0.0.1:8787/)：

1. 在账号池中按 `邮箱|密码|2FA` 格式导入账号。
2. 点击账号行的「登录」，完成设备码授权，生成加密凭据快照。
3. 选择目标账号，勾选确认后切换；点击「立即重启桌面端」使新账号生效。

> 切换更新本机 `~/.codex/auth.json`，目标账号需先完成登录并保存快照。

## 自动切换与可选功能

启用额度耗尽后自动切换，并在切换成功后重启桌面端：

```powershell
.\start.ps1 -EnableSwitch -EnableAutoSwitch -RestartDesktop
```

默认每 90 秒检查额度，按剩余 5 小时额度、7 天额度依次选择候选账号。其他功能可在启动命令中追加参数：

| 参数 | 功能 |
|---|---|
| `-EnableLogin` | 启用登录助手，默认使用手动设备码授权 |
| `-EnableGoalResume` | 启用任务续跑；在会话卡片预览并续跑，或点击「自动」指定切换后续跑的任务 |
| `-Driver playwright` | 配合 `-EnableLogin` 自动填写登录信息，需安装 Playwright 和 Chrome |
| `-Tray` | 后台运行并显示托盘，支持 `Ctrl+Alt+C` 快捷键 |
| `-EnableAutoReset` | 配合自动切换，在无可用候选账号且满足条件时消耗重置券；默认关闭，消耗不可撤回 |

重新启动前先运行 `.\stop.ps1`。设置面板保存的值优先于启动参数；配置优先级与生效方式见[配置参考](docs/reference.md)。

Chrome 侧边栏：在 `chrome://extensions` 开启开发者模式，加载仓库中的 `extension/` 目录。详见[扩展说明](extension/README.md)。

## 数据与安全

- 默认监听 `127.0.0.1:8787`，数据保存在 `%LOCALAPPDATA%\CodexSwitcher\`。
- 账号凭据与快照使用 Windows DPAPI 加密，绑定当前 Windows 用户，不能直接跨用户或机器迁移。
- 切换前加密备份当前凭据，写入时执行原子替换与校验，失败自动回滚。

## 文档

- [配置、数据与 API](docs/reference.md)
- [常见问题](docs/troubleshooting.md)
- [更新日志](CHANGELOG.md)

## 许可

MIT，见 [LICENSE](LICENSE)。
