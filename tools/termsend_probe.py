"""termsend 只读探针：对本机真实会话核对「终端识别 + 两源一致闸」，一个字都不送。

只调读路径（`detect_terminal` / `_own_pids` / `can_send(for_key=True)` / `_target_of`），
这些只跑 `ps eww` 和 `cmux sessions list`（都是只读）。**绝不**调 `send_text` / `send_key`
（那两个才会 `cmux paste` / `send-key` 真打字），所以跑它不会动任何会话。

用法：python tools/termsend_probe.py
"""

from __future__ import annotations

import sys

from bridge.fleet.claude import ClaudeAdapter
from bridge.fleet.termsend import TermSend, TERM_UNKNOWN


def main() -> int:
    adapter = ClaudeAdapter()
    ts = TermSend(adapter)
    table = adapter._table()
    if getattr(table, "error", ""):
        print("读不到进程表：", table.error)
        return 1

    # 注册表里 source == user 的会话（主人自己开的那些）
    sids = []
    seen = set()
    for source, rec in adapter._registry():
        sid = rec.get("sessionId")
        if source == "user" and isinstance(sid, str) and sid not in seen:
            seen.add(sid)
            sids.append(sid)

    print(f"本机 user 会话：{len(sids)} 个\n")
    cols = f"{'会话':10} {'终端':8} {'有活进程':8} {'能按Esc':8} {'两源闸':8} 说明"
    print(cols)
    print("-" * len(cols))

    n_cmux = n_sendable = n_gate_ok = 0
    for sid in sids:
        short = sid[:8]
        pids = ts._own_pids(sid, table)
        kind = ts.detect_terminal(sid)
        can_key, why_key = ts.can_send(sid, for_key=True)
        target = ts._target_of(sid)  # None = 两源对不上 / 取不到窗口
        gate = "过" if target is not None else "不过"
        note = ""
        if kind == TERM_UNKNOWN:
            note = "认不出终端（退回 relay，不送字）"
        elif target is None:
            note = "两源对不上或取不到窗口 ⇒ 一个字不送（安全）"
        elif not can_key:
            note = (why_key or "").strip()[:30] or "这会儿按不了"
        else:
            note = "可真执行斜杠命令 / Esc"
        if kind == "cmux":
            n_cmux += 1
        if can_key:
            n_sendable += 1
        if target is not None:
            n_gate_ok += 1
        print(f"{short:10} {kind:8} {str(bool(pids)):8} {str(bool(can_key)):8} {gate:8} {note}")

    print(f"\n小结：{len(sids)} 个里 cmux {n_cmux} 个，能按 Esc {n_sendable} 个，两源闸过 {n_gate_ok} 个。")
    print("注：本探针只读，没有向任何会话送字 / 按键。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
