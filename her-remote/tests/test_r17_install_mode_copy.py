"""第 17 轮：装完那一屏说「它现在是【只读】的，要改会先弹卡片问你」—— 而默认已经是全授权。

值守 15:13 报的，我复核了（`install-mac.sh:1253`、`install-windows.ps1:1294`）：那两行是
**无条件 echo** 的，不看实际档位。而同一个安装器写进配置的是 `mode = "trusted"`
（`install-mac.sh:839`）⇒ 用户装完第一眼看到的是「我是安全的、要改会问我」，
实际是**不问就做**。

🔴 这比「漏改一处文案」严重：方向是反的。少说一句是信息不足，说反了是让他按错误的前提
放心（他以为有一道确认拦着，于是不设防地让它去改东西）。
2026-11-05 已经栽过一次同形状的跟头：只读档的文案承诺「不能改文件」，而实测点同意就能改
（见 `tests/test_copy_quality.py` 和 `policy.describe` 的红字）。

这一轮改成**按真实档位说**，而且档位从写进配置的那个值取（不是另写一个常量 ——
两处各写一份就会漂）。
"""
from __future__ import annotations

import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN = os.path.join(_ONECLICK, "src", "install-windows.ps1")
README = os.path.join(_ONECLICK, "README.md")



def _mode_read_block(text: str) -> str:
    """安装器里「算这台装完是哪一档」那一段（第 19 轮起是：先问 python，算不出再 sed）。

    这里的 VENV 指到不存在的地方 ⇒ 走的是 sed 兜底那条路；python 那条路由
    test_r19_migration_e2e.py 的尾屏用例真跑着兜。
    """
    start = text.index("INSTALLED_MODE=")
    start = text.rindex("\n", 0, start) + 1
    end = text.index("\nesac\n", start) + len("\nesac\n")
    return text[start:end]

def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _run_mac_final_screen(mode: str) -> str:
    """把 mac 尾屏那段 `case` 抽出来真跑一次，返回它打印的内容。

    🔴 判据从「文件里不许出现某个字符串」改成「跑一遍看它说什么」。
    第一版我写成 `assert "它现在是【只读】的" not in text` —— 方向错了：
    正确实现里**只读档必须说那句话**，所以修对了也永远红，除非把只读档的正确文案删掉。
    值守 15:34 指出来的。要钉的是「不许**无条件**说」，不是「不许说」。
    """
    import re
    import subprocess
    import tempfile

    text = _read(MAC)
    i = text.index('case "${INSTALLED_MODE:-trusted}" in')
    screen = text[i:text.index("esac", i) + 4]
    read_line = _mode_read_block(text)
    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "config.toml")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write(f'[agent]\nmode = "{mode}"\n')
        script = f'set -u\nAPP_HOME="{tmp}"\nVENV="{tmp}/no-venv"\nCONFIG="{config}"\n{read_line}\n{screen}\n'
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                              text=True, timeout=60).stdout


def test_mac_final_screen_says_the_truth_in_every_mode():
    """三档各跑一次：说的必须和配置里的档位一致。"""
    trusted = _run_mac_final_screen("trusted")
    assert "全授权" in trusted and "只读】" not in trusted, trusted
    assert "不问就做" in trusted, trusted

    readonly = _run_mac_final_screen("readonly")
    assert "只读" in readonly and "全授权】" not in readonly, readonly
    assert "弹一张卡片问你" in readonly, readonly

    write = _run_mac_final_screen("write")
    assert "改文件" in write and "全授权】" not in write, write


def test_mac_final_screen_defaults_to_trusted_when_mode_is_unreadable():
    """配置读不出来（权限、残缺）⇒ 按安装器实际写的那个档说，不许瞎猜成只读。"""
    out = _run_mac_final_screen("")
    assert "全授权" in out, out


def test_readonly_sentence_only_lives_inside_the_case():
    """那句「现在是只读」只许出现在按档位分支的 case 里，不许在外面无条件打印。"""
    text = _read(MAC)
    i = text.index('case "${INSTALLED_MODE:-trusted}" in')
    j = text.index("esac", i)
    for match in re.finditer(r"它现在是【只读】的", text):
        assert i < match.start() < j, "「现在是只读」出现在按档位分支之外 ⇒ 会无条件打印"


@pytest.mark.parametrize("path", [MAC, WIN], ids=["mac", "windows"])
def test_installer_copy_matches_the_mode_it_writes(path):
    """那一屏的措辞要跟着写进配置的档位走，而且说全授权时要说清挡什么。

    🔴 两个平台分开参数化（原来写在一个 for 里）：Windows 侧归角色 B，
    混在一条用例里的话，B 还没改完就看不出 mac 已经好了 —— 一条红盖住两件事。
    """
    text = _read(path)
    assert re.search(r'mode = "trusted"', text), f"{os.path.basename(path)} 默认档变了？"
    assert "不问就做" in text or "不用每次问你" in text, os.path.basename(path)
    for guard in ("删", "推代码", "密钥"):
        assert guard in text, f"{os.path.basename(path)} 没说清全授权挡什么：缺「{guard}」"


def test_installer_tells_how_to_tighten():
    """领导原话「如果用户要求安全的话，让他单独设定只读权限」⇒ 要说清怎么收紧。"""
    for path in (MAC, WIN):
        text = _read(path)
        assert "只读" in text and ("权限" in text or "收" in text), os.path.basename(path)


def test_readme_does_not_promise_readonly_default():
    text = _read(README)
    assert "**默认只读。**" not in text, "README 还在说默认只读"
    assert "不能改文件" not in text, "README 还在承诺「不能改文件」（那句话实测是假的）"


# ---------- 🔴 读得太早：老用户重装时尾屏仍会说错档 ----------


def test_mode_is_read_after_carryover():
    """值守 15:18 报的，我复核了：`INSTALLED_MODE` 读在 `carryover.py` **之前**。

    carryover 会把老 config 的 `mode` 写回新 config（`bridge/carryover.py:44` 的带回清单里
    就有 `mode`/`max_mode`），所以老用户重装时那一行读到的是**写回之前**的值 ⇒
    尾屏说「全授权」而配置里是 `readonly`。

    两种实测会保留 readonly 的路径（都是 `_migrating_from_old_default` 刻意**不**迁移的）：
      - 老用户 `max_mode = "readonly"`（自己锁死只读）
      - 第 16 轮之后自己挑的只读（有 `defaults_rev`）
    这和我刚修掉的那个 bug 是同一个方向（说的和实际不一样），只是反过来：
    这次是说得比实际更宽，用户以为已全开。

    判据：读那一行必须排在 carryover 那个 if 块**之后**。
    """
    text = _read(MAC)
    i_read = text.index("INSTALLED_MODE=")
    # 🔴 要找**调用** carryover 那一处，不是文件名第一次出现的地方 ——
    # `carryover.py` 在安装器前面还出现过一次（`write_file "$APP_HOME/bridge/carryover.py"`，
    # 把模块写到磁盘上）。第一版我写成 `text.index('carryover.py')`，
    # 匹配到的是那个嵌入段 ⇒ 顺序断言恒真、测试为了错误的理由通过（今天第三次踩这个形状）。
    i_carry = text.index('"$APP_HOME/bridge/carryover.py" "$OLD_CONFIG"')
    assert i_read > i_carry, (
        "INSTALLED_MODE 读在 carryover 之前 ⇒ 老用户重装时尾屏说错档"
    )


def test_final_screen_says_readonly_for_a_locked_old_user():
    """真跑一遍：老用户锁死只读 + 重装 ⇒ 尾屏必须说「只读」，不许说「全授权」。

    把安装器里「写配置 → carryover → 读档位 → 尾屏那段」抽出来在 `/tmp` 下真跑，
    不碰生产配置、不跑完整安装器。
    """
    import re
    import subprocess
    import tempfile

    text = _read(MAC)
    # 尾屏那一段（按档位分支的 case）
    i = text.index('case "${INSTALLED_MODE:-trusted}" in')
    screen = text[i:text.index("esac", i) + 4]
    read_line = _mode_read_block(text)

    with tempfile.TemporaryDirectory() as tmp:
        config = os.path.join(tmp, "config.toml")
        # carryover 跑完之后的样子：老用户锁死只读 ⇒ mode 被写回 readonly
        with open(config, "w", encoding="utf-8") as handle:
            handle.write('[agent]\nmode = "readonly"\nmax_mode = "readonly"\n')
        script = f'set -u\nAPP_HOME="{tmp}"\nVENV="{tmp}/no-venv"\nCONFIG="{config}"\n{read_line}\n{screen}\n'
        out = subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                             text=True, timeout=60).stdout
    assert "只读" in out, out
    assert "全授权" not in out, f"🔴 配置里是只读，尾屏却说全授权：\n{out}"
