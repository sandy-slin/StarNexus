# Her Remote 操作手册索引

本文件只负责导航。具体流程按责任边界分为几份，避免一份大文档同时承载 Computer Use、菜单发布和运行交付。

| 场景 | 细节文档 |
|---|---|
| Computer Use 和飞书真实验收 | [`computer-use.md`](computer-use.md) |
| 常驻菜单修改、发布和客户端刷新 | [`menu-update.md`](menu-update.md) |
| 开发分支、合并回 main、版本标签 | [`dev-release.md`](dev-release.md) |
| 后台同步、回归测试、Git 交付和结果记录 | [`runtime-delivery.md`](runtime-delivery.md) |

通用原则：先读取对应细节文档；服务端回执、后台进程状态和客户端实际画面分别取证；所有交付物必须提交到 Git 并在交付说明中报告 SHA。

不要在本索引追加具体操作步骤；新增规则应放入上面几份文档中最匹配的一份。
