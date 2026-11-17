# 开发分支、合并与发版

Codex 和 Claude 都按本文开发。`main` 只接受回归通过的合并；每次合回 `main` 都发一个版本标签。

## 1. 开工：每个需求一个工作区

```bash
cd /Users/LinSen/work/remote_control
git fetch origin && git checkout main && git pull --ff-only origin main
git worktree add -b codex/<需求名> ../remote_control-wt/<需求名> main   # Claude 用 claude/<需求名>
```

- 所有修改都在该工作区的开发分支上进行，不直接在 `main` 上改代码。
- 一个需求一个分支，名字用小写加连字符，例如 `codex/newsession-card`。

## 2. 合回 main 之前

1. 同步：`git fetch origin`，检查 `git merge-base --is-ancestor origin/main HEAD`。
   不是基于最新 `main` ⇒ 先 `git merge origin/main`，在开发分支上解决冲突，不丢弃对方修改。
2. 回归（任何一步失败 ⇒ 修复后从第 1 步重来，直到全部通过）：
   - 单元测试：`~/.carher-feishu-agent/venv/bin/python -m unittest discover -s tests -t .`，必须全部通过；
   - 部署开发分支代码（见 [`runtime-delivery.md`](runtime-delivery.md)），跑真机回归 `tests/live/live_regression.py`；
   - 涉及卡片、菜单、群的功能，按 [`computer-use.md`](computer-use.md) 在 MacBook Pro2 上用 Computer Use 实测。
3. 合并：
   ```bash
   cd /Users/LinSen/work/remote_control && git checkout main && git pull --ff-only origin main
   git merge --no-ff codex/<需求名>
   ```
   合并前若 `main` 又有新提交 ⇒ 回到第 1 步。

## 3. 合回 main 之后：第一件事打标签

```bash
git tag -a x.y.z -m "x.y.z <一句话说明>"
git push origin main && git push origin x.y.z
```

- 版本号 `x.y.z`：z = 修 bug；y = 新功能；x = 大版本变化（如加入董事长 / CEO 能力）。只增不减，不复用、不移动已推送的标签。
- 版本规划：`1.0.0` 阶段一（2848fd9）；`1.1.0` 一张卡开新会话；`1.2.0` 从 GitHub 自升级；`2.0.0` 董事长 / CEO。
- 打标签后再部署 `main`，记录标签和 SHA；之后清理工作区：`git worktree remove ../remote_control-wt/<需求名>`，分支已合并可删除。
- 本机服务每 6 小时按最新标签自升级（`tools/selfupgrade.py`），所以**未通过回归的代码不得打标签**。

## 4. 例外：feishu-agent 分支

`feishu-agent` 是跟踪官方安装包的独立分支（与 `main` 没有共同历史），不走本流程、不打 `x.y.z` 标签、永不合并进 `main`。维护方式见该分支的 `feishu-agent-fromandy/README.md`。
