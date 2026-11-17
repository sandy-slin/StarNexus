"""聊天里能改、重启还在的两个设置：权限档位 和 模型。

## 为什么要有这个文件

用户原话：「可以上来就给我按钮菜单吗？打字太不方便了。」菜单里最该有的两项
（对照豆包工作输入框那一排的「按需确认」和「自动 高」）就是权限和模型。
但这两个值原来**只能改 config.toml 再重启服务** —— 手机上做不到。

## 边界

- **档位有天花板**：config.toml 的 mode = 本机上限；聊天里能在上限以内切换，收紧点一次，放开要二次确认
  （见 `policy.clamp_mode`）。IT 给同事装成 `readonly`，他点什么都提不上去。
- **模型是白名单**：`policy.MODEL_CHOICES`。这个值要进 `claude --model` 的 argv，
  拼错的名字会让每一轮都失败，而用户看到的只是"它不回我"。
- **读失败一律当没设过**：回落到 config.toml 的值。这个文件是便利设施不是权威，
  读不出来不能阻塞启动。

## 落盘写法

临时文件 + `os.replace()`，抄 `WorkspaceStore._save()`。直接 `open(w)` 中途断电会
留下半截 JSON ⇒ 下次启动读失败 ⇒ 设置一起丢。
看门狗在长连接静默时**会自杀等重启**（lark-oapi #169 的唯一保险），
所以"重启"在这套东西里是常态不是例外，内存态活不过一次重启等于没存。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from .policy import Policy, clamp_mode, known_model

logger = logging.getLogger("feishu-bridge.prefs")


class PrefsStore:
    """落盘的「用户在聊天里改过的设置」。

    只存**偏离 config.toml 的部分**，没改过就不写键 —— 这样用户之后改了
    config.toml（比如 IT 收紧成 readonly），没被覆盖的项会跟着变，
    而不是被一份旧的偏好文件钉死。
    """

    def __init__(self, path: str, base: Policy) -> None:
        self._path = path
        self._base = base
        self._mode = ""
        self._model = ""
        self.last_save_ok = True
        self._load()

    # ---------- 落盘 ----------

    def reload(self) -> None:
        """重新读一遍落盘的设置。

        第 16 轮：迁移时会把 `prefs.json` 里的旧只读档位删掉（`clear_readonly_override`），
        删完要让内存里这份跟着变，否则本次重启之前还是按旧的来。
        """
        self._mode = ""
        self._model = ""
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        mode = data.get("mode")
        if isinstance(mode, str) and mode:
            # 🔴 **必须重新过一遍天花板**，不能信磁盘。
            # config.toml 的 mode 会被改小（IT 收紧、或安装器重跑覆盖），
            # 而这个文件还留着旧档位 —— 直接采信等于让一次旧的点击永久提权。
            clamped = clamp_mode(mode, self._base.ceiling_mode)
            if clamped != mode:
                logger.info("落盘档位 %s 超过本机上限，压到 %s", mode, clamped)
            self._mode = clamped
        model = data.get("model")
        if isinstance(model, str) and known_model(model):
            self._model = model
        elif model:
            logger.info("落盘模型 %r 不在白名单里，忽略", model)

    def _save(self) -> bool:
        payload = {}
        if self._mode:
            payload["mode"] = self._mode
        if self._model:
            payload["model"] = self._model
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory or None, delete=False
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self._path)
            self.last_save_ok = True
            return True
        except OSError:
            logger.exception("设置落盘失败：%s", self._path)
            self.last_save_ok = False
            return False

    # ---------- 对外 ----------

    def policy(self) -> Policy:
        """当前生效的 Policy。没改过就是 config.toml 那份（同一个对象）。"""
        policy = self._base
        if self._mode:
            policy = policy.with_mode(self._mode)
        if self._model:
            policy = policy.with_model(self._model)
        return policy

    def set_mode(self, requested: str) -> tuple[str, bool]:
        """切档。返回（真正生效的档位, 是不是被天花板压过）。"""
        target = clamp_mode(requested, self._base.ceiling_mode)
        clamped = target != requested
        # 🔴 和 config.toml 一样就**清掉覆盖**，不记下来。
        # 原来点一下「✅ 只读」（当前档）也会写 {"mode":"readonly"}，之后 IT 把
        # config.toml 放开到 write 并重启，生效的还是 readonly —— 被一次无意义的点击钉死了。
        self._mode = "" if target == self._base.mode else target
        self._save()
        return target, clamped

    def set_model(self, name: str) -> bool:
        """换模型。白名单外返回 False，不落盘。"""
        if not known_model(name):
            return False
        self._model = "" if name == self._base.model else name
        self._save()
        return True


def clear_readonly_override(path: str) -> bool:
    """把落盘的 `{"mode": "readonly"}` 删掉（只删 mode 这一个键）。删了返回 True。

    🔴 第 16 轮现场（2026-11-07 14:04 用户截图）：更新完卡片还显示「现在是 只读」。
    配置里已经迁移成 trusted，但这个文件里留着老版本点过的 `{"mode": "readonly"}`，
    而 `PrefsStore._load` 只按**上限**收敛、不看 config 里的档 ⇒ 它原样顶掉新默认。

    为什么删它安全：旧默认（只读）下**主动选只读在这里不留痕迹** ——
    `set_mode` 在目标档等于 config 档时会清掉覆盖。所以留下 `"mode": "readonly"` 的
    只可能是更早版本点了一下当前档留下的噪音。`trusted` / `write` 一律保留（那是他自己选的）。
    只在**真的发生迁移**那一次调用（见 `app._tell_about_migration`）。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("mode") != "readonly":
        return False
    data.pop("mode", None)
    try:
        directory = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         delete=False) as tmp:
            json.dump(data, tmp, ensure_ascii=False)
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except OSError:
        logger.warning("清掉落盘的只读档位失败：%s", path)
        return False
    logger.info("迁移：清掉了落盘的旧只读档位（它会顶掉新的默认全控制）")
    return True
