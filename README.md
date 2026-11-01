# Codex Switcher

Windows 本地 Codex 多账号管理工具，支持额度监控、自动切换账号和任务续跑。

- **账号管理**：批量导入、设备码登录、加密保存凭据快照。
- **额度监控**：查看 5 小时与 7 天额度、历史走势和耗尽预测。
- **自动切换**：额度用尽后优先选择剩余额度最多的可用账号。
- **任务续跑**：手动恢复因额度中断的任务，或为指定任务启用切换后自动续跑。

![Dashboard](docs/dashboard.png)

## 快速开始

需要 Windows 10/11、Python 3.10+ 和可用的 Codex CLI。基础服务仅依赖 Python 标准库。

```powershell
git clone https://github.com/Kajiekazz/codex-switcher.git
cd codex-switcher
.\start.ps1 -EnableLogin -EnableSwitch
```

打开 [本地控制台](http://127.0.0.1:8787/)：

1. 在账号池中按 `邮箱|密码|2FA` 格式导入账号。
2. 点击账号行的「登录」，完成设备码授权，生成加密凭据快照。
3. 选择账号并确认切换，按提示重启 Codex 桌面端。

自动切换、任务续跑、Playwright 登录及托盘操作见[使用指南](docs/usage.md)。

## 详细文档

| 文档 | 内容 |
|---|---|
| [使用指南](docs/usage.md) | 账号导入与登录、手动与自动切换、任务续跑、重置券 |
| [配置与数据](docs/reference.md) | 启动参数、设置优先级、环境变量、数据存储与安全 |
| [浏览器扩展](extension/README.md) | Chrome 侧边栏安装、工具栏徽章与更新 |
| [故障排查](docs/troubleshooting.md) | 服务、登录、额度、切换与续跑问题 |
| [API 参考](docs/api.md) | 本地 HTTP 接口、请求格式与确认要求 |
| [开发指南](docs/development.md) | 源码结构与测试命令 |
| [更新日志](CHANGELOG.md) | 版本变更 |

服务默认监听 `127.0.0.1:8787`。账号凭据与快照使用 Windows DPAPI 加密，切换前备份当前 `~/.codex/auth.json`，写入失败时尝试回滚并报告结果。详见[配置与数据](docs/reference.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
