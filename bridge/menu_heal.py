"""常驻菜单自愈：装机那次没写成，桥跑起来之后自己补上 —— 而且**成功一次就永远不再写**。

## 为什么要有它（第 12 轮）

悬浮菜单原来只在装机那一刻写一次（安装器跑 `botmenu.py` 的 `setup()`）。那一刻写不成
（最常见：`application:application:patch` 还在等管理员审批 ⇒ `write_menu` 回 99991672），
之后管理员批了、同事也发了版本，**没有任何东西会再写一次** ⇒ 永远没有悬浮菜单。

## 怎么判「菜单在不在」：本地落盘标记，不是查接口

飞书没有「读机器人菜单」的接口能给我们用（2026-11-06 读 SDK 1.7.5 源码 `lark_oapi/api/application/`）：
- v7 `application_ability` 只有 `patch` / `apatch`，没有 get。
- v6 `Application`（`GET /application/v6/applications/:app_id`）的字段里没有 `ability`。
- 唯一带 `ability.bot.bot_menus` 的是 v6 `ApplicationAppVersion`（`GET .../app_versions/:version_id`），
  但它要 `application:application:self_manage` 或 `application:application.app_version:readonly` ——
  2026-11-06 只读实测本机应用两个都没有（99991672，见 `botmenu.py` 版本状态那段），
  我们装机申请的权限清单（`register.WANT_SCOPES`）里也没有。

所以退一步：**这个 app_id 的菜单写成功 + 订阅成功 + 版本提交成功** ⇒ 在 config.toml 旁边落一个
`menu_heal.json` 标记。有标记就不再写、不再发版、不碰网络。安装器那次成功了也落同一个标记
（`botmenu.py` 的 `__main__`），所以装机成功的机器上这里一个请求都不会发。

## 🔴 为什么「成功一次就永远不再写」

每写一次菜单都得发一个版本才生效；企业开了审核时**每次发版都要管理员批一次**。
桥会被看门狗反复重启 —— 要是每次启动都写 + 发版，一天能给管理员发十几条审批。
- 成功 ⇒ 落标记，之后永不再写（换了机器人 = 换了 app_id ⇒ 标记按 app_id 记，新机器人会补一次）
- 失败 ⇒ **不发版**（菜单都没写进去，发了也没东西）
- 失败后 30 分钟才重试；上次失败的时间也落盘，看门狗重启不会把重试节奏打快

## 节奏

启动后 1 分钟第一次（别和长连接、开场消息挤在一起）；失败每 30 分钟一次；成功就停，线程退出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time

logger = logging.getLogger("feishu-bridge")

STATE_FILENAME = "menu_heal.json"
FIRST_DELAY_SEC = 60
RETRY_EVERY_SEC = 30 * 60
# 缺的是这一项时，后台权限页直接带上它
_PATCH_SCOPE = "application:application:patch"


def _botmenu():
    """两种导入方式都兼容（同 `botmenu._reg`）：`botmenu.py` 的脚本入口也会 import 本模块。"""
    try:
        from . import botmenu  # 包方式
    except ImportError:
        import botmenu  # type: ignore[no-redef]  # 脚本方式
    return botmenu


def state_path_for(config_path: str) -> str:
    """标记文件放在 config.toml 旁边（和 paused.json / approvals 同一套取法）。"""
    home = os.path.dirname(config_path or "") or os.path.expanduser("~/.carher-feishu-agent")
    return os.path.join(home, STATE_FILENAME)


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    return apps if isinstance(apps, dict) else {}


def _save(path: str, apps: dict) -> None:
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".menu_heal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"apps": apps}, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record(path: str, app_id: str) -> dict:
    entry = _load(path).get(app_id)
    return entry if isinstance(entry, dict) else {}


def _update(path: str, app_id: str, **fields) -> None:
    apps = _load(path)
    entry = apps.get(app_id) if isinstance(apps.get(app_id), dict) else {}
    entry.update(fields)
    apps[app_id] = entry
    _save(path, apps)


def scopes_fingerprint() -> str:
    """我们**现在**想要的那套权限清单 + 悬浮菜单形状的指纹。任何一个变了它就变。

    （函数名保留旧的：安装器 / 测试 / `menu_heal.json` 的 `scopes` 键都按这个名字认。）

    🔴 为什么要有（第 17 轮，角色 F 调研流式时撞出来）：
    装机时申请权限只发生一次，而 `done: true` 之后自愈线程直接返回、不再碰权限
    ⇒ **以后新增的 scope 永远到不了老用户手上**。现场实测：本机生产应用 13 项 scope、
    流式要的 `cardkit` 一项都没有，而 `menu_heal.json` 已经是 `done: true`。
    两边都看不出来：用户那边是「新功能没反应」，我们这边是「我发版了啊」。

    判据放在**我们要什么**（`register.WANT_SCOPES`）上，不是「服务端现在有什么」——
    后者要发请求，而这个函数在判「要不要重跑」时被调用，不能依赖网络。
    """
    try:
        try:
            from .register import WANT_CALLBACKS, WANT_SCOPES   # 包方式
        except ImportError:
            # 🔴 脚本方式：安装器跑 `python bridge/botmenu.py`，没有父包。
            # 这个文件上面 `botmenu` 的 import 已经是这个形状（`menu_heal.py:57-59`），
            # 漏了这一处会让指纹恒为空串 ⇒ 判据静默失效（`test_r12_menu_heal` 当场抓到）。
            from register import WANT_CALLBACKS, WANT_SCOPES  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001 —— 认不出就返回空串 ⇒ 退回老行为（done 仍然算 done）
        return ""
    material = "\n".join(sorted(WANT_SCOPES) + sorted(WANT_CALLBACKS))
    # 🔴 2026-11-10（悬浮栏加「🌐 网页版」）：菜单的形状也要进指纹。原来只看权限清单 ⇒
    # 改了 `botmenu.MENU_TREE`，老机器 `done: true` + 指纹不变 ⇒ 自动更新跳过、自愈也不写 ⇒
    # **新菜单永远到不了老用户**（和第 17 轮新 scope 到不了是同一个坑）。
    # 代价：菜单每改一次，每个应用会多发一个版本（企业开了审核 = 管理员多批一次）——
    # 这正是让新菜单生效必须付的那一次，不会反复：写成后指纹对上就停。
    try:
        menus = _botmenu().build_bot_menus()
    except Exception:  # noqa: BLE001 —— 读不出菜单 ⇒ 只按权限清单算（老行为）
        menus = None
    if menus is not None:
        material += "\nmenu:" + json.dumps(menus, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def is_done(path: str, app_id: str) -> bool:
    """这个 app 的菜单 / 权限 / 订阅都装好了吗。

    🔴 「装好了」要连**装的是哪一套清单**一起看（见 `scopes_fingerprint`）：
    - 记录里没有 `scopes` 字段 ⇒ 是这一轮之前写的（现场所有老机器都是这样）⇒ 当成没装好，
      自愈会再跑一次把新权限补上
    - 指纹和现在的清单不一样 ⇒ 我们新增过 scope ⇒ 同上
    - 一样 ⇒ 照旧「成功一次永不再写」（这个设计是对的，别退回「每次启动都申请」）
    """
    if not app_id:
        return False
    record = _record(path, app_id)
    if record.get("done") is not True:
        return False
    want = scopes_fingerprint()
    if not want:
        return True          # 认不出清单 ⇒ 不要因为判不出就反复申请
    return record.get("scopes") == want


def mark_done(path: str, app_id: str, version: str, *, at: float | None = None) -> None:
    """菜单写成 + 订阅成 + 版本提交成 ⇒ 落标记。安装器（`botmenu.py` 脚本入口）和自愈线程共用。

    连**这一次装的是哪套权限清单**一起记（`scopes`）—— 以后清单变了才知道要重跑。
    """
    _update(path, app_id, done=True, version=version, at=at if at is not None else time.time(),
            scopes=scopes_fingerprint(), last_error="")


class MenuHeal:
    """一个 app_id 的菜单自愈。`http` / `clock` 注入进来：测试里一个真请求都不发、不真等。

    线程模型：`run_forever()` 跑在自己的 daemon 线程里；`health_text()` 可以从别的线程读
    （只读几个简单属性）。
    """

    def __init__(self, app_id: str, app_secret: str, state_path: str, *,
                 http=None, clock=time.time):
        self.app_id = app_id
        self.app_secret = app_secret
        self.state_path = state_path
        self._http = http
        self._clock = clock
        self._done = is_done(state_path, app_id)
        self.attempts = 0
        self.last_reason = ""  # 给诊断卡的短句；空 = 还没失败过
        record = _record(state_path, app_id)
        last_try = record.get("last_try")
        now = clock()
        self._next_at = now + FIRST_DELAY_SEC
        if isinstance(last_try, (int, float)) and not isinstance(last_try, bool):
            # 看门狗重启后接着上次的节奏：上次失败不到 30 分钟 ⇒ 等满 30 分钟
            self._next_at = max(self._next_at, last_try + RETRY_EVERY_SEC)
            self.last_reason = str(record.get("last_error") or "")

    # ---------- 节奏 ----------

    @property
    def done(self) -> bool:
        return self._done

    def next_delay(self) -> float | None:
        """离下一次尝试还有几秒；`None` = 不用再跑了（已成功）。"""
        if self._done:
            return None
        # 时钟往回拨过（上次尝试的时间在「将来」）⇒ 最多等一个重试间隔，不会一等好几天
        return min(max(0.0, self._next_at - self._clock()), float(RETRY_EVERY_SEC))

    def run_forever(self, sleep=time.sleep) -> None:
        """等 → 试一次 → 成功就退出；失败等 30 分钟再试。一拍出异常不让线程死。"""
        while True:
            delay = self.next_delay()
            if delay is None:
                return
            if delay > 0:
                sleep(delay)
            try:
                self.attempt()
            except Exception:  # noqa: BLE001 —— 菜单是锦上添花，不许把桥带崩
                logger.exception("常驻菜单自愈这一次出错，30 分钟后再试")
                self._failed("程序出错")

    # ---------- 一次尝试 ----------

    def attempt(self) -> bool:
        """写菜单 → 订阅点击 → 发版。成功落标记返回 True；任何一步失败都**不发版**。"""
        if self._done:
            return True
        # 安装器（重装）可能刚落了标记 —— 每次动手前都回读一次，别和它各发一版
        if is_done(self.state_path, self.app_id):
            self._done = True
            return True
        self.attempts += 1
        bm = _botmenu()
        call = self._http or bm._http
        try:
            token = bm.tenant_token(self.app_id, self.app_secret, http=call)
        except RuntimeError as exc:
            return self._failed("拿不到 token", detail=exc)

        out = bm.write_menu(self.app_id, token, http=call)
        if out.get("code"):
            return self._failed_with(out, "写菜单")

        # 写菜单缺权限时，订阅（同一个 patch 权限）当初多半也失败了 ⇒ 菜单出来了点了没反应。
        # 增量语义（add_events / add_callbacks），重复订阅无副作用。
        sub = bm.subscribe(self.app_id, token, http=call)
        if sub.get("code"):
            return self._failed_with(sub, "订阅按钮点击")

        # 发版前再回读一次标记：安装器可能就在这几秒里发过了
        if is_done(self.state_path, self.app_id):
            self._done = True
            return True
        version = bm.version_for_time(self._clock())
        # 措辞不再写「装机时没配上」：菜单改了形状（指纹变了）也走这里，那时装机是好的。
        pub = bm.publish(self.app_id, token, version, "补上或更新常驻菜单", http=call)
        if pub.get("code"):
            return self._failed_with(pub, "发布版本", table=bm.PUBLISH_ERRORS)

        shown = (pub.get("data") or {}).get("version") or version
        self._done = True
        self.last_reason = ""
        try:
            mark_done(self.state_path, self.app_id, shown, at=self._clock())
        except OSError:
            # 本进程里照样不再写；但下次重启会再补一次（= 多一次审批）。说出来。
            logger.exception("常驻菜单已补上，但标记没落盘 —— 下次重启会再发一次版本")
        logger.info("常驻菜单已补上并提交版本 %s（企业要审核的话等管理员批一下）", shown)
        return True

    def _failed_with(self, out: dict, step: str, *, table=None) -> bool:
        bm = _botmenu()
        missing = [s for s in bm._missing_scopes(out) if s]
        if missing:
            return self._failed(f"缺权限 {', '.join(missing)}", missing=missing)
        human = bm._human(out, table if table is not None else bm.CONFIG_ERRORS)
        return self._failed(f"{step}失败 code={out.get('code')}", detail=human)

    def _failed(self, short: str, *, detail="", missing=()) -> bool:
        """记一次失败：日志一行（给 IT 看的细节只进日志）+ 诊断卡短句 + 落盘失败时间。"""
        now = self._clock()
        self._next_at = now + RETRY_EVERY_SEC
        self.last_reason = short
        if missing:
            logger.warning(
                "常驻菜单还没配上（第 %d 次）：应用还没有「%s」权限（多半还在等管理员审批），"
                "手动开 https://open.feishu.cn/app/%s/auth?q=%s 。30 分钟后自动再试，没发版本。",
                self.attempts, "、".join(missing), self.app_id, ",".join(missing),
            )
        else:
            logger.warning("常驻菜单还没配上（第 %d 次）：%s（%s）。30 分钟后自动再试，没发版本。",
                           self.attempts, short, detail)
        try:
            _update(self.state_path, self.app_id, done=False, last_try=now, last_error=short)
        except OSError:
            logger.debug("记录菜单自愈失败时间没落盘", exc_info=True)
        return False

    # ---------- 诊断卡 ----------

    def health_text(self) -> str:
        """`/诊断` 卡上那一行的值（模板在 `status.HEALTH_FIELDS` 的 `menu` 键）。不含任何凭据。"""
        if self._done:
            return "✅ 已配好"
        if self.last_reason:
            return f"❌ {self.last_reason} · 每 30 分钟自动再试"
        return "⏳ 启动 1 分钟后检查"
