# 后台同步、回归与 Git 交付

## 代码变更后的顺序

1. 先跑针对性测试、语法检查和 `git diff --check`。
2. 用项目虚拟环境跑完整测试；依赖缺失时记录环境阻塞，不把部分通过写成全量通过。
3. 将代码、测试、文档等交付物加入 Git，提交并记录完整 SHA。
4. 备份 `~/.carher-feishu-agent/bridge`，同步仓库 `bridge/`。
5. 至少核对 `bridge/botmenu.py` 和 `bridge/menu_heal.py` 的文件哈希。
6. 重启 `com.carher.feishu-agent`，确认 launchd 为 `running`，日志出现飞书连接成功。
7. 按 [`computer-use.md`](computer-use.md) 做真实验收，按 [`menu-update.md`](menu-update.md) 做菜单验收。

## 不让安装包覆盖仓库代码

本机运行目录用仓库代码同步，不走安装包。`~/.carher-feishu-agent/.local-deploy` 存在时，自动更新一律跳过（`bridge/selfupdate.py`）。2026-11-17 04:43 发生过一次：自动更新装回 20261116 的旧包，覆盖了已修好的 `codex queue` 参数等改动，群里发给 Codex 的消息全部失败。这个标记文件不要删；每次同步后核对一次关键文件里的新函数还在。

## Git 交付门禁

- 所有交付文件必须在仓库中，不能只存在于 `/tmp`、运行目录或聊天附件。
- 交付前检查 `git status --short --branch`：交付文件不能是 `??`，也不能有未说明的修改。
- 最终说明必须给出提交 SHA、交付文件路径、测试结果、部署状态和遗留问题。
- 临时日志、密钥、access token、app secret 和完整授权链接不得提交。

## 推送到 GitHub

- 远端：`origin` = `git@github.com:sandy-slin/StarNexus.git`，分支 `main`。
- 核心更新提交后立即推送：功能代码、测试、产品定义、操作手册、菜单变更。不要攒到最后一起推。
- 每次推送前先把远端同步到本地，顺序固定：
  1. `git fetch origin`，再 `git pull --rebase origin main`。Codex 和 Claude 都可能推送过，不能假设本地是最新的。
  2. 有冲突就在本地解决，保留双方的有效修改；不能用丢弃对方修改的方式解决。
  3. 同步进来新提交时，重跑完整测试；代码有变化还要按本文“代码变更后的顺序”同步运行目录并重启服务。
  4. 用 `git diff origin/main..main` 扫一遍密钥、access token、app secret、私钥；有命中就先移除，不推送。
  5. 最后 `git push origin main`。
- 只用普通推送；不强推、不改写已推送的历史。推送仍被拒时，回到第 1 步重新同步。
- 推送后 `git status -sb` 应显示与 `origin/main` 一致，交付说明里写明已推送的 SHA。

## 结果记录

项目记录写根因、决策、提交、测试、同步、重启、飞书版本和客户端证据。需要林森事后关注的事项用红色字体标注。项目记录不替代 Git 交付。
