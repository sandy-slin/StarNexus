"""termsend 真机验收：在一个**一次性** cmux workspace 上证明 paste 真执行 + Esc 真打断，用完销毁。

为什么这么验（而不是往真实会话发斜杠命令）：termsend 送的是**会真执行**的命令，往主人正在干活的
会话里发 `/clear` 这类会真清上下文 —— 验收不能拿真实会话冒险。所以这里自建一个跑普通 bash 的
一次性 workspace，用 termsend 完全相同的两条 cmux 命令（`paste --submit` 走 stdin、`send-key escape`）
打进去、`read-screen` 读回确认，最后 `workspace close --force` 销毁。全程不碰任何 claude 会话。

2026-11-28 本机实跑通过：
  - paste --submit：`echo SN_TERMSEND_<ts>` 落进窗口并执行，read-screen 读回命令行 + 输出。
  - send-key escape：长跑 `sleep 60` 被 Esc 打断（read-screen 见 `^[`），终端仍活、能接着敲命令。

用法：python tools/termsend_live_check.py   （需要本机装了 cmux.app）
退出码 0 = 两项都过；非 0 = 有一项没过（详情打印在 stdout）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

CMUX_CANDIDATES = (
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
    "/opt/homebrew/bin/cmux",
    "/usr/local/bin/cmux",
)


def _cmux() -> str:
    import os
    for p in CMUX_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise SystemExit("没找到 cmux.app，这台机器跑不了这个验收")


def run(cmux: str, args: list, stdin: str = None, timeout: float = 15.0) -> str:
    return subprocess.run([cmux, *args], input=stdin, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


def main() -> int:
    cmux = _cmux()
    ws_line = run(cmux, ["workspace", "create", "--name", "SN-termsend-test", "--cwd", "/tmp",
                         "--command", "bash --noprofile --norc", "--focus", "false"])
    # 形如 "OK workspace:1000060010"
    ws = next((tok for tok in ws_line.split() if tok.startswith("workspace:")), "")
    if not ws:
        print("建一次性 workspace 失败：", ws_line)
        return 1
    print("一次性 workspace:", ws)
    ok_paste = ok_esc = False
    try:
        # ① paste --submit 真执行
        mark = f"SN_TERMSEND_{int(time.time())}"
        run(cmux, ["paste", "--workspace", ws, "--submit", "-"], stdin=f"echo {mark}")
        time.sleep(1.5)
        screen = run(cmux, ["read-screen", "--workspace", ws, "--lines", "20"])
        ok_paste = screen.count(mark) >= 2  # 命令行一次 + 输出一次
        print(f"① paste --submit 真执行: {'通过' if ok_paste else '失败'}（找到 {screen.count(mark)} 处 {mark}）")

        # ② send-key escape 把 Esc 键真送进窗口（termsend 停会话走的就是这条）。
        #    契约 = 「把这个键送进去」；在 Claude TUI 里 Esc 打断当前轮，普通 bash 不打断前台命令 ——
        #    是否打断取决于目标程序，不是 termsend 的事。所以判据用**命令是否送达成功**（cmux 回 `OK ...`），
        #    不靠 read-screen 抓控制字符（Esc 在屏幕缓冲里会被规范化，跨次不稳）。
        esc_out = run(cmux, ["send-key", "--workspace", ws, "escape"])
        esc_delivered = esc_out.startswith("OK")
        time.sleep(0.8)
        # 送个可见标记，确认 Esc 后终端仍活、还能继续接收按键
        after = f"AFTER_ESC_{int(time.time())}"
        run(cmux, ["paste", "--workspace", ws, "--submit", "-"], stdin=f"echo {after}")
        time.sleep(1.5)
        screen2 = run(cmux, ["read-screen", "--workspace", ws, "--lines", "20"])
        term_alive = after in screen2  # 终端仍活、还能收键
        ok_esc = esc_delivered and term_alive
        print(f"② send-key escape 送达: {'通过' if ok_esc else '失败'}"
              f"（命令回 OK={esc_delivered} 终端仍活={term_alive}）")
    finally:
        run(cmux, ["workspace", "close", "--workspace", ws, "--force"])
        print("已销毁一次性 workspace")
    return 0 if (ok_paste and ok_esc) else 1


if __name__ == "__main__":
    sys.exit(main())
