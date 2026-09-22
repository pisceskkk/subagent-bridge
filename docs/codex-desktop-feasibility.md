# Codex Desktop 桥接可行性

日期：2026-09-22

## 结论

在“Desktop 通过 SSH 使用 WSL Codex”的固定部署下，目标链路已经证明可行。Desktop 的 `app-server proxy` 与 Bridge 可以同时连接 WSL 中的同一个 managed app-server。Bridge 通过该实例执行 `thread/start` 和 `turn/start` 后，Desktop 能实时显示新任务、输入和模型回复。

最终约束是：Bridge 必须作为共享 app-server 的第二个客户端，不能为 Desktop 已持有的 thread 另起竞争 app-server。独立 app-server 仍适合隔离测试或 Bridge 自有会话，但不能用于回投 Desktop 当前会话。

最终部署和实施细节见 `docs/final-wsl-desktop-architecture.md`。

## 与当前设计的差异

现有 `design.md` 把 Codex 父连接器设想为 plugin/hook，并要求 `working/idle` 心跳、当前 turn 注入和 receipt。app-server 已提供更直接的基础能力：

- `thread/fork`：从持久线程派生新线程；
- `thread/resume`：加载已有线程；
- `turn/start`：对 idle 线程启动新 turn，相当于主动唤醒；
- `turn/steer`：向正在执行的 turn 追加输入，可作为 `immediate` 的候选实现；
- `thread/status/changed`、`turn/completed`：可替代部分猜测式 idle 检测。

建议为 Codex 单独增加 `codex_app_server` Parent Connector，不再把它仅建模成通用 plugin。Bridge 必须拥有或连接同一 app-server 实例，并保持长连接订阅事件。父线程 ID 应由连接器显式注册，不能扫描私有文件或按“最近会话”猜测。

## 能力判定

| 目标 | 判定 | 条件 |
| --- | --- | --- |
| Codex 主线程派生子线程 | 可行 | `thread/fork`；源 turn 进行中时应等待完成或显式接受中断标记 |
| Bridge 主动继续 idle 线程 | 可行 | `thread/resume` 后 `turn/start` |
| 父 turn 执行中立即补充消息 | 可行 | `turn/steer`，并携带准确的 `expectedTurnId` |
| 子结果回注父线程下一 turn | 可行 | 父线程 idle 后 `turn/start` |
| Bridge 唤醒 Desktop 当前 UI 会话 | 已验证可行 | Desktop 与 Bridge 连接同一 WSL managed app-server，并使用精确 thread id |
| 用 Codex 内置 subagent 代替 Bridge 子进程 | 技术可行但目标不同 | 内置 subagent 由父 turn 管理，不等同于 Bridge 的跨厂商 daemon、恢复和投递模型 |

## 原型工具

`tools/codex_app_server_demo.py` 使用 Python 标准库启动独立 app-server：

```bash
python3 tools/codex_app_server_demo.py probe
python3 tools/codex_app_server_demo.py --connect-managed \
  --managed-socket ~/.codex/app-server-control/app-server-control.sock probe
python3 tools/codex_app_server_demo.py \
  --unix-websocket ~/.codex/app-server-control/app-server-control.sock probe
python3 tools/codex_app_server_demo.py resume-check --thread-id THREAD_ID
python3 tools/codex_app_server_demo.py fork --thread-id THREAD_ID --prompt "do bounded work"
python3 tools/codex_app_server_demo.py wake --thread-id THREAD_ID --prompt "consume the child result"
```

`resume-check` 不启动模型 turn。`fork` 和 `wake` 默认只打印将发送的协议消息；加入 `--execute` 才会真正启动模型 turn，避免误操作和额度消耗。

当 Desktop 通过 SSH 连接远端 Codex host，并由该 host 启动 managed app-server 时，`--connect-managed` 通过 `codex app-server proxy` 接入同一个 control socket，而不是创建竞争写入者。该模式应先用 `probe` 验证，再考虑任何写操作。

control socket 使用 WebSocket over Unix socket。`--unix-websocket` 执行 HTTP Upgrade 和 RFC 6455 帧编码，是 Bridge 直接接入 managed app-server 的验证路径；不能把 stdio JSONL 直接写给 `app-server proxy`。

## 下一阶段验收

1. 把 WebSocket demo 提取为长期运行的共享 app-server connector。
2. 从 Desktop 当前 Codex 会话调用 Bridge，并用 `CODEX_THREAD_ID` 建立 parent binding。
3. 子 Agent 完成后，等待父线程 idle，再用 `turn/start` 把结果投递回原线程。
4. 父线程运行时用 `turn/steer` 注入带 delivery id 的消息，验证一次性语义。
5. 为 delivery 添加幂等键、父 generation 和 app-server 响应证据；协议成功只代表接受，不代表父 Agent 已处理。

## WSL SSH managed app-server 实测

2026-09-22 在 ChatGPT Desktop 通过 SSH 连接同机 WSL2 Ubuntu 的环境中验证成功：

1. Desktop 的 SSH 会话运行 `codex app-server proxy`。
2. 远端 host 使用 `codex ... app-server --listen unix://`，control socket 位于 `~/.codex/app-server-control/app-server-control.sock`。
3. Bridge 使用 WebSocket over Unix socket 进行第二客户端握手，成功执行 `initialize` 和只读 `thread/list`。
4. Desktop proxy 自动重连到 Bridge 启动的 app-server；socket 同时存在已建立连接，证明 Desktop 与 Bridge 可以共享同一实例。

该环境还存在两个必要条件：

- SSH 启动环境必须继承 Desktop 使用的企业代理、`NO_PROXY` 和 CA 配置，否则模型与 MCP 初始化会超时。
- SSH 下发的 `CODEX_REMOTE_PAYLOAD` 必须传给 Bridge-owned app-server，才能保留 Desktop remote host 的启动上下文。

`app-server proxy` 是原始字节代理，而 control socket 使用 WebSocket。Bridge 必须实现 HTTP Upgrade 和 RFC 6455 帧，不能把 stdio JSONL 直接写入 proxy。当前 demo 的 `--unix-websocket` 已验证该传输层。

随后通过共享 socket 成功执行 `thread/start` 和 `turn/start`。测试 thread `01a0c7f7-a409-7f12-83ab-2efcaefb661b` 完成回复 `BRIDGE_SHARED_APP_SERVER_WRITE_OK`，Desktop 实时显示了任务与回复。仍待验证的是 working 状态下的 `turn/steer`、同一 thread 的并发写仲裁以及完整 delivery 幂等恢复。
