# feishu-agent-fromandy：官方 feishu-agent 版本跟踪

本分支 `feishu-agent` 只跟踪官方 feishu-agent（Her Remote）安装包的每个版本，方便和 StarNexus 对比、挑选可借鉴的功能。

- 与 `main` 没有共同历史，**永远不合并进 `main`**，不打 `x.y.z` 标签，不走 StarNexus 的开发发版流程。
- 每个提交对应一个官方版本，提交说明第一行是 `upstream <build_id>`，正文是 AI 对比上一版生成的中文摘要（新增/改动、风险、对 StarNexus 的影响）。权限、自升级、向官方服务器上报这三类变化单独列出。

> ⚠️ **不要在 MacBook Pro2 上运行官方安装命令**（`curl … install.sh | bash` 或双击 `.command`）。官方安装器不认 StarNexus 的 `.local-deploy` 标记，会停掉服务、改写 `com.carher.feishu-agent` 启动配置，并用官方版整个替换 `~/.carher-feishu-agent/bridge/`。本分支的工具只下载、核对、解包，**从不运行**安装包。

## 目录

| 路径 | 内容 |
|---|---|
| `app_home/` | 安装包里嵌入的文件，按安装后的原路径还原（`app_home/bridge/...` 对应 `~/.carher-feishu-agent/bridge/...`） |
| `other/` | 安装包里写到 `$APP_HOME` 之外的文件（如 `DOCTOR`、`GUARD`） |
| `meta/BUILD` | 版本号 `build_id`、安装包 `sha256`、字节数、发布时间 |
| `tools/extract.py` | 解包：只读安装包里的 `write_file "$APP_HOME/..." <<'TAG'` 段，不执行任何内容 |
| `tools/sync.py` | 同步：查官方新版 → 下载 → 核 sha256 → 解包 → AI 摘要 → 提交 → 推送 |
| `tools/com.starnexus.feishu-agent-sync.plist` | 每日自动同步的 launchd 配置模板 |

原始安装包（约 1.8 MB）不入库，用 `meta/BUILD` 的 sha256 核对。

## 从 curl 方式拿到新版（手工步骤）

官方安装命令 `curl -fsSL https://chat.auto-link.com.cn/ccdl/feishu-agent/install.sh | bash` 做的是：读 `latest.json` → 下载 `feishu-agent-mac.command` → 核 sha256 → **用 bash 运行它**。本分支只做前三步，第四步换成解包：

```bash
BASE=https://chat.auto-link.com.cn/ccdl/feishu-agent
curl -fsSL "$BASE/latest.json" -o /tmp/latest.json          # 1. 读最新版本信息
cat /tmp/latest.json                                        #    看 build_id、mac.file、mac.sha256
curl -fsSL "$BASE/feishu-agent-mac.command" -o /tmp/fa.command   # 2. 下载安装包（不要 bash 运行）
shasum -a 256 /tmp/fa.command                               # 3. 与 latest.json 里 mac.sha256 一致才继续
cd <本分支工作区>/feishu-agent-fromandy
python3 tools/sync.py --file /tmp/fa.command --build <build_id> --published <published_at>   # 4. 解包+提交+推送
```

## 自动同步

```bash
python3 feishu-agent-fromandy/tools/sync.py            # 有新版就解包、提交、推送 origin/feishu-agent
python3 feishu-agent-fromandy/tools/sync.py --dry-run  # 只看有没有新版
```

- 每天 09:30 由 launchd 任务 `com.starnexus.feishu-agent-sync` 自动执行一次，日志在 `~/Library/Logs/starnexus-feishu-agent-sync.log`。
- 工作区固定在 `/Users/LinSen/work/remote_control-wt/feishu-agent`（StarNexus 仓库的 worktree），不要在里面做别的修改。
- 版本号与 `meta/BUILD` 相同 ⇒ 跳过；sha256 不符 ⇒ 报错退出，不提交。
- AI 摘要用本机 `codex exec`（只读）；失败时退回规则摘要（文件统计 + 风险关键词命中数），不会因此漏提交。

## 对比示例

```bash
git log --oneline feishu-agent                                  # 版本列表
git diff <旧提交> <新提交> -- feishu-agent-fromandy/app_home/bridge/selfupdate.py
git diff main:bridge/fleet/newsession.py feishu-agent:feishu-agent-fromandy/app_home/bridge/fleet/newsession.py
```
