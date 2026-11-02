# 桌面端指定回合中断验证

2026-11-02，适用本机 Codex `26.1027.3384.0`。用户授权删除续跑记录时立即强制停止当前回合。分析限于本机程序与隔离测试，未修改安装文件，未停止用户的真实任务。原始附件位于本地 `work/desktop-targeted-stop/`，未入库。

## 结论

独立 app-server 的 `turn/interrupt` 无法控制桌面端持有的线程。桌面端另有 `codex-ipc` 协调通道，可查找实际 owner，再将带当前回合 ID 的中断请求转发给它。实现采用这一通道。中断失败时保留记录和错误，不提前移除。

## Evidence → Finding → Path

| 证据 | 观察 | 结论与实现 |
|---|---|---|
| E1：两份 native app-server 隔离实验 | 外部连接返回 `thread not found`，实际 owner 可将回合中断 | 不用另起 app-server 冒充中断成功 |
| E2：安装包 339、491、824 JS bundle | `thread-owner-discovery` 定位 owner；`thread-follower-interrupt-turn` 转发到 `interruptConversation`；`Cs` 先核对 `expectedTurnId` | `desktop_ipc.py` 使用对应协议，限定目标会话与回合 |
| E3：真实桌面端的负向校验 | 对运行会话传入不存在的回合 ID，返回无匹配中断；原回合保持运行 | ID 不匹配时拒绝成功，不扩大停止范围 |
| E4：隔离管道与真实 native owner | 新客户端经隔离协调管道发出中断，native 回合返回 `interrupted`，耗时约 84 ms | 验证实际中断，无需等待推理或任务自然完成 |
| E5：隔离 HTTP 服务和真实浏览器 | 暂停项可直接继续；删除撤回消息、暂停目标、中断目标回合并隐藏记录；原会话仍存在 | 前后端操作闭环，停止失败保留错误 |

E4 的协调服务为隔离夹具，实际终止由 native app-server 执行；E3 验证了安装中桌面端的请求路径与回合保护。未用用户的真实任务做正向停止实验。

## 协议与定位

安装包内 `.vite/build/early-bootstrap-339-096ec1020e8592e0.js` 的 IPC client/router 使用小端 4 字节长度前缀和 JSON 帧；824 bundle 的模块 `92272` 定义 Windows 管道 `\\.\pipe\codex-ipc`。本机 491 bundle SHA256 为 `db3fd1590ec3d3c621a85c2cf925785ea6d99df8bc4eaec8bc4b1e974758a8ab`。

调用顺序为 `initialize`（版本 0）、`thread-owner-discovery`（版本 1）、`thread-follower-interrupt-turn`（版本 4）。最后一步携带 `targetClientId`、`conversationId`、`mode:user-stop` 与 `expectedTurnId`，只认可同一 `interruptedTurnId`。代码从未启动路由服务，也不接受其他客户端的调用。

这是内部版本协议。桌面端更新可能返回版本不匹配；此时显示错误并保留记录，禁止退回到结束应用进程。

## 回归验证

在仓库根目录运行以下命令，无需真实账号或桌面端：

```powershell
python -m unittest discover -s tests -p test_desktop_ipc.py
python -m unittest discover -s tests -p test_resume_board.py
node --test tests/test_session_ui.js
```

覆盖帧格式、owner 定位、回合不匹配、缺少 owner、中断失败保留记录、移出状态持久化，以及后续手动继续重新显示。隔离 native 实验与浏览器脚本保存在本地 `work/`，不作为项目运行依赖。
