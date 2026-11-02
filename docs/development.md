# 开发指南

[返回首页](../README.md) · [使用指南](usage.md) · [配置与数据](reference.md) · [API](api.md) · [常见问题](troubleshooting.md)

## 本地运行

基础服务使用 Python 标准库，依赖 Windows DPAPI。登录和任务续跑需要可用的 Codex CLI；Playwright 登录驱动需要额外安装 Playwright 和 Chrome。

安装版和 `switcher.pyw` 提供原生托盘与后台服务生命周期，启动后用默认浏览器打开原有页面。打包、原生架构校验与 GitHub Actions 见[打包与发布](packaging.md)。

在仓库根目录运行：

```powershell
python service.py
```

手动切换和继续一次始终可用；自动切换、登录默认关闭，需要时使用 `start.ps1` 显式启用，见[配置参考](reference.md#启动参数)。切号自动恢复按会话开启，统一暂停开关位于设置。控制台 HTML 在请求时读取，修改 `dashboard.html` 后刷新页面即可；修改服务端 Python 代码后重启服务。

`start.ps1` 使用 `service-launch.ps1` 在独立隐藏进程中运行服务，窗口只等待并报告退出码。关闭启动窗口不会停止服务；使用 `stop.ps1` 停止。标准输出和错误分别写入仓库根目录 `service.out.log`、`service.err.log`，启动 PID 和退出码记录在 `service-launcher.log`。直接运行 `python service.py` 仍为前台调试方式。

开发重启脚本：

```powershell
.\restart-dev.bat
.\restart-dev.bat trace
```

该脚本停止现有服务，启用登录、切换、自动切换、桌面端重启和目标续跑，使用 Playwright 驱动。`trace` 额外启用续跑通知追踪。服务在隐藏窗口运行，输出写入仓库根目录的 `service.out.log`、`service.err.log`。脚本按固定端口 `8787` 检查健康状态；已保存的设置仍优先于脚本环境变量。

## 测试

运行全部单元测试：

```powershell
python -m unittest discover -s tests
```

会话管理和重置券页面的行为回归使用 Node.js 内置测试工具，不需要安装 npm 依赖：

```powershell
node --test tests/test_session_ui.js tests/test_quota_ui.js tests/test_switch_ui.js tests/test_dashboard_polling.js tests/test_login_ui.js tests/test_setup_ui.js
```

按修改范围运行相关文件，例如重置券或续跑投递逻辑：

```powershell
python -m unittest discover -s tests -p "test_quota_reset.py" -v
python -m unittest discover -s tests -p "test_resume_queue.py" -v
```

现有切换和 API 测试使用临时路径、伪凭据及测试替身验证文件操作和上游交互。新增涉及账号、登录或续跑的测试时，沿用隔离目录和凭据路径的方式，不依赖个人账号。部分哈希校验针对测试凭据文件，不能据此声称所有测试均验证真实凭据未变。

## 目录与模块

| 文件或目录 | 职责 |
|---|---|
| `service.py` | 服务入口 |
| `switcher.pyw` / `application.py` / `windows_ui.py` | 原生启动器、单实例、后台服务与托盘 |
| `onboarding.py` / `startup.py` / `components.py` | 首次引导、当前用户自启、按需下载浏览器 |
| `packaging/` / `.github/workflows/` | 安装包构建、架构校验和自动发布 |
| `start.ps1` / `stop.ps1` | 启动参数、环境变量与进程停止 |
| `tray.ps1` | Windows 托盘与快捷键 |
| `restart-dev.bat` | 开发重启与健康检查 |
| `codex_switcher/api.py` | HTTP 路由与服务组装 |
| `codex_switcher/dashboard.html` | Web 控制台、样式与交互 |
| `codex_switcher/accounts.py` | 导入解析、邮箱脱敏、TOTP、元数据规范化 |
| `codex_switcher/auth_state.py` | 凭据公共身份信息解析 |
| `codex_switcher/config.py` | 路径、开关、默认值与配置优先级 |
| `codex_switcher/settings.py` / `ui_prefs.py` | 设置校验与持久化、共享主题 |
| `codex_switcher/dpapi.py` / `store.py` | DPAPI、SQLite 账号池、加密快照和额度采样 |
| `codex_switcher/switcher.py` | 凭据备份、原子替换、校验、回滚及切换后回调 |
| `codex_switcher/quota.py` / `trend.py` | 额度与重置券交互、缓存、消耗预测 |
| `codex_switcher/auto_switch.py` | 候选选择、冷却、限频、熔断与自动用券 |
| `codex_switcher/login.py` / `sms.py` | 登录驱动、登录队列、接码平台与号码生命周期 |
| `codex_switcher/appserver.py` | Codex app-server 进程与协议 |
| `codex_switcher/goals.py` / `goal_resume.py` | 会话与目标读取、续跑投递、历史与暂停 |
| `codex_switcher/resume_queue.py` | 按会话合并的续跑看板、目标后续回合状态 |
| `codex_switcher/desktop.py` | 桌面客户端检测与重启 |
| `codex_switcher/extension_install.py` | 扩展检测与路径复制 |
| `extension/` | Manifest V3 扩展、侧边栏、后台徽章及图标 |
| `legacy/` | 已归档的 Tampermonkey 面板 |
| `tests/` | `unittest` 测试 |

修改扩展后在 `chrome://extensions` 重新加载，详见[扩展说明](../extension/README.md)。调整路由时同步更新 [API 参考](api.md)；调整配置时核对 `config.py`、`settings.SPEC` 和[配置参考](reference.md)的默认值与生效方式。

## 本地数据

凭据库、日志、登录截图和浏览器数据不属于源代码。`.gitignore` 排除了 `data/`、`work/`、`shots/`、日志、SQLite、JSONL 等文件；提交前仍需检查暂存内容，尤其是自行生成的 JSON、截图和导出文件。存储格式及 DPAPI 边界见[数据目录说明](reference.md#数据目录与安全边界)。
