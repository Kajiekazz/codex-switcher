# 开发指南

[返回首页](../README.md) · [使用指南](usage.md) · [配置与数据](reference.md) · [API](api.md) · [常见问题](troubleshooting.md)

## 本地运行

基础服务使用 Python 标准库，依赖 Windows DPAPI。登录和任务续跑需要可用的 Codex CLI；Playwright 登录驱动需要额外安装 Playwright 和 Chrome。

在仓库根目录运行：

```powershell
python service.py
```

未配置时，切换、自动切换、登录和目标续跑默认关闭。需要对应功能时使用 `start.ps1` 显式启用，见[配置参考](reference.md#启动参数)。控制台 HTML 在请求时读取，修改 `dashboard.html` 后刷新页面即可；修改服务端 Python 代码后重启服务。

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

会话管理页面的行为回归使用 Node.js 内置测试工具，不需要安装 npm 依赖：

```powershell
node --test tests/test_session_ui.js
```

按修改范围运行相关文件，例如重置券或续跑接管逻辑：

```powershell
python -m unittest discover -s tests -p "test_quota_reset.py" -v
python -m unittest discover -s tests -p "test_resume_takeover.py" -v
```

现有切换和 API 测试使用临时路径、伪凭据及测试替身验证文件操作和上游交互。新增涉及账号、登录或续跑的测试时，沿用隔离目录和凭据路径的方式，不依赖个人账号。部分哈希校验针对测试凭据文件，不能据此声称所有测试均验证真实凭据未变。

## 目录与模块

| 文件或目录 | 职责 |
|---|---|
| `service.py` | 服务入口 |
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
| `codex_switcher/goals.py` / `goal_resume.py` | 会话与目标读取、续跑、接管、历史 |
| `codex_switcher/desktop.py` | 桌面客户端检测、重启与接管辅助 |
| `codex_switcher/extension_install.py` | 扩展检测与路径复制 |
| `extension/` | Manifest V3 扩展、侧边栏、后台徽章及图标 |
| `legacy/` | 已归档的 Tampermonkey 面板 |
| `tests/` | `unittest` 测试 |

修改扩展后在 `chrome://extensions` 重新加载，详见[扩展说明](../extension/README.md)。调整路由时同步更新 [API 参考](api.md)；调整配置时核对 `config.py`、`settings.SPEC` 和[配置参考](reference.md)的默认值与生效方式。

## 本地数据

凭据库、日志、登录截图和浏览器数据不属于源代码。`.gitignore` 排除了 `data/`、`work/`、`shots/`、日志、SQLite、JSONL 等文件；提交前仍需检查暂存内容，尤其是自行生成的 JSON、截图和导出文件。存储格式及 DPAPI 边界见[数据目录说明](reference.md#数据目录与安全边界)。
