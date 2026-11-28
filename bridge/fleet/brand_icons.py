'''总览卡分组标题前的三家品牌图标（第 36 轮，主人原话「换成他们自己的图标比如claude cursor codex」）。

飞书卡片里放自定义小图只认 `img_key`（`POST /open-apis/im/v1/images`，image_type=message 上传后拿到），
不能直接给 URL / base64。所以：
- 图本身放在这个模块里（64×64 PNG 的 base64，下面 `_PNG_B64`）—— 同事机器上没装某个 App 也有图，
  安装器照常按 `@@EM` + `BED` 嵌 .py 就能带上，不用改嵌入机制。
- 桥起来后**后台线程**上传一次（`start`），key 按 **app_id** 缓存到 `~/.carher-feishu-agent/brand_icons.json`
  （0600）。每个同事是自己的飞书应用 ⇒ 每台桥各传各的，换了应用不会拿别人的 key。
- 画卡时只读内存（`key_for`），绝不在卡片回调（3 秒）里现传。

🔴 拿不到 key 一律回落原来的 🤖：没开 `im:resource` 权限（实测本机机器人 2026-11-16 上传返回
99991672）、网络不通、缓存坏了 —— 总览卡绝不能因为图标画不出来。
🔴 卡片里只要有一个飞书不认的 img_key，**整张卡**被拒（实测 code=230099 / ErrCode 200570
「card contains invalid image keys」）⇒ app.py 发总览失败时调 `disable()` 去掉图标再发一次。
'''

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("feishu-bridge.fleet")

FALLBACK = "🤖"
CACHE_NAME = "brand_icons.json"
# 飞书返回的 image_key 长这样：img_v3_0238_073f1823-df2b-...；缓存里读出别的东西一律不用
_KEY_RE = re.compile(r"^img_v\d+_[A-Za-z0-9_-]{1,200}$")

_lock = threading.Lock()
_active: Optional["IconStore"] = None
_disabled = False
_started = False


def png(tool: str) -> bytes:
    """这一家的图（PNG 字节）；没有这一家 ⇒ b""。"""
    data = _PNG_B64.get(tool, "")
    return base64.b64decode(data) if data else b""


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key))


class IconStore:
    """`{"apps": {app_id: {tool: img_key}}}`。只认自己 app_id 那一格；写的时候保留别的 app_id（同一台机器换过应用）。"""

    def __init__(self, path: str, app_id: str) -> None:
        self.path = path
        self.app_id = str(app_id or "")
        self._keys: dict = {}
        self._load()

    def _read_all(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) and isinstance(data.get("apps"), dict) else {"apps": {}}
        except FileNotFoundError:
            return {"apps": {}}
        except Exception:  # noqa: BLE001 —— 缓存坏了 = 当没有，下次重新传
            logger.info("品牌图标缓存读不出来，当没有", exc_info=True)
            return {"apps": {}}

    def _load(self) -> None:
        mine = self._read_all()["apps"].get(self.app_id) if self.app_id else None
        mine = mine if isinstance(mine, dict) else {}
        self._keys = {t: k for t, k in mine.items() if t in _PNG_B64 and valid_key(k)}

    def keys(self) -> dict:
        return dict(self._keys)

    def put(self, tool: str, key: str) -> None:
        if not (self.app_id and tool in _PNG_B64 and valid_key(key)):
            return
        data = self._read_all()
        apps = data["apps"]
        mine = apps.get(self.app_id) if isinstance(apps.get(self.app_id), dict) else {}
        mine[tool] = key
        apps[self.app_id] = mine
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".brand_icons.", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
        self._keys[tool] = key


def bind(store: Optional[IconStore]) -> Optional[IconStore]:
    """把这份缓存设成画卡时读的那份（测试里传 None 解绑）。顺带清掉 `disable()`。"""
    global _active, _disabled
    with _lock:
        _active = store
        _disabled = False
    return store


def disable(reason: str = "") -> None:
    """这个进程里不再画图标（缓存不动；重启后再试）。发总览卡失败时 app.py 调。"""
    global _disabled
    with _lock:
        if not _disabled:
            logger.warning("总览卡先不带品牌图标了：%s", reason or "（没说原因）")
        _disabled = True


def key_for(tool: str) -> str:
    """画卡时用：这一家的 img_key；没有 / 关掉了 / 出任何错 ⇒ ""（调用方回落 🤖）。"""
    try:
        with _lock:
            store, off = _active, _disabled
        if off or store is None:
            return ""
        key = store.keys().get(tool, "")
        return key if valid_key(key) else ""
    except Exception:  # noqa: BLE001
        return ""


def card_uses_icons(card) -> bool:
    return isinstance(card, str) and '"custom_icon"' in card


def ensure(store: IconStore, upload: Callable[[str, bytes], Optional[str]]) -> dict:
    """缓存里缺的那几家各传一次。🔴 不抛：传不上就记一行日志、照旧回落 🤖。

    第一张就失败（多半是没开 `im:resource`）⇒ 后面两张不再试 —— 三次一模一样的 99991672 没有意义。
    """
    try:
        have = store.keys()
        for tool in _PNG_B64:
            if tool in have:
                continue
            try:
                key = upload(tool, png(tool))
            except Exception:  # noqa: BLE001
                logger.info("品牌图标 %s 上传抛异常，总览先用 %s", tool, FALLBACK, exc_info=True)
                key = None
            if not valid_key(key):
                break
            try:
                store.put(tool, key)
            except Exception:  # noqa: BLE001 —— 写不进缓存：这次进程里也不用（重启后再传）
                logger.info("品牌图标缓存写不进去", exc_info=True)
                break
        return store.keys()
    except Exception:  # noqa: BLE001
        logger.info("品牌图标准备失败", exc_info=True)
        return {}


def lark_uploader(client) -> Callable[[str, bytes], Optional[str]]:
    """用 lark-oapi 的 `im.v1.image.create`（image_type=message）上传，返回 image_key；失败 ⇒ None。"""

    def upload(tool: str, data: bytes) -> Optional[str]:
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        if not data:
            return None
        request = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder().image_type("message").image(io.BytesIO(data)).build()
            )
            .build()
        )
        response = client.im.v1.image.create(request)
        if not response.success():
            # 99991672 = 应用没开 im:resource（要在开发者后台加权限、重新发版本）
            logger.info("品牌图标 %s 上传被拒 code=%s msg=%s，总览先用 %s",
                        tool, response.code, str(response.msg or "")[:120], FALLBACK)
            return None
        return getattr(getattr(response, "data", None), "image_key", None)

    return upload


# 🔴 权限是**启动后**才补上的（menu_heal 补申请 → 发版 → 生效，几秒到几分钟）⇒ 启动那一下传不上很正常，
# 隔一阵再试；全传上了就停。总共试 RETRY_TIMES 次（约 2 小时），之后等下次重启。
RETRY_EVERY_SEC = 600
RETRY_TIMES = 12


def _ensure_with_retry(store: IconStore, upload: Callable[[str, bytes], Optional[str]],
                       sleep: Callable[[float], None] = time.sleep) -> dict:
    have: dict = {}
    for attempt in range(RETRY_TIMES):
        have = ensure(store, upload)
        if all(t in have for t in _PNG_B64):
            if attempt:
                logger.info("品牌图标第 %d 次传上了", attempt + 1)
            return have
        if attempt + 1 < RETRY_TIMES:
            sleep(RETRY_EVERY_SEC)
    return have


def start(client, app_id: str, home: str) -> Optional[threading.Thread]:
    """桥启动时调一次：绑缓存（已有的 key 立刻能用），缺的在后台线程里传。不抛、不阻塞。"""
    global _started
    try:
        store = bind(IconStore(os.path.join(home, CACHE_NAME), app_id))
        with _lock:
            if _started:
                return None
            _started = True
        if len(store.keys()) == len(_PNG_B64):
            return None
        thread = threading.Thread(target=_ensure_with_retry, args=(store, lark_uploader(client)),
                                  daemon=True, name="brand-icons")
        thread.start()
        return thread
    except Exception:  # noqa: BLE001
        logger.info("品牌图标没起来，总览用 %s", FALLBACK, exc_info=True)
        return None


# ---------- 图（64×64 PNG，从本机 .icns 用 `sips -s format png -z 64 64` 转出来的）----------
# Claude.app/Contents/Resources/electron.icns、OpenAI Codex.app/…/electron.icns、Cursor.app/…/Cursor.icns
_PNG_B64 = {
    "claude": (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAABGdBTUEAALGPC/xhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA"
        "6AAAdTAAAOpgAAA6mAAAF3CculE8AAAAeGVYSWZNTQAqAAAACAAEARoABQAAAAEAAAA+ARsABQAAAAEAAABGASgAAwAAAAEA"
        "AgAAh2kABAAAAAEAAABOAAAAAAAAAJAAAAABAAAAkAAAAAEAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAA"
        "QAAAAACU3PoRAAAACXBIWXMAABYlAAAWJQFJUiTwAAABzWlUWHRYTUw6Y29tLmFkb2JlLnhtcAAAAAAAPHg6eG1wbWV0YSB4"
        "bWxuczp4PSJhZG9iZTpuczptZXRhLyIgeDp4bXB0az0iWE1QIENvcmUgNi4wLjAiPgogICA8cmRmOlJERiB4bWxuczpyZGY9"
        "Imh0dHA6Ly93d3cudzMub3JnLzE5OTkvMDIvMjItcmRmLXN5bnRheC1ucyMiPgogICAgICA8cmRmOkRlc2NyaXB0aW9uIHJk"
        "ZjphYm91dD0iIgogICAgICAgICAgICB4bWxuczpleGlmPSJodHRwOi8vbnMuYWRvYmUuY29tL2V4aWYvMS4wLyI+CiAgICAg"
        "ICAgIDxleGlmOkNvbG9yU3BhY2U+MTwvZXhpZjpDb2xvclNwYWNlPgogICAgICAgICA8ZXhpZjpQaXhlbFhEaW1lbnNpb24+"
        "MTAyNDwvZXhpZjpQaXhlbFhEaW1lbnNpb24+CiAgICAgICAgIDxleGlmOlBpeGVsWURpbWVuc2lvbj4xMDI0PC9leGlmOlBp"
        "eGVsWURpbWVuc2lvbj4KICAgICAgPC9yZGY6RGVzY3JpcHRpb24+CiAgIDwvcmRmOlJERj4KPC94OnhtcG1ldGE+CsHtO6kA"
        "ABcYSURBVHgB7VtpkFzVdT79XnfPdE/PvmlmhMS+msUIBBQmgADhGBHHrCEBi0qqqMShChJjJ/+CUyExMZWtnEpwKgaxBMTi"
        "Yt8UEwjGiM0GIwTCbBLSjDT7aLbued3v5fu++97MSJoeRPIzuqPX7727nHvOd5Z77u2W2YFyAIEDCBxA4AAC/38RSP1fRP/w"
        "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"
    ),
    "codex": (
        "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"
        "GAlenyYDp8AAAAAASUVORK5CYII="
    ),
    "cursor": (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAABGdBTUEAALGPC/xhBQAAACBjSFJNAAB6JgAAgIQAAPoAAACA"
        "6AAAdTAAAOpgAAA6mAAAF3CculE8AAAAeGVYSWZNTQAqAAAACAAEARoABQAAAAEAAAA+ARsABQAAAAEAAABGASgAAwAAAAEA"
        "AgAAh2kABAAAAAEAAABOAAAAAAAAAJAAAAABAAAAkAAAAAEAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAA"
        "QAAAAACU3PoRAAAACXBIWXMAABYlAAAWJQFJUiTwAAABy2lUWHRYTUw6Y29tLmFkb2JlLnhtcAAAAAAAPHg6eG1wbWV0YSB4"
        "bWxuczp4PSJhZG9iZTpuczptZXRhLyIgeDp4bXB0az0iWE1QIENvcmUgNi4wLjAiPgogICA8cmRmOlJERiB4bWxuczpyZGY9"
        "Imh0dHA6Ly93d3cudzMub3JnLzE5OTkvMDIvMjItcmRmLXN5bnRheC1ucyMiPgogICAgICA8cmRmOkRlc2NyaXB0aW9uIHJk"
        "ZjphYm91dD0iIgogICAgICAgICAgICB4bWxuczpleGlmPSJodHRwOi8vbnMuYWRvYmUuY29tL2V4aWYvMS4wLyI+CiAgICAg"
        "ICAgIDxleGlmOkNvbG9yU3BhY2U+MTwvZXhpZjpDb2xvclNwYWNlPgogICAgICAgICA8ZXhpZjpQaXhlbFhEaW1lbnNpb24+"
        "NTEyPC9leGlmOlBpeGVsWERpbWVuc2lvbj4KICAgICAgICAgPGV4aWY6UGl4ZWxZRGltZW5zaW9uPjUxMjwvZXhpZjpQaXhl"
        "bFlEaW1lbnNpb24+CiAgICAgIDwvcmRmOkRlc2NyaXB0aW9uPgogICA8L3JkZjpSREY+CjwveDp4bXBtZXRhPgoAheCYAAAJ"
        "bUlEQVR4Ae1bW2wU1xn+Z3Z9wxeW3TU2BrzYzq21SSCoLWqfEElooqqQUEGDVCkvfajUpzxFat8qtY9VpFaRIjVVHiMhocak"
        "BQK4pGACNb5hQmobY4xZr70Xr71rY3u9M/2/Y89q2c7sbY6Jq+yR1jN7Lv/83/dfzmXWRKVSYqDEQImBEgPfXgaUYqHvrK31"
        "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"
    ),
}
