# Remote Control 协作入口

本仓库由 Codex 和 Claude 共同开发。两者必须遵循同一份规则：本文件是项目级入口，具体操作细节按下面的索引读取，不在这里复制完整流程。

## 开始任务

- 先检查 `git status`，保留已有修改，不覆盖用户改动。
- 先读与当前任务相关的细节文档，再修改代码。
- 代码逻辑发生变化时，必须完成仓库测试、同步本机运行目录、重启后台服务，并核对运行状态。
- 只在目标 bot `Her remote · Pro` 和目标 MacBook Pro2 上做真实飞书验收；不要误操作“林森的her”。

## 细节文档索引

| 任务 | 必读文档 | 关键要求 |
|---|---|---|
| Computer Use、飞书真实验收、菜单更新、后台同步 | [`docs/operations.md`](docs/operations.md) | 按四层证据逐层验收；客户端画面未确认时不能宣布完成 |
| 常驻菜单结构、节点限制、事件订阅和版本发布 | [`bridge/botmenu.py`](bridge/botmenu.py)、[`bridge/menu_heal.py`](bridge/menu_heal.py) | 先跑 `validate_tree()`；菜单形状变化必须发布新版本 |
| 菜单版本号回归 | [`tests/test_menu_version.py`](tests/test_menu_version.py) | 覆盖整点前十分钟等前导零场景 |
| 新建会话、建群、模型和权限 | [`bridge/fleet/newsession.py`](bridge/fleet/newsession.py)、[`bridge/fleet/chatbind.py`](bridge/fleet/chatbind.py)、[`bridge/fleet/model.py`](bridge/fleet/model.py) | 以代码和测试为准，真实群聊必须在目标 bot 上核对 |
| 群进展通知 | [`bridge/fleet/grouppush.py`](bridge/fleet/grouppush.py) | 需要人介入必须推送；普通过程更新不刷屏 |
| CEO 编排 | [`bridge/fleet/ceo.py`](bridge/fleet/ceo.py) | CEO 负责拆分、派发、汇总、仲裁和向人请求决策 |

## 代码变更后的最低门禁

1. 运行针对性测试和 Python 语法检查。
2. 运行完整测试；依赖缺失时记录为环境阻塞，不能把部分通过写成全量通过。
3. 提交 Git，记录提交 SHA。
4. 备份并同步 `bridge/` 到 `~/.carher-feishu-agent/bridge/`。
5. 核对关键文件哈希，重启 `com.carher.feishu-agent`，确认服务 `running`。
6. 对飞书功能做 Computer Use 实测；只看到服务端回执不算客户端验收通过。

## 结果记录

关键过程、根因、提交、测试、部署状态和遗留问题写入项目记录。需要林森事后关注的事项使用红色字体标注；不要记录密钥、access token、app secret 或完整授权链接。
