# 打包与发布

[返回 README](../README.md) · [使用指南](usage.md) · [开发指南](development.md)

## 获取安装包

从 [最新 Release](https://github.com/Kajiekazz/codex-switcher/releases/latest) 下载 Windows x64 或 ARM64 安装包，运行对应的 `Setup.exe`；每个安装包附带 SHA-256 校验文件。开发预览版可从 [Actions 构建列表](https://github.com/Kajiekazz/codex-switcher/actions/workflows/build-windows.yml) 中选择成功的构建，下载并解压对应架构的 Artifact。

安装包包含 Python、主程序、Playwright 驱动及本机二维码解析库；自动登录浏览器首次需要时下载，不随安装包分发。管理页使用系统默认浏览器。电脑仍需安装 Codex；可通过首次引导检测或选择 CLI 位置。

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

原生托盘、服务和启动检查由 `switcher.pyw` / `application.py` 提供。`components.py` 按当前 Playwright 版本下载浏览器及所需组件：先测试可用源的速度，下载失败时自动尝试备用源，也可在引导中手动选择下载源后重试。多个登录任务共享组件，已安装版本会直接复用。源码运行也可使用系统 Chrome。

## 自动构建

工作流 [build-windows.yml](../.github/workflows/build-windows.yml) 支持以下触发方式：

| 触发 | 结果 |
|---|---|
| `main` 相关代码变更 | 原生 x64、ARM64 构建和验证，保留 14 天的下载产物 |
| Pull Request | 同样构建和测试，不发布 Release |
| 手动 `Run workflow` | 对选定分支构建两种架构 |
| `v*` 标签 | 两个架构都成功后，将安装包及校验文件上传到对应 Release |

x64 使用 `windows-2025`，ARM64 使用 `windows-11-arm`，各自安装原生 Python、Playwright 和 PyInstaller。构建后校验主程序、Python DLL、Playwright Node 驱动及二维码解析扩展的机器类型；在清空开发工具 PATH 和空数据目录中验证启动、重复启动、引导、二维码导入和退出。二维码使用公开测试密钥，CI 不使用个人账号，不会触发真实绑定、切号或续跑。

ARM64 包的应用、Python 和驱动为原生 ARM64。实际下载的浏览器由 Playwright 版本决定，Windows 上可能通过系统的 x64 兼容支持运行。Windows 10 的源码基础服务与安装版自动登录组件要求不同，浏览器系统要求以当前 Playwright 支持范围为准。

发布时同步修改 `codex_switcher/__init__.py` 和 `extension/manifest.json` 的版本，再创建对应的 `v<版本>` 标签。主分支构建只产生 Artifact，不自动创建版本标签。标签构建成功后，已有 Release 只更新附件并保留其发布状态；尚无 Release 时自动创建并发布。因此，预先建立的草稿需在核对文案和附件后手动发布。
