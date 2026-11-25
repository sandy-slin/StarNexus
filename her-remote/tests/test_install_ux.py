"""装机文案的回归测试：盯「非技术用户会不会卡在这一步」。

这些都不是风格洁癖，每一条对应一种**用户会放弃安装**的具体形状：

1. **静默等待** —— 装依赖那步要等一分钟，期间一个字都不打。用户的判断是「卡死了」
   ⇒ 关窗口 ⇒ 盘上留一个半装的 venv，然后来报「装不上」。所以长步骤必须先说要等多久。

2. **`die` 不给下一步** —— `✗ 创建虚拟环境失败` 对工程师够了，对付费用户等于死路：
   他不知道该装什么、改什么、找谁。每个硬失败都必须接一句「下一步」。

3. **最后一屏混进 launchctl / Scheduled-Task 命令** —— 噪音本身无害，但它会把
   「还差一步：去飞书发这个码」挤出视线，于是人装完了不知道还差一步。
   技术细节挪进「技术细节.txt」，最后一屏只留用户真要做的动作。

4. **扫码那步不说失败怎么办** —— 这一步要在飞书里点确认，而「管理员禁止自建应用」
   是公司环境里的常态。不写出口，用户只会看到一个没反应的窗口。

只测文本特征，不测交互 —— 交互要真人。
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick")
)

MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")
MAC_PRODUCT = os.path.join(_ONECLICK, "feishu-agent-mac.command")
WIN_PRODUCT = os.path.join(_ONECLICK, "feishu-agent-windows.bat")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def code_lines(path: str) -> list[tuple[int, str]]:
    """只看会执行的行 —— 判据不该被注释里的说明文字喂饱。"""
    return [
        (no, line)
        for no, line in enumerate(read(path).splitlines(), 1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ---------- 1. 进度感：长步骤必须先说要等多久 ----------

# 「约 1 分钟」「约 10 秒」「最多 8 秒」这类话。
_DURATION = re.compile(r"(约|最多)\s*\d+\s*(秒|分钟)")


def test_mac_declares_how_long_the_download_takes():
    """装依赖是最长的一段静默等待 ⇒ 开始之前必须给出预期时长。"""
    text = read(MAC_SRC)
    pip_at = text.index('"$VENV/bin/pip" install --no-index --find-links "$PIP_WHEELS" "lark-oapi')
    # 往前找最近的几行提示，里面得有「多久」
    before = text[max(0, pip_at - 600) : pip_at]
    assert _DURATION.search(before), (
        "mac 安装器在开始下载依赖前没说要等多久 —— 用户会以为卡死了：\n" + before[-300:]
    )


def test_windows_declares_how_long_the_download_takes():
    text = read(WIN_SRC)
    pip_at = text.index("Invoke-WithDots $venvPy @('-m','pip','install','-i'")
    before = text[max(0, pip_at - 600) : pip_at]
    assert _DURATION.search(before), (
        "Windows 安装器在开始下载依赖前没说要等多久：\n" + before[-300:]
    )


def test_both_installers_have_a_heartbeat_during_the_long_wait():
    """光说「约 1 分钟」不够：还得有个在动的东西，证明它没死。"""
    mac = read(MAC_SRC)
    assert "heartbeat_start" in mac and "heartbeat_stop" in mac, (
        "mac 安装器没有等待心跳 —— 一分钟零输出和卡死在用户眼里一模一样"
    )
    # 心跳是后台进程 ⇒ 必须挂 trap，否则脚本中途 die 时点点循环留在后台
    assert re.search(r"^trap \w+ EXIT", mac, re.M), (
        "心跳进程没挂 trap EXIT，die 之后会留个野进程在后台打点"
    )

    win = read(WIN_SRC)
    assert "Invoke-WithDots" in win, "Windows 安装器没有等待心跳"


def test_every_step_header_carries_an_expectation():
    """[N/6] 那几行要么是一瞬间的事，要么得带时长/「要你动手」。

    用户对「这一步会不会很久」的唯一信息来源就是这一行。
    """
    for path, pattern in ((MAC_SRC, r'say "\[(\d)/6\][^"]*"'), (WIN_SRC, r'Say "\[(\d)/6\][^"]*"')):
        bare = []
        for match in re.finditer(pattern, read(path)):
            header = match.group(0)
            if not (_DURATION.search(header) or "要你动手" in header or "几秒" in header):
                bare.append(header)
        assert not bare, (
            f"{os.path.basename(path)} 这些步骤标题没给用户任何时间预期：\n"
            + "\n".join(bare)
        )


# ---------- 2. 每个 die 都要给下一步 ----------

_NEXT_STEP_HINTS = ("下一步", "重新双击", "发给 IT", "改用")


def _mac_die_messages() -> list[tuple[int, str]]:
    """抓出 mac 侧每个 die 的完整消息（die 的实参可能跨多行）。"""
    text = read(MAC_SRC)
    out = []
    for match in re.finditer(r'(?m)^\s*(?:\|\| )?die "', text):
        start = match.end()
        lineno = text[: match.start()].count("\n") + 1
        # 从开引号往后找配对的收尾引号（消息里不含裸双引号）
        end = text.index('"', start)
        out.append((lineno, text[start:end]))
    return out


def test_every_mac_die_tells_the_user_what_to_do_next():
    """🔴 `die "✗ 创建虚拟环境失败"` 这种话等于把用户扔在原地。"""
    offenders = []
    for lineno, message in _mac_die_messages():
        if not any(hint in message for hint in _NEXT_STEP_HINTS):
            offenders.append(f"install-mac.sh:{lineno}  {message.splitlines()[0]}")
    assert not offenders, (
        "这些失败只说了「错了」，没说「那我该干什么」：\n" + "\n".join(offenders)
    )


def _win_die_messages() -> list[tuple[int, str]]:
    """抓出 Windows 侧每个 Die 的消息（here-string 或单行字符串）。"""
    lines = read(WIN_SRC).splitlines()
    out = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("Die "):
            continue
        if stripped.startswith('Die @"'):
            body = []
            for follow in lines[idx + 1 :]:
                if follow.rstrip() == '"@':
                    break
                body.append(follow)
            out.append((idx + 1, "\n".join(body)))
        else:
            out.append((idx + 1, stripped))
    return out


def test_every_windows_die_tells_the_user_what_to_do_next():
    offenders = []
    for lineno, message in _win_die_messages():
        if not any(hint in message for hint in _NEXT_STEP_HINTS):
            first = message.splitlines()[0] if message.splitlines() else message
            offenders.append(f"install-windows.ps1:{lineno}  {first}")
    assert not offenders, (
        "这些失败只说了「错了」，没说「那我该干什么」：\n" + "\n".join(offenders)
    )


def test_die_next_steps_are_not_just_raw_log_paths():
    """「看日志：tail -50 ...」不是下一步 —— 用户看了日志也不知道该干什么。

    阳性对照方向：给了日志路径的同时，必须还给一个**动作**
    （跑自检 / 发给 IT / 重装）。
    """
    offenders = []
    for lineno, message in _mac_die_messages():
        if "agent.log" not in message:
            continue
        if not ("doctor" in message or "发给 IT" in message):
            offenders.append(f"install-mac.sh:{lineno}")
    assert not offenders, (
        "只甩了一个日志路径当下一步（用户看不懂日志）：\n" + "\n".join(offenders)
    )


# ---------- 3. 最后一屏不放技术命令 ----------

# 用户看不懂、也用不上的那些。launchd / 计划任务 / venv 内部路径。
_TECH_NOISE = (
    "launchctl kickstart",
    "launchctl bootout",
    "Stop-ScheduledTask",
    "Unregister-ScheduledTask",
    "bridge.pairing reissue",
    "tail -f",
    "Get-Content",
)


def _mac_final_screen() -> str:
    """装完那一屏 = 最后那条分隔线之后、真会打到屏幕上的内容。

    要排掉两类东西，它们都不在用户眼前：
      - 注释（解释「为什么不打这些命令」时难免要把命令名写出来）
      - 写进「技术细节.txt」的那段 heredoc
    """
    text = read(MAC_SRC)
    text = re.sub(r"cat > \"\$NOTES\" <<NOTESEOF\n.*?\nNOTESEOF\n", "", text, flags=re.S)
    anchor = text.rindex('say "======')
    return "\n".join(
        line
        for line in text[anchor:].splitlines()
        if not line.lstrip().startswith("#")
    )


def _win_final_screen() -> str:
    text = read(WIN_SRC)
    text = re.sub(r'\$notesText = @"\n.*?\n"@\n', "", text, flags=re.S)
    # 拼 reissue 命令的那一行是为了写进文件，不打在屏幕上
    text = re.sub(r"(?m)^\$reissueCmd = .*\n", "", text)
    anchor = text.rindex('Say "======')
    return "\n".join(
        line
        for line in text[anchor:].splitlines()
        if not line.lstrip().startswith("#")
    )


def test_mac_final_screen_has_no_raw_launchctl():
    """🔴 非技术用户用不上 launchctl，而它会把「还差一步」挤出视线。"""
    screen = _mac_final_screen()
    found = [noise for noise in _TECH_NOISE if noise in screen]
    assert not found, (
        "装完那一屏还在打这些命令，用户看不懂也用不上：\n" + "\n".join(found)
    )


def test_windows_final_screen_has_no_raw_task_commands():
    screen = _win_final_screen()
    found = [noise for noise in _TECH_NOISE if noise in screen]
    assert not found, (
        "装完那一屏还在打这些命令，用户看不懂也用不上：\n" + "\n".join(found)
    )


def test_tech_details_are_written_to_a_file_not_dropped():
    """阳性对照：上面两条不能靠「把技术细节删了」通过 —— 它得搬进文件。

    重启/看日志/重发配对码这些事偶尔真要做，只是不该挡在最后一屏。
    """
    mac = read(MAC_SRC)
    assert "技术细节.txt" in mac, "mac 侧没有把技术细节落到文件"
    for needed in ("launchctl kickstart", "bridge.pairing reissue", "agent.log"):
        assert needed in mac, f"技术细节文件里缺 {needed}（删掉了而不是搬走了）"

    win = read(WIN_SRC)
    assert "技术细节.txt" in win, "Windows 侧没有把技术细节落到文件"
    for needed in ("Stop-ScheduledTask", "bridge.pairing reissue", "agent.log"):
        assert needed in win, f"技术细节文件里缺 {needed}（删掉了而不是搬走了）"


def test_final_screen_points_at_the_notes_file():
    """搬走了还得告诉用户搬哪儿去了，否则等于藏起来。"""
    assert "${NOTES}" in _mac_final_screen(), "mac 最后一屏没说技术细节在哪个文件"
    assert "$notes" in _win_final_screen(), "Windows 最后一屏没说技术细节在哪个文件"


def test_final_screen_does_not_say_ready_when_not_connected():
    """🔴 没连上时不能说「现在就能用了」。

    实测（改完第一版后渲染失败分支）：表头刚打「先别发消息，没人会回」，
    下面紧跟一句「现在就能用了：直接跟它说话」 —— 两段同屏。
    用户会按后一句做，发出去没人回，然后判定「这东西是坏的」。
    修法：那句「现在就能用了」必须挂在连通性判定的成功支上。
    """
    mac = _mac_final_screen()
    assert '现在就能用了' in mac, "mac 最后一屏没有「可以用了」的出口"
    assert 'elif [ "$CONNECTED" = true ]' in mac, (
        "mac 的「现在就能用了」没有挂在 CONNECTED 成功支上 ⇒ 没连上也会这么说"
    )

    win = _win_final_screen()
    assert "现在就能用了" in win, "Windows 最后一屏没有「可以用了」的出口"
    assert "} elseif ($connected) {" in win, (
        "Windows 的「现在就能用了」没有挂在 $connected 成功支上"
    )


def test_pairing_code_is_deferred_when_not_connected():
    """没连上时发配对码是收不到的 —— 不说清楚，用户会把 5 次机会烧光。"""
    mac = _mac_final_screen()
    assert "先留着" in mac, (
        "mac 在没连上时仍然催用户立刻发码 ⇒ 码收不到，试错次数白烧"
    )
    win = _win_final_screen()
    assert "先留着" in win, "Windows 在没连上时仍然催用户立刻发码"


def test_final_screen_tells_the_user_where_to_find_the_bot():
    """「去飞书找你的机器人」要说清怎么找 —— 不然他在飞书里翻半天。"""
    for screen, name in (
        (_mac_final_screen(), "install-mac.sh"),
        (_win_final_screen(), "install-windows.ps1"),
    ):
        assert "搜索框" in screen, f"{name} 没说在飞书里怎么找到这个机器人"


# ---------- 4. 扫码那步：说清点完会怎样、失败怎么办 ----------


def _scan_block(path: str, start_marker: str, end_marker: str) -> str:
    text = read(path)
    return text[text.index(start_marker) : text.index(end_marker)]


MAC_SCAN = ("qrcode 2>/dev/null", "# ── 5.")
WIN_SCAN = ("$regOut = & $venvPy", "# ── 5.")


def test_scan_step_says_what_to_click_and_what_success_looks_like():
    """这是最关键的一步：用户要在飞书里点「确认」，点完得知道成了没。"""
    for path, (start, end), name in (
        (MAC_SRC, ("say \"  ┌─ 这一步要你在飞书里点一下", "REG_OUT="), "install-mac.sh"),
        (WIN_SRC, ('Say "  +- 这一步要你在飞书里点一下', "$regOut ="), "install-windows.ps1"),
    ):
        block = _scan_block(path, start, end)
        assert "确认" in block, f"{name} 没说要在飞书里点【确认】"
        assert "应用已创建" in block, f"{name} 没告诉用户「成功长什么样」"
        assert "回到这个窗口" in block, (
            f"{name} 没让用户点完回到窗口 —— 很多人点完就去干别的了"
        )


def test_scan_step_says_what_to_do_when_it_fails():
    """🔴 「管理员禁止自建应用」在公司环境里是常态，不给出口就等于卡死。"""
    for path, start, name in (
        (MAC_SRC, "warn \"  ✗ 扫码这条路没走通", "install-mac.sh"),
        (WIN_SRC, 'Warn "  X 扫码这条路没走通', "install-windows.ps1"),
    ):
        text = read(path)
        assert start in text, f"{name} 没有扫码失败的分支"
        block = text[text.index(start) : text.index(start) + 1400]
        assert "管理员" in block, f"{name} 扫码失败时没提「可能要管理员放开权限」"
        assert "过期" in block, f"{name} 扫码失败时没提「码过期了重跑就行」"
        assert "手工" in block, f"{name} 扫码失败后没给第二条路"


# ---------- 5. 工作目录：不问（2026-11-06 用户原话「用户都是小白…争取一键搞定」）----------
#
# 原来这里扫 8 秒找 git 项目、列编号让人选（那三条测试测的是扫描本身）。
# 小白不知道「目录」「项目」是什么，这一问只会让他犹豫 ⇒ 整段改成**不问**：
# 重装沿用上次的、IT 可用 CARHER_WORKSPACE 指定、否则用默认。装完想换在飞书点「换项目」。


def test_workspace_step_asks_nothing():
    """🔴 一键装：「能看哪个目录」这一步一个输入都不许有。"""
    mac = read(MAC_SRC)
    block = mac[mac.index('if [ -n "$KEEP_WS" ]; then'):mac.index("fi  # KEEP_WS")]
    assert "read " not in block, "mac 又在问目录了"
    win = read(WIN_SRC)
    wblock = win[win.index("if ($keepWs) {"):]
    wblock = wblock[:wblock.index("# 纯数字")]
    assert "Read-Host" not in wblock, "Windows 又在问目录了"


def test_workspace_default_is_announced_with_how_to_change():
    """不问，但要说一句它能看哪个文件夹、以后怎么换 —— 按钮，不是命令。"""
    for src in (read(MAC_SRC), read(WIN_SRC)):
        assert "它能看的文件夹" in src and "换项目" in src
        assert "/cd" not in src[src.index("它能看的文件夹") - 200:src.index("它能看的文件夹") + 200]


def test_admin_can_preset_the_workspace():
    """IT 推广时可以统一指定（同 APP_ID 预烤凭据的思路），两个平台同一个变量名。"""
    assert "CARHER_WORKSPACE" in read(MAC_SRC) and "CARHER_WORKSPACE" in read(WIN_SRC)


def test_whole_mac_installer_has_at_most_the_fallback_prompts():
    """🔴 整个 Mac 安装器里，要用户输入的只剩「扫码失败后手动粘凭据」那条兜底路上的。"""
    mac = read(MAC_SRC)
    import re as _re

    prompts = [m.start() for m in _re.finditer(r"read -r [A-Z_]+ </dev/tty", mac)]
    manual = mac.index('if [ "$CREATE_MODE" = 2 ]; then')
    outside = [i for i in prompts if not (manual < i < manual + 6000)]
    assert not outside, f"扫码成功的主路上还有 {len(outside)} 处要用户输入"


def test_workspace_rejects_out_of_range_number_with_a_hint():
    """输了个不存在的编号，要说清有效范围，不能只说「不存在」。"""
    mac = read(MAC_SRC)
    assert "没有第 ${WORKSPACE} 项" in mac, "mac 侧没处理越界编号"
    win = read(WIN_SRC)
    assert "没有第 $workspace 项" in win, "Windows 侧没处理越界编号"


# ---------- 6. 重装：说清不会丢东西 ----------


def test_reinstall_screen_says_nothing_is_lost():
    """已装过的人重跑，第一反应是「会不会把我绑的关系冲掉」。

    行为上确实不会（沿用已绑定的主人），但不写出来用户不知道 ⇒
    他会中途关窗口，或者去手动删目录，那才真丢东西。
    """
    for path, name in ((MAC_SRC, "install-mac.sh"), (WIN_SRC, "install-windows.ps1")):
        text = read(path)
        assert "以前装过" in text, f"{name} 重装时没说清这是「升级」"
        assert "会保留" in text, f"{name} 重装时没说明什么会被保留"
        assert "不用重新扫码" in text, f"{name} 没打消「要不要再扫一次码」的疑问"


def test_reinstall_keeps_the_bound_owner():
    """🔴 安全行为回归：重装绝不能把已绑定的主人擦成「待配对」。

    2026-11-03 真出过事：重跑一次安装器，用户 open_id 被擦掉，
    他发的每条消息都走静默丢弃，飞书里毫无反应且不知道为什么。
    文案可以改，这条逻辑不许动。
    """
    mac = read(MAC_SRC)
    assert "EXISTING_OWNER" in mac and "owner_open_ids" in mac
    assert 'OWNER_OPEN_ID="${EXISTING_OWNER}"' in mac, (
        "mac 侧不再沿用已绑定的主人 —— 重装会让用户失去控制权"
    )
    win = read(WIN_SRC)
    assert "$ownerOpenId = $ownerMatch.Matches[0].Groups[1].Value" in win, (
        "Windows 侧不再沿用已绑定的主人"
    )


# ---------- 7. 安全行为只改文案、不改逻辑 ----------


def test_security_defaults_untouched():
    """配对码机制 / readonly 默认 / 600 权限 / 不回显 —— 这些只许改措辞。"""
    mac = read(MAC_SRC)
    assert 'mode = "readonly"' in mac, "mac 默认权限不再是 readonly"
    assert 'chmod 600 "$CONFIG"' in mac, "mac 不再把配置收成 600"
    assert "read -rs APP_SECRET" in mac, "mac 不再隐藏 App Secret 的输入"
    assert "pairing_max_attempts = 5" in mac, "mac 配对码不再限制试错次数"
    assert "pairing_ttl_min = 30" in mac, "mac 配对码不再过期"

    win = read(WIN_SRC)
    assert 'mode = "readonly"' in win, "Windows 默认权限不再是 readonly"
    assert "-AsSecureString" in win, "Windows 不再隐藏 App Secret 的输入"
    assert "SetAccessRuleProtection" in win, "Windows 不再断开配置文件的权限继承"
    assert "pairing_max_attempts = 5" in win, "Windows 配对码不再限制试错次数"
    assert "pairing_ttl_min = 30" in win, "Windows 配对码不再过期"


def test_notes_file_is_not_world_readable():
    """技术细节文件里有配置路径，顺手也收紧权限（里面不放 secret，但别给别人看）。"""
    mac = read(MAC_SRC)
    assert 'chmod 600 "$NOTES"' in mac, "技术细节文件没收紧权限"


def test_pairing_code_warning_survived_the_rewrite():
    """配对码那段警告是安全提示，重写文案时不能顺手删掉。"""
    for path, name in ((MAC_SRC, "install-mac.sh"), (WIN_SRC, "install-windows.ps1")):
        text = read(path)
        assert "就归谁" in text, f"{name} 丢了「谁先发码这台电脑就归谁」的警告"
        assert "别发到群里" in text or "别把它发到群里" in text, (
            f"{name} 丢了「别把码发到群里」的警告"
        )


# ---------- 8. 产物要跟着源一起更新 ----------


def test_rebuilt_products_carry_the_new_copy():
    """产物比源新（= build.sh 跑过了）时，新文案必须真进了产物。

    🔴 判据刻意挂在 mtime 上，**不能**写成「产物必须永远含这些字」：
    build.sh 是先跑全量测试再生成产物，那种写法会把第一次构建卡成死锁 ——
    测试失败 ⇒ 构建中止 ⇒ 产物永远不会被生成（build.sh 里为 chmod 踩过同一个坑）。
    这里能抓的是另一种真实故障：build.sh 跑了，但 @@EMBED 替换或模板裁剪出了岔子，
    产物里少了半截文案。
    """
    for src, product in ((MAC_SRC, MAC_PRODUCT), (WIN_SRC, WIN_PRODUCT)):
        if not os.path.exists(product):
            continue
        if os.path.getmtime(product) < os.path.getmtime(src):
            continue  # 还没重新构建，不在这条测试的管辖范围
        with open(product, encoding="utf-8", errors="replace") as handle:
            built = handle.read()
        # 「输编号」2026-11-06 随选目录那一问一起删了 ⇒ 换成取代它的那句「它能看的文件夹」
        for marker in ("技术细节.txt", "它能看的文件夹", "以前装过"):
            assert marker in built, (
                f"{os.path.basename(product)} 比源新，却没有「{marker}」 ⇒ "
                f"build.sh 的替换/裁剪把文案吃掉了一截"
            )


def test_windows_product_stays_crlf_and_ascii_head():
    """bat 头的字节纪律：cmd 按字节偏移续读，头里有非 ASCII 就吃掉行首字符。"""
    if not os.path.exists(WIN_PRODUCT):
        return
    with open(WIN_PRODUCT, "rb") as handle:
        blob = handle.read()
    cut = blob.index(b"##CARHER-PS1")
    assert not blob.startswith(b"\xef\xbb\xbf"), "产物带了 BOM"
    assert all(byte < 128 for byte in blob[:cut]), "bat 头混进了非 ASCII"
    assert blob.count(b"\n") == blob.count(b"\r\n"), "换行不是全 CRLF"
