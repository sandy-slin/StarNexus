# 后台同步、回归与 Git 交付

## 代码变更后的顺序

1. 先跑针对性测试、语法检查和 `git diff --check`。
2. 用项目虚拟环境跑完整测试；依赖缺失时记录环境阻塞，不把部分通过写成全量通过。
3. 将代码、测试、文档等交付物加入 Git，提交并记录完整 SHA。
4. 备份 `~/.carher-feishu-agent/bridge`，同步仓库 `bridge/`。
5. 至少核对 `bridge/botmenu.py` 和 `bridge/menu_heal.py` 的文件哈希。
6. 重启 `com.carher.feishu-agent`，确认 launchd 为 `running`，日志出现飞书连接成功。
7. 按 [`computer-use.md`](computer-use.md) 做真实验收，按 [`menu-update.md`](menu-update.md) 做菜单验收。

## Git 交付门禁

- 所有交付文件必须在仓库中，不能只存在于 `/tmp`、运行目录或聊天附件。
- 交付前检查 `git status --short --branch`：交付文件不能是 `??`，也不能有未说明的修改。
- 最终说明必须给出提交 SHA、交付文件路径、测试结果、部署状态和遗留问题。
- 临时日志、密钥、access token、app secret 和完整授权链接不得提交。

## 结果记录

项目记录写根因、决策、提交、测试、同步、重启、飞书版本和客户端证据。需要林森事后关注的事项用红色字体标注。项目记录不替代 Git 交付。
