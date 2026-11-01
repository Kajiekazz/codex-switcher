# Codex Switcher Chrome 扩展（侧边栏 + 工具栏徽章）

Manifest V3 扩展，是浏览器侧面板的**唯一来源**。旧 Tampermonkey 脚本已退役，留档在
`legacy/codex-switcher.user.js`，不再维护。

## 组成

| 文件 | 作用 |
|---|---|
| `manifest.json` | MV3 清单：`sidePanel` / `action` / `alarms` / `storage` / `notifications`，`host_permissions` 只含 `http://127.0.0.1:8787/*` |
| `background.js` | service worker。`chrome.alarms` 每分钟唤醒一次，拉 `/api/status` + `/api/quota`，把 5h 剩余百分比写进工具栏徽章（颜色分档），额度从「有」变「无」时发系统通知；同时把快照写入 `chrome.storage.local` |
| `sidepanel.html` | 侧边栏骨架 + 样式（浅色卡片主题） |
| `sidepanel.js` | 侧边栏逻辑。扩展页面自身带 host 权限，因此**直连** `127.0.0.1:8787`，不依赖 service worker 醒着 |
| `icons/` | 16/32/48/128 图标 |

## 安装

最省事：打开 Dashboard <http://127.0.0.1:8787/> → 「浏览器扩展」卡片 → **复制安装路径**。
之后点地址栏右侧拼图图标 🧩 →「管理扩展程序」→「开发者模式」→「加载已解压的扩展程序」→ `Ctrl+V` 粘贴 → 选择文件夹。

> Chrome 会丢弃命令行传进来的 `chrome://` 网址（实测 Chrome 156：只开一个空白标签页，
> `--app=` / `--new-window` / 转发给已运行实例都不行），所以没有任何程序能替你打开扩展页；
> 卡片提供一个「复制扩展页网址」按钮，方便粘到地址栏。

手动安装：

1. 先启动本地服务：
   ```powershell
   cd codex-switcher          # 仓库根目录
   .\start.ps1 -EnableSwitch
   ```
2. 打开 `chrome://extensions`，开启右上角「开发者模式」。
3. 点「加载已解压的扩展程序」，选择本目录 `extension/`。
4. 把「Codex 切号助手」钉到工具栏，点图标即打开侧边栏。

装好后回 Dashboard 点「重新检测」，卡片状态会变成「已安装」（它的做法是读 Chrome
`User Data/*/Preferences` 里 `extensions.settings.<id>`，不会改动任何东西）。

> 为什么不做真正的静默安装：Chrome 只认 Web Store 和「企业策略」两条通路，策略方式
> 会让浏览器显示「由组织管理」，所以这里选择引导式安装，不写注册表。

## 徽章含义

| 徽章 | 含义 |
|---|---|
| `62%` 绿 | 当前账号 5h 剩 ≥50% |
| `34%` 琥珀 | 5h 剩 20–49% |
| `8%` 红 | 5h 剩 <20%，准备切号 |
| `0%` 红 | 额度用尽（同时按需发系统通知） |
| `–` 灰 | `~/.codex/auth.json` 不存在 |
| `!` 灰 | 本地服务没起来 |
| `?` 灰 | 服务在线但 `/api/quota` 失败，悬停看原因 |

## 说明

- 只访问本机 `127.0.0.1:8787`，不把账号数据发到外部。
- 侧边栏里的「切号」仍需本地服务显式设置 `CODEX_SWITCHER_ENABLE_SWITCH=1`；
  自动切号另外需要 `CODEX_SWITCHER_ENABLE_AUTO_SWITCH=1`。
- 额度过期/401 时徽章不会自己变绿，等本地服务下一次刷新 `/api/quota` 成功后自动恢复。
- 改了 `sidepanel.*` / `background.js` 后，在 `chrome://extensions` 点本扩展的刷新按钮即可；
  侧边栏不需要重新安装。
- 只支持 Chromium（`chrome.sidePanel` 需要 Chrome 114+）。Firefox/Tampermonkey
  不再是一等公民。
- 自动化测试里 `--load-extension` 在 Chrome 137+ 稳定版被移除，只影响脚本化加载；
  手动「加载已解压的扩展程序」不受影响。
