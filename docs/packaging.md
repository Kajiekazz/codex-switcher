# 打包与发布

[返回 README](../README.md) · [使用指南](usage.md) · [开发指南](development.md)

## 获取安装包

[Actions 构建列表](https://github.com/Kajiekazz/codex-switcher/actions/workflows/build-windows.yml) 提供 Windows x64 与 ARM64 安装包和 SHA-256 文件。打开成功的构建，下载对应架构的 Artifact 并解压，运行其中的 `Setup.exe`。正式版本从 [Releases](https://github.com/Kajiekazz/codex-switcher/releases) 下载。

安装包包含 Python、主程序和 Playwright 驱动；自动登录浏览器首次需要时下载，不随安装包分发。管理页使用系统默认浏览器。电脑仍需安装 Codex；可通过首次引导检测或选择 CLI 位置。

程序默认安装到当前用户的 `%LOCALAPPDATA%\Programs\Codex Switcher`，不申请管理员权限。账号、设置和组件保存在 `%LOCALAPPDATA%\CodexSwitcher`。安装与覆盖升级不删除此数据目录，卸载仅移除程序、快捷方式及自身自启注册。

## 本地构建

使用目标架构的 Windows 和 Python 3.14，以及 Inno Setup 7 编译器：

```powershell
python -m pip install -r packaging/requirements-build.txt
.\packaging\build.ps1 -Iscc 'C:\Program Files\Inno Setup 7\ISCC.exe'
```

`-Architecture x64` 或 `-Architecture arm64` 必须与 Python 的架构一致，不能通过改名将 x64 包作为 ARM64 发布。`-SkipInstaller` 只生成目录包。

输出：

- `dist/CodexSwitcher/`：可运行目录包。
- `dist/CodexSwitcher-版本-windows-架构-Setup.exe`：安装程序。
- 同名 `.sha256`：安装程序校验文件。

原生托盘、服务和启动检查由 `switcher.pyw` / `application.py` 提供。`components.py` 调用随包驱动的官方浏览器安装命令，只下载使用的 Chromium 版本；下载超时/失败需显式重试，多个登录任务共享一次下载。源码旧工作流仍可使用系统 Chrome。

## 自动构建

工作流 [build-windows.yml](../.github/workflows/build-windows.yml) 支持以下触发方式：

| 触发 | 结果 |
|---|---|
| `main` 相关代码变更 | 原生 x64、ARM64 构建和验证，保留 14 天的下载产物 |
| Pull Request | 同样构建和测试，不发布 Release |
| 手动 `Run workflow` | 对选定分支构建两种架构 |
| `v*` 标签 | 两个架构都成功后，将安装包及校验文件上传到对应 Release |

x64 使用 `windows-2025`，ARM64 使用 `windows-11-arm`，各自安装原生 Python、Playwright 和 PyInstaller。构建后读取 PE 头校验主程序、Python DLL、Playwright Node 驱动的机器类型；在清空开发工具 PATH 和空数据目录中验证启动、重复启动、引导和退出。CI 不使用个人账号，不会触发真实切号或续跑。

ARM64 包的应用、Python 和驱动为原生 ARM64。实际下载的浏览器由 Playwright 版本决定，Windows 上可能通过系统的 x64 兼容支持运行。Windows 10 的源码基础服务与安装版自动登录组件要求不同，浏览器系统要求以当前 Playwright 支持范围为准。

发布时先修改 `codex_switcher/__init__.py` 的版本，再创建同版本标签（例如 `v0.7.0`）。主分支构建只产生 Artifact，不自动创建版本标签。
