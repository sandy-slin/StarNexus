# 调研：官方 20261123 版 vs StarNexus 1.9.9 —— 能力对比与移植取舍

对比对象：官方 feishu-agent `20261123-ebfd8863`（只解包，未运行、未安装）与 StarNexus `4a70f40`（1.9.9，build `20261113-e9dd47bc`）。
方法：从官方一键安装包 `feishu-agent-mac-20261123-ebfd8863.command`（sha256 已核 `f61f3d58…`，与 `latest.json` 一致）里解出全部 48 个 `bridge/` 模块，逐个与本仓库 `bridge/` 做 diff。

🔴 **没有运行官方安装器**。运行它会停掉本机 StarNexus 服务、改写启动配置、用官方版整包替换（全权限、`SN·` 群名、opencode、CEO、插话全部消失），且删掉旧备份。见 [`2026-11-17-upstream-newsession-selfupdate.md`](2026-11-17-upstream-newsession-selfupdate.md)。

## 一、总体结论

StarNexus 在**编排能力**上明显领先官方：CEO 编排、中途插话（steering）、多会话、opencode、`SN·` 群、GitHub 自升级都是本仓库独有，官方 20261123 一个都没有。官方这一版比我们新 10 天，真正值得看的是它新增的**本机能力**和一些共享核心模块的打磨。

不整包合并，按价值挑着移植。

## 二、官方有、我们没有的模块

| 模块 | 行数 | 它做什么 | 对 StarNexus 的取舍 |
|---|---|---|---|
| `fleet/termsend.py` | 733 | 往电脑上那个**终端窗口**里送斜杠命令 / Esc / 按键，走 cmux 的 `paste --submit` 和 `send-key escape`，用目标会话自己的 socket 令牌鉴权 | 🟢 **移植**。直接补我们的插话缺口 |
| `fleet/brand_icons.py` | 463 | 总览卡分组标题前放 claude / cursor / codex 自家图标（上传飞书拿 img_key，按 app_id 缓存，拿不到回落 🤖） | 🟢 **移植**。纯 UX，无冲突 |
| `fleet/toolkit.py` | 568 | Claude 会话群输入框上方的快捷工具栏（停下 / 接着干 / 模型 / 进度 / 改动 5 个按钮） | 🟢 **照搬移植（林森 11-26 拍板）**。点击回调经官方 `chat.auto-link.com.cn/her-tk` 收件箱中转、桥每 ~25 秒长轮询取走。明知这会重新引入官方服务器依赖，仍按官方实现照搬 |
| `errreport.py` | 1135 | 桥崩溃 / 报错自动上报到官方服务器 198 | 🔴 **不移植**。[`2026-11-17` 文档](2026-11-17-upstream-newsession-selfupdate.md)已决定关闭向官方服务器的崩溃上报 |

## 三、看起来重合、但我们已经更强的

| 能力 | 官方 20261123 | StarNexus 1.9.9 | 结论 |
|---|---|---|---|
| 一张卡开会话 | `newsession.py` 976 行（未拆分） | `fleet/nscard.py`：工具 ▾ + 项目 ▾ + **模型 ▾ + 强度 ▾** + 项目框 / 会话名联动 + 新建项目 | 🔴 **不移植**，照搬是倒退。官方卡上选不了模型 / 强度 |

## 四、共享核心模块的差异（有分歧，需逐项甄别）

这些模块两边都有、但已分叉，且和我们的 fleet 功能（CEO / 插话 / 多会话）缠在一起，盲目移植会打断独有功能。差异行数（diff 的增删行）：

| 模块 | repo 行 | prod 行 | diff 行 | 备注 |
|---|---|---|---|---|
| `app.py` | 8539 | 6844 | 4861 | 我们多出 fleet 编排、插话、`SN·` 群等大量逻辑 |
| `fleet/claude.py` | 1641 | 1510 | 563 | 插话 / relay 相关 |
| `fleet/codex.py` | 1543 | 1167 | 438 | 同上 |
| `runner.py` | 553 | 453 | 426 | |
| `selfupdate.py` | 587 | 869 | 374 | 官方走官方服务器；我们走 GitHub，方向不同 |
| `fleet/cards.py` | 1269 | 1571 | 360 | 官方加了 brand_icons 相关画法 |
| `menu.py` | 659 | 678 | 341 | |
| `fleet/control.py` | 469 | 410 | 317 | |
| 其余 | | | | model/relay/chatbind/actions/workspace/policy/review 等均有分歧 |

这些**不在本轮移植范围**，逐项甄别需要配合 AGENTS.md 要求的真机飞书验收，另开任务。

## 五、本轮移植计划（按价值排序）

1. **termsend（终端送键）** — ✅ 已在 `claude/termsend` 分支完成（代码 + 单测 20 个，全量过）。补插话缺口：斜杠命令走 cmux `paste --submit` 真执行，Esc 走 `send-key escape` 真停。合回 main / 打标签前补 MacBook Pro2 真机验收。
2. **brand_icons（品牌图标）** — ✅ 已在 `claude/brand-icons` 分支完成（代码 + 单测 12 个，全量过）。需 `im:resource` 权限，拿不到回落 🤖。
3. **toolkit（会话群工具栏）** — ✅ 已在 `claude/toolkit` 分支完成（代码 + 单测 24 个，全量过）。**适配移植（林森 11-26 两次拍板）**：传输层（工具栏按钮 + `/her-tk` 点击轮询）照官方搬；但 5 个按钮**路由到 StarNexus 已有处理器**（`/stop`、已有 `modelcard`、详情卡、relay「接着干」、`_git_changes`），不新造官方那套 `build_toolbar_model_card` + `set_prefs`（StarNexus 的 `modelcard` + `model_prefs` 更强，照搬会并存两套模型机制）。依赖官方 `/her-tk` 收件箱。建群 body 的 `toolkit_ids` 头启动优化**故意不做**（名字前缀判据脆弱，`ensure_bound` 在同一流程里兜住正确性）。

## 附二：手机答「选择卡」的现状与缺口（林森 11-26 提问）

**问题**：以后 Claude / Codex 弹出「选哪个方案 A/B/C」这种选择卡，怎么从手机（飞书）做选择？

**现状**：分两种卡。
- **会话内选择卡**（agent 任务中途要你拍板）：StarNexus 后台走 `claude -p --resume`，这个模式**没有 `AskUserQuestion` 工具**（`fleet/choices.py` 模块头 2026-11-17 实测），Claude 会改成在回复里写「问题 + 1. 2. 3. 编号选项」。`choices.py` 的 `extract_choices` 认出结尾那组编号选项（2~6 个 + 在问的语气），挂成**回复下方的按钮**；点一下（`_on_choice_click`，act=`ch`）把那一项当你的回答发回同一会话（限主人、有时效）。手机上**点按钮或直接打字**都能答。
- **Claude Code 自己的 `AskUserQuestion` 对话框**（就是本轮我弹给林森的那种）：属于客户端 harness UI，不经过桥，飞书里没有对应按钮；经飞书的等价物就是上面那条文本回落。

**缺口（future 候选，未动）**：`choices.py` 只接**文本回落形式**，依赖 agent 把选项写成干净编号。待核：对 Codex 的选项格式、字母选项（A/B/C，正则已含）、多选、更花哨的排版的覆盖度。要「手机答任意 agent 的选择卡」得把这层做厚。

## 附：「198」和 `/her-tk` 是什么

官方注释里的「198」就是官方后端服务器，对外域名 `chat.auto-link.com.cn`。官方产品用到的路径：

| 路径 | 用途 | StarNexus |
|---|---|---|
| `/ccdl/feishu-agent/latest.json`、安装包 | 官方版本清单 / 安装包 | 已屏蔽，改走 GitHub 标签 |
| `/ccdl/feishu-agent/python`、`/wheels/` | 官方托管 Python 运行时、pip 镜像 | 不用 |
| `/ai/api/install-report` | 安装失败上报 | 不用 |
| errreport 上报 | 桥崩溃上报 | 不移植 |
| `/her-tk/register`、`/her-tk/poll`、`/her-tk/hook/<hook_id>` | toolkit 按钮点击中转收件箱 | 本轮移植 |

为什么 toolkit 需要它：飞书 `call_back` 型工具栏按钮被点时只回调**公网 webhook**（不填报 232001），本机桥没有公网地址。于是飞书回调落到 `/her-tk/hook/<hook_id>`，桥带 `poll_key` 长轮询 `/her-tk/poll` 取走点击。回调没有飞书签名，防伪造只靠 hook_id 保密 + payload 形状校验 + log_id 去重 + 120 秒时效。菜单和卡片按钮走飞书长连接，不经过这台服务器。

## 六、明确不做

- 不运行官方安装器。
- 不移植 `errreport.py`（向官方服务器上报）。
- 不移植官方 `newsession.py`（我们的 `nscard.py` 更强）。
- 不整包合并共享核心模块。
