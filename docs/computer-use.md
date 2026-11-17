# Computer Use 与飞书验收

## 目标确认

- 确认 Computer Use 连接的是 MacBook Pro2，而不是当前开发机或其他远程窗口。
- 飞书目标必须是 `Her remote · Pro`；窗口可能显示 `Her remote · Lin’s MacBook Pro`。不要误发给“林森的her”。
- 真实消息、菜单和群聊验收只在目标机器的飞书窗口中进行。

## 操作节奏

- 首次使用或重置后先调用一次 `getState()`。
- 应用名称解析失败时用 bundle id，例如 `com.electron.lark`。
- 每次操作后重新读取 `getAXState()`，只使用最新元素编号。
- 原生应用优先用可访问性元素；目标不清楚时用截图辅助。
- 打开指定私聊或群时用飞书客户端协议：`open "lark://applink.feishu.cn/client/chat/open?openChatId=<chat_id>"`。不要用 `open "https://applink.feishu.cn/..."`，它会先在默认浏览器里弹出「即将在飞书中打开」中转页，打断林森当前的窗口。
- 能在飞书里点会话列表就不用链接跳转；会话列表会因新消息重排，按坐标点之前先读最新的可访问性树或截图。
- 观察到最终结果后停止操作，并只调用一次 `completion_summary`。

## 飞书验收顺序

1. 打开 `Her remote · Pro` 私聊并确认发送对象。
2. 发送最小测试消息，确认机器人回复。
3. 执行目标功能，例如 `/ns codex ~/work/test1115`。
4. 核对目标目录、新群、首轮结果和群内续聊。
5. 验证写权限时创建测试文件并读取内容确认。
6. 菜单更新后重新进入或刷新私聊，确认菜单文字实际出现。

## 失败判定

- `ScreenCaptureKit SCStreamErrorDomain -3811/-3812` 表示屏幕捕捉链路失败；不能把点击返回或服务端 API 成功当成 UI 验收成功。恢复后必须重新读取目标窗口。
- accessibility tree 没变化时先做明确操作或改用截图 / 全量树，不要无间隔重复读取。
- 只读到历史消息只能证明历史存在，不能证明本次操作成功。
