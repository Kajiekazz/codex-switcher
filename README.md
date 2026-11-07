# Codex Switcher

面向 Windows 版 Codex 桌面端的本地多账号管理工具，支持额度监控、自动切换账号和会话续跑。

- **同设备多账号切换**：绕过 Codex 桌面端同一设备的账号数量限制，集中管理多个账号，复用登录快照快速切换。
- **额度监控**：按账号实际额度窗口显示用量、历史走势和耗尽预测。
- **Team 工作区**：浏览器授权，同一邮箱的个人空间与多个工作区独立管理。
- **自动切换**：检测可用额度与续跑模型，同套餐优先选择剩余额度更多的账号。
- **桌面端续跑**：支持单次与持续续跑，自动补建长期目标，集中管理队列；持续续跑默认开启该会话的切号自动恢复。

账号凭据可与使用同一份 `auth.json` 的 Codex CLI 共用。**会话续跑、切号自动恢复和强制停止目前适配桌面端，尚未接管终端中运行的 CLI。** 详见[支持范围](docs/usage.md#支持范围)。

![Codex Switcher 控制台：账号池、额度与自动切号](docs/dashboard.png)

<details>
<summary>查看续跑队列</summary>

![续跑队列：执行状态、切号自动恢复与会话操作](docs/resume-queue.png)

</details>

截图使用示例账号与会话。

## 快速开始

会话续跑需要已安装并运行 Windows 版 Codex 桌面端，Switcher 安装包不包含 Codex。

从 [GitHub Actions](https://github.com/Kajiekazz/codex-switcher/actions/workflows/build-windows.yml) 的成功构建中下载对应架构的安装包；正式版本见 [Releases](https://github.com/Kajiekazz/codex-switcher/releases)。

1. 运行 `CodexSwitcher-版本-windows-x64-Setup.exe`；ARM 设备选择 `arm64`。
2. 双击桌面上的 **Codex Switcher**，按引导检测 Codex、准备登录组件、导入账号并完成授权。
3. 引导自动检测本地已登录账号，默认勾选后在下一步导入。自动登录组件仅下载一次；手动授权或仅导入本地账号可跳过下载。无需另装 Python、Node 或 Git。

关闭网页后后台继续运行。托盘右键可打开管理页、设置开机自启和退出；开启自启后自动启动 Codex，恢复已标记的未完成任务。设置里提供同一个自启开关，账号与配置在覆盖升级后保留。

<details>
<summary>从源码启动</summary>

需要 Windows、Python 3.10+ 和可用的 Codex CLI；可自动检测桌面端随附的 CLI。CLI 用于登录及本地接口调用，会话续跑仍由桌面端执行。基础服务仅依赖 Python 标准库。

```powershell
git clone https://github.com/Kajiekazz/codex-switcher.git
cd codex-switcher
python -m pip install -r packaging/requirements-build.txt
pythonw switcher.pyw
```

打开 [本地控制台](http://127.0.0.1:8787/)：

1. 在账号池中按 `邮箱|密码` 或 `邮箱|密码|TOTP密钥` 格式导入账号。
2. 在登录进度弹窗完成授权；手动模式点击账号行的「登录」，成功后账号即可切换。
3. 选择账号并确认切换，按提示重启 Codex 桌面端。

自动切换、任务续跑、Playwright 登录及托盘操作见[使用指南](docs/usage.md)。

源码版与安装版使用同一套原生托盘，功能开关在页面设置中调整；退出使用托盘菜单或 `pythonw switcher.pyw --exit`。前台调试见[开发指南](docs/development.md)。

</details>

## 详细文档

| 文档 | 内容 |
|---|---|
| [使用指南](docs/usage.md) | 账号导入与登录、手动与自动切换、任务续跑、重置券 |
| [配置与数据](docs/reference.md) | 启动参数、设置优先级、环境变量、数据存储与安全 |
| [浏览器扩展](extension/README.md) | Chrome 侧边栏安装、工具栏徽章与更新 |
| [故障排查](docs/troubleshooting.md) | 服务、登录、额度、切换与续跑问题 |
| [API 参考](docs/api.md) | 本地 HTTP 接口、请求格式与确认要求 |
| [开发指南](docs/development.md) | 源码结构与测试命令 |
| [打包与发布](docs/packaging.md) | Windows 安装包、x64 / ARM64 构建与发布流程 |
| [更新日志](CHANGELOG.md) | 版本变更 |

服务默认监听 `127.0.0.1:8787`。账号凭据与快照使用 Windows DPAPI 加密，切换前备份当前 `~/.codex/auth.json`，写入失败时尝试回滚并报告结果。详见[配置与数据](docs/reference.md)。

## 许可

MIT，见 [LICENSE](LICENSE)。
