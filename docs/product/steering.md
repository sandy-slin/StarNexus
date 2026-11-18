# 中途插话（steering）：调研结论与实现方案

> 状态：**1.8.0 已实现 Claude（会话群、私聊、新建会话第一轮）和 opencode（接着聊）的中途插话**；Codex（1.9.0）未实现。验收用例 FC-38~FC-43 见 [`full-control.md`](full-control.md) §8。
>
> 1.7.0 范围：会话群 / 对话模式里，**桥自己起的** Claude 那一轮（`ClaudeAdapter._run_streaming`，stream-json 输入开着）。
> 1.8.0 补上：私聊里桥自己跑的 Claude（`ClaudeRunner`，`_run_locally`）、Claude 新建会话的第一轮、opencode 接着聊的那一轮。
> 不在范围：终端 / App 里开着的会话、Cursor、opencode 新建会话的第一轮（仍是 `--standalone` 私有服务，插不进，照旧提示）。

## 1. 要解决的问题

会话一轮还在跑时，用户在飞书里再说一句话，这句话现在只能**排队**：私聊回「前面还有 N 条在跑，这条排上了」，
会话群回「上一条还在发，等它回来再说」。用户想纠偏（“别改那个文件”“换个思路”），只能等它跑完，或者 `/stop` 后重说。

目标：一轮在跑时，新发的话**插进正在跑的这一轮**，它在下一步就看到并调整方向；不支持的工具明说“这一轮结束后再处理”。

## 2. 调研结论（2026-11-18 本机实测，一次性目录，已清理）

三种工具的 CLI 都支持在一轮进行中插话。实测方法相同：第一句让它逐条执行 10 次 `sleep 3 && date >> log.txt`，
约 20 秒后插一句“立即停止剩下的 sleep，只回复 STEER-OK”。脚本在 [`docs/research/steering-probes/`](../research/steering-probes/)。

| 工具 | 版本 | 接口 | 结果 |
|---|---|---|---|
| Codex | codex-cli 0.167.0 | `codex app-server`（stdio JSON-RPC）：`turn/start` 开一轮，`turn/steer {threadId, expectedTurnId, input}` 插话，`turn/interrupt` 停 | ✅ 插话返回同一个 turnId；只跑了 1 次 sleep 就停，同一轮结束，回 STEER-OK |
| Claude | Claude Code 2.1.327 | 常驻 `claude -p --input-format stream-json --output-format stream-json --replay-user-messages`，往 stdin 再写一条 user 消息 | ✅ 跑了 2 次 sleep 后停，只出 1 个 result，回 STEER-OK；`--replay-user-messages` 在 1 秒内回显插话，可当送达回执 |
| opencode | v2.0.15 | 后台服务 HTTP：`POST /api/session/{id}/prompt`，`delivery: "steer"`（另有 `queue`）；`POST .../interrupt` 停 | ✅ 跑了 2 次 sleep 后停，回 STEER-OK |
| Cursor | — | 桥只能单向发送 | ❌ 不支持，退回排队 |

几点细节：

- **Codex** 的 `expectedTurnId` 必须是当前在跑的那一轮，不匹配直接报错。这正好防止“上一轮已经结束、插话落成新一轮”的误判，失败时退回排队即可。
- **Claude** 插话不会另起一轮，模型在当前工具调用结束后读到新消息。进程要一直开着，stdin 关掉才退出。
- **opencode** 的 `prompt` 本身就是“持久入队 + 调度”，`delivery` 决定插进当前这一轮还是排到下一轮；`inbox` 接口能看还没送达的条目，也能撤回。
- **终端 / App 里开着的会话**不归桥管，插不进去（和 `/stop` 停不了是同一个原因），维持现状：排队或提示到电脑上操作。

## 3. 交互定义

- **默认插话**：会话在跑、工具支持、而且这一轮是桥自己起的 ⇒ 新消息直接插进去。先回一句「↪️ 已插进正在跑的这一轮」，收到工具回执（Claude 回显 / Codex 返回 turnId / opencode 返回 delivery=steer）后才算送达。
- **退回排队**：工具不支持、会话在终端里开着、或插话失败（这一轮恰好刚结束）⇒ 照旧排队，并说清楚「这一轮结束后再处理」。
- **不再回“等它回来再说”**：会话群里同一个会话在跑时，第二句不再拒收，按上面两条处理。发给**别的**会话的那条还在跑时仍然拒收（一个私聊同一时间只跟一个会话说话）。
- **`/stop` 不变**：停整轮。Codex 用 `turn/interrupt`，opencode 用 `interrupt`，Claude 关 stdin 并停进程。
- **状态卡**：插话后在同一张进展卡上加一行「插话 1 条：……（前 20 字）」，不另刷消息。
- **结果**：插话和原任务属于同一轮，最后只贴一次回复。
- **斜杠命令和卡片按钮不当插话**：`/stop`、`/m` 等照旧在拿锁之前处理。

## 4. 实现方案

### 4.1 能力声明

`bridge/fleet/model.py` 的 `CAPS` 给 claude、codex、opencode 加 `"steer"`；cursor 不加。菜单、提示语一律按 `can(tool, "steer")` 判断，不写死工具名。

### 4.2 统一接口

`Adapter` 新增两个方法，默认实现返回“不支持”：

- `steer(session_id, text) -> SteerResult`：`ok / queued / unsupported / failed`，带一句给用户看的说明。
- `is_steerable(session_id) -> bool`：这一轮是桥自己起的、进程还活着、在跑。

桥这边只在两处改分支：私聊的 `_run_locally`（`session.lock` 被占时）和会话群的 `_maybe_send_to_fleet`（`_fleet_inflight` 命中同一个会话时）。先试 `steer`，不是 `ok` 再走原来的排队逻辑。

### 4.3 各工具改造

| 工具 | 现在 | 改成 | 要处理的 |
|---|---|---|---|
| Codex | 每轮 `codex exec` 一次性进程 | 桥内常驻一个 `codex app-server` 子进程（stdio），按 thread 管理；一轮 = `turn/start`，插话 = `turn/steer`，停 = `turn/interrupt` | 审批请求改从 app-server 的 server request 回（和现有审批卡对接）；权限档位映射到 `sandbox` / `approvalPolicy`；app-server 崩了自动重启并 `thread/resume`；它是实验功能，版本升级要跑探针回归 |
| Claude | 每轮 `claude -p --resume` 一次性进程，`--output-format json` | 一轮在跑期间用 stream-json 输入输出，插话写 stdin；收到 `result` 后关 stdin，进程退出（**不做长期常驻**） | 结果解析从单个 json 改成逐行事件；审批钩子、`--setting-sources ""`、allowed/disallowed tools 原样保留；FleetJobs 登记、防分叉锁、`inflight.json` 断点续跑不变 |
| opencode | 每轮 `opencode run --standalone` | 插话时调 `opencode api POST /api/session/{id}/prompt`，`delivery: steer`；一轮的启动方式暂不变 | `--standalone` 起的是私有服务，插话要连到**同一个**服务：改成桥固定起一个私有服务（`--server` 指过去），否则插不进去 |

### 4.4 选择和取舍

- **Claude 只在一轮期间保持进程**，不做会话级常驻。常驻要处理空闲回收、内存、和终端抢同一个会话，收益只是省掉每轮启动的几秒。按轮开关的进程和现在的生命周期一致，防分叉锁不用重写。
- **Codex 改走 app-server**，而不是 `codex queue`。`queue` 是排到下一轮，达不到“插进当前这一轮”；app-server 的 `turn/steer` 实测能插进去。代价是换掉整条 Codex 发送通道，风险最大，所以放最后做、单独回归。
- **插话默认开启**，不加开关。用户在一轮跑着时说话，本意多半是纠偏；如果只想排队，等它回完再说即可。

### 4.5 分步交付

1. `1.7.0`：能力声明 + 桥的分支逻辑 + **Claude** 插话（改动面最小，覆盖默认工具）+ 状态卡“插话 N 条”。
2. `1.8.0`：**opencode** 固定私有服务 + `delivery: steer`。
3. `1.9.0`：**Codex** 发送通道换成 app-server，`turn/steer` / `turn/interrupt`，审批对接。

每一步都按 [`dev-release.md`](../dev-release.md)：开发分支 → 全量单测 → 部署 → 真机回归 → Computer Use 实测 FC-38~FC-43 → 合并打标签。

## 5. 风险

- <span style="color:red">Codex app-server 是实验功能，协议可能随 codex 升级变化。每次 codex 升级要先跑 `docs/research/steering-probes/codex_probe.py`，不通过就把 Codex 的 `steer` 能力关掉，退回排队。</span>
- <span style="color:red">插话会改变正在跑的任务。插错会话的代价比排队大，所以“发给别的会话的还在跑”时仍然拒收，不做跨会话插话。</span>
- 插话的时机由模型决定：Claude 和 opencode 在当前工具调用结束后才读到，长命令（比如跑 10 分钟的测试）中途插话要等这条命令跑完。要立刻停，用 `/stop`。
- 一轮恰好在插话时结束：Codex 会因 `expectedTurnId` 不匹配报错，Claude 的 stdin 已关闭；两种都退回排队，这句作为下一轮发出，不会丢。

## 6. 1.7.0 实现记录

- `ClaudeAdapter._send_locked`：真实进程出口（有 `runner.popen`）走 `_run_streaming`；测试替身没有 `popen` 时照旧一次性运行。
- `_run_streaming`：`_stream_argv` 把一次性参数换成 `--input-format stream-json --output-format stream-json --verbose --replay-user-messages`，其余参数（权限、钩子、白名单、模型）不变；第一句写 stdin；收到 `result` 且回显数 ≥ 写入数才关 stdin；插话撞上收尾时它会再跑一轮，多个 `result` 的文字合并成一次回复。
- `steer()` 只写 `_live` 里登记的那一轮；`FleetControl.steer` 按 `can(tool, "steer")` 分派。
- 桥：`_maybe_send_to_fleet` 发现同一个会话在飞 ⇒ `_try_steer`（先过暂停和审查）⇒ 回「↪️ 已插进正在跑的这一轮」；插不进照旧回「上一条还在发」。
- 顺带修掉一条多余提示：桥自己起的那一轮在扫描里是 `owner=bridge`，原来被当成「电脑上又打开了那个会话」，每次发话都冒「提示：…和刚才不一样。这条照常发过去了」。现在 `owner=bridge` 和 `none` 同一条路；这类提示不需要人处理，改为只记日志。
- 测试：`tests/test_steering.py`（假 stream-json 进程覆盖插进同一轮、撞上收尾不丢、结束后拒收、桥的插话 / 回退 / 暂停 / 去提示）；真机探针（10 次 sleep，20 秒插话）跑 3 次后停、回 STEER-OK；插话后 `/stop` 立即结束。

## 7. 1.8.0 实现记录

- **私聊里桥自己跑的 Claude**：`bridge/runner.py` 的 `ClaudeRunner.run_turn` 带上 `steer_key`（私聊 scope）后改用 stream-json 进程，和 `_run_streaming` 同一套规则：第一句写 stdin，回显数 ≥ 写入数且收到 `result` 才关 stdin，多个 `result` 合并；`try/finally` 保证结束必关 stdin、撤销登记。`_run_locally` 在锁被占时先 `_try_steer_local`，插进就回「↪️ 已插进正在跑的这一轮」，插不进照旧排队。
- **新建会话第一轮（Claude）**：桥先生成 uuid 写进 `_fleet_inflight`，作为 `session_id` 传给 `control.start` → `claude --session-id`，第一轮一开始就有真实会话号，后面的话可以插进去。第一轮正好收尾时回「新会话第一轮正好在收尾，这句没插进去。等它回来再说一次。」
- **opencode**：接着聊时先 `opencode service status` 判断本机常驻服务在不在；在的话不加 `--standalone`，这一轮跑在常驻服务里。插话先查 `GET /api/session/active` 确认这个会话在跑，再 `POST /api/session/{id}/prompt {delivery: "steer"}`，响应里 `delivery` 是 `steer` 才算插进。常驻服务按会话自己的目录干活（实测写对目录）。
- **opencode 停下**：走常驻服务时 `/stop` 先调 `/interrupt`。实测只杀掉 `opencode run` 客户端，服务里那一轮还会继续跑。
- **opencode 新建会话**仍走 `--standalone`（第一轮没有会话号，连不到同一个服务），所以第一轮不能插话，照旧提示。
- 测试：`tests/test_steering_dm.py`（假 stream-json 进程）、`tests/test_steering_opencode.py`（假 HTTP 服务）；真机回归 `steer_dm`、`steer_first_turn`、`steer_opencode`、`steer_claude` 全过。
