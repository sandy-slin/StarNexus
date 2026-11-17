# 从 GitHub 自升级（1.2.0）

StarNexus 只从 GitHub 仓库 `sandy-slin/StarNexus` 的 `x.y.z` 标签升级，不用官方安装包。

## 组成

| 东西 | 位置 |
|---|---|
| 升级脚本（仓库） | `tools/selfupgrade.py` |
| 升级脚本（运行副本） | `~/.carher-feishu-agent/upgrade/selfupgrade.py`，部署时从仓库复制 |
| 定时任务 | `com.starnexus.selfupgrade`，模板 `tools/com.starnexus.selfupgrade.plist`，每 6 小时一次 |
| 记录 | `~/.carher-feishu-agent/upgrade/state.json`（已装标签、提交、坏版本、最近 20 次结果） |
| 日志 | `~/.carher-feishu-agent/logs/selfupgrade.log` |
| 备份 | `~/.carher-feishu-agent/bridge.bak-upgrade-<时间>`，保留最近 5 份 |

升级脚本在服务外面独立运行：重启服务不会把它自己停掉。

## 一次升级做什么

1. `git ls-remote --tags` 取最新 `x.y.z`；不比已装的新、或是坏版本 ⇒ 结束。
2. 服务在干活（`inflight.json` 非空，或最近 3 分钟有会话活动）⇒ 下一次再来。
3. 在 `~/.carher-feishu-agent/upgrade/src` 浅克隆该标签，不碰开发仓库；核对提交与标签一致。
4. 用服务的 venv 跑全部单元测试；不过 ⇒ 记坏版本、通知、结束，运行目录不动。
5. 备份 `bridge/` → `rsync --checksum --delete` 同步 → 核对整棵目录哈希 → 重启服务。
6. 90 秒内日志出现「桥已启动」和长连接 connected，进程号换新，之后 3 分钟一直 running、没再重启 ⇒ 记为已装。
7. 第 6 步不满足 ⇒ 换回备份、重启，记坏版本；坏版本不再自动重试，直到出现更新的标签。

结果（成功或失败）私聊主人，并发到 Remote control dev 群。

## 常用命令

```bash
P=~/.carher-feishu-agent/venv/bin/python
$P ~/.carher-feishu-agent/upgrade/selfupgrade.py --dry-run          # 只看有没有新标签
$P ~/.carher-feishu-agent/upgrade/selfupgrade.py                    # 立即跑一次
$P ~/.carher-feishu-agent/upgrade/selfupgrade.py --tag 1.2.0        # 人工装指定标签（绕过坏版本记录）
$P ~/.carher-feishu-agent/upgrade/selfupgrade.py --mark-installed 1.2.0   # 人工部署后登记版本
launchctl kickstart gui/$(id -u)/com.starnexus.selfupgrade          # 触发定时任务
```

## 和人工部署的关系

- 人工按 [`runtime-delivery.md`](runtime-delivery.md) 部署开发分支做实测，不影响自升级；合回 `main` 打标签后，下一次自升级会把运行目录换成该标签的代码。
- 人工部署了某个标签的代码后，执行 `--mark-installed <标签>` 登记，避免重复安装。
- 修改了 `tools/selfupgrade.py` ⇒ 同步运行副本：`cp tools/selfupgrade.py ~/.carher-feishu-agent/upgrade/`。

## 官方更新与上报

- `~/.carher-feishu-agent/.local-deploy` 存在 ⇒ 桥内置的官方自动更新跳过，诊断里的「服务端最新版」不再查官方源，飞书里发「更新」只报 GitHub 自升级状态。
- 启动守卫 `launch-guard.sh` 在有 `.local-deploy` 时不向官方服务器上报崩溃。
- 不要在这台机器上运行官方安装命令（`curl … install.sh | bash`），它不认 `.local-deploy`，会整体覆盖。

## 验收记录

| 编号 | 用例 | 结果 |
|---|---|---|
| UP-01~UP-15 | 单元测试（`tests/test_selfupgrade.py`）：标签排序、已是最新、正常安装、测试不过、起不来回滚、启动后崩溃回滚、忙时不装、取代码失败不记坏版本、坏版本后出新标签、只看不装、备份保留 5 份、不向官方上报、「更新」回话、重启前清守卫计数 | 通过 |
| UP-16 | 真机：GitHub 查最新标签 `--dry-run` | 11-18 通过（读到 1.0.1 → f9e1280） |
| UP-17 | 真机：飞书私聊发「更新」 | 11-18 00:20 通过，回「只从 GitHub 的版本标签自动升级…当前版本：1.1.0」 |
| UP-18 | 真机：1.1.0 → 1.2.0 端到端自动升级 | 见下方 |
