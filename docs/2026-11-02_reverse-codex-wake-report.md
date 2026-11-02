# Codex 队列唤醒故障诊断

2026-11-02，Windows x64，Codex Desktop 26.1027.3384.0。普通故障逆向分析，flavor=null。

## 结论与范围

旧版用 `DETACHED_PROCESS` 启动 PowerShell，辅助进程可能退出码为 0，却未交付唤醒 URI。改为 `CREATE_NO_WINDOW` 后，指定空闲置顶会话约 7 秒内开始了 `turn_trigger="queue"` 的新回合，验证过程中未通过 UI 打开会话。队列行消失仍不能单独证明执行，因为编辑或删除也会移除消息；页面已改为“已出队 · 执行待确认”。

用户授权检查本机程序、适当 dump，并指定空闲会话进行实际续跑。本地范围记录为 `work/codex-wake-lazy-load/scope.md`。分析采用 bundle 静态追踪、隔离 CODEX_HOME 队列实验及进程内存元数据读取；没有注入、暂停进程、修改安装文件或保存原始内存。原始本地附件未入库，不公开个人会话内容。

依赖与调用锚点为 Start-Process/ShellExecute、MSIX codex 协议、wakeQueuedThread、thread/queue/list 和 thread/resume；故障位于启动及协议调用路径，无需扩展到无关的 PE 导入表。

## Evidence 与 Findings

source_ref 相对本地 `work/codex-wake-lazy-load`。动态记录 content_hash 为 n/a，仅保留必要元数据。

| E-id | source_ref | 观察与复现 |
|---|---|---|
| E-001 | static-notes.md S1–S7 | wake 查询队列并恢复线程，通知桥接存在；独立终止 seed 的三组功能开关对照均能启动队列。重现：本地附件 native_fresh_feature_probe.py，需相同安装版本 |
| E-002 | launcher-comparison-20261102-170334.json | detached 退出 0、未命中 nonce；no-window 在主进程及 app-server 命中 URI/查询元数据。重现：本地 compare_launchers.py，需调整 PID，使用随机不存在会话 |
| E-003 | 指定会话 rollout、只读 thread API | 17:04:32.293 出现 queue 归因的新回合，出队耗时 6.7 秒；17:15 复核已有持续工具执行和进度输出。复现需授权的空闲未归档会话，对照 task_started.turn_attribution |
| E-004 | tests/test_desktop.py、tests/test_resume_queue.py | 29 个客户端测试与 9 个投递测试通过，命令见下 |
| E-005 | runtime-notes.md | 历史 queue/delete 后没有新回合，证明仅凭出队不能确认执行；源数据为个人会话，仅保留脱敏记录 |

安装版 ASAR 的 content_hash（SHA256）为 `6fbedf054a8d8cafaf5c745b40aa59176512e106e0bd6308275ae123b60fd074`。

| F-id | severity | evidence_ids | confidence | location | status |
|---|---|---|---|---|---|
| F-001：辅助进程启动方式导致 URI 丢失 | n/a_re | E-002、E-003、E-004 | high | DesktopClient._open_url | validated，已修复 |
| F-002：出队误报执行成功 | n/a_re | E-005 | high | 看护、续跑结果及页面 | validated，已修复 |
| F-003：manager 缺失或功能开关要求先点开 | n/a_re | E-001 | high | 桌面唤醒/通知路径 | false_positive，已排除 |

## Path 与时间线

P-001，path_type=callflow：Switcher 入队并启动辅助进程（F-001）→ Windows 交付 URI（E-002）→ 主进程 list/resume，native 调度队列（E-001）→ 新回合归因为 queue（E-003）。未将“消息出队”替代为“任务完成”。

16:07 建立范围；16:33–16:51 静态及隔离对照，纠正早期假设；16:47 内存元数据确认协议链路；17:03 启动标志对照；17:04 实际队列回合启动；17:07 Switcher 服务加载修复。本地完整记录为 work/codex-wake-lazy-load/timeline.md。

## 验证和适用边界

```powershell
python -m unittest discover -s tests -p test_desktop.py -v
python -m unittest discover -s tests -p test_resume_queue.py -v
node --test tests/test_session_ui.js tests/test_quota_ui.js
```

更新后重启 Switcher 服务加载 Python 修改。既有消息无需重复入队，看护会在重试时使用新方式。实测要使用空闲、未归档的会话，对照新回合归因。本次证明了队列自动启动，不表示任务内容已完成，也不保证其他版本和机器具有相同延迟。
