"""起 `claude -p` 子进程跑一轮，把事件流收干净。

这个文件里有两处是**专门为了绕开已知 bug** 写的，改之前请先读注释：

1. **stdout 的读取必须在 spawn 的同一时刻就接上，不能 await 之后再接。**
   lark-channel-bridge issue #21：他们把 readline 放在一个懒生成器里，前面先 await 了
   两件事（加表情 ~2s、建卡片 ~0.5s）。而短轮次的 `claude -p` 1.5~2 秒就退了。
   对一个**已经 EOF** 的流再挂 readline，Node 既不补发缓冲的 line 也不发 close ⇒
   `for await` 永不返回 ⇒ 那个会话的队列永久堵死，重启进程才能恢复。
   这里用 `asyncio.create_subprocess_exec` 后**立刻** `gather` 读取协程，中间不插任何
   await。Python 的 StreamReader 语义和 Node 不同（EOF 后仍能读出缓冲），但这个顺序
   本身是对的，不依赖语言细节兜底。

2. **零输出不等于成功。** issue #250：上一轮 `--resume` 在子 agent 还在飞的时候退了，
   下一轮 resume 发现轮次不完整，写了个 `[Request interrupted by user]` 就在 2.7 秒内
   退出，**根本没调模型**。事件流是空的，桥接那边的 skip-empty 分支就什么都没发。
   人发了消息却收不到任何回音 = 故障，这里显式判。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import os
import time
from dataclasses import dataclass, field

from .policy import Policy


# stream-json 的单行事件上限。16MiB 足够装下任何单条工具结果，
# 又不至于让一个坏掉的子进程吃光内存。
logger = logging.getLogger("feishu-bridge.runner")

_STREAM_LIMIT = 16 * 1024 * 1024

# 附加给 agent 的上下文。写得短 —— 它会进每一轮的 token 预算。
#
# 三件事是实测必须说的：
# 1. **在飞书里、用户多半在手机上** ⇒ 不说的话回复会是几千字的终端风格输出
# 2. **问句就是问句** ⇒ 实测"测试怎么跑"被当成"让测试跑起来"，去装了依赖
# 3. **不要自作主张改东西** ⇒ 用户没要求就别动文件，哪怕看起来"应该顺手修一下"
def _context_for(workspace: str, policy) -> str:
    """拼出这一轮的上下文提示。

    🔴 **必须告诉 agent 它的边界在哪**。
    实测不说的后果：workspace 是 `tools/feishu-agent-bridge` 时，用户问
    "这目录下有几个 py 文件"，agent 去读父目录、被读边界拦住，然后回
    「我没有读取 … 的权限，请授权后我…」—— 用户会以为产品坏了。
    提前说清范围，它就直接在范围内干活。
    """
    allowed = "、".join(policy.workspaces) if policy.workspaces else workspace
    lines = [_BRIDGE_CONTEXT]
    lines.append(
        f"你现在在 {workspace}。你只能读写这些目录里的东西：{allowed}。"
        "这是刻意的限制，不是配置错误 —— 范围外的路径不要去试，"
        # 🔴 2026-11-06：不许教用户打命令。原来这里写「他可以用 /cd 换目录」⇒ 模型照抄给用户，
        # 实测开场例句 15 次回答里 6 次带「用 /cd 换目录」—— 用户原话「不要让用户输入 /cancel 等类似的」。
        # 菜单里有「📂 换项目」按钮，就指那个。
        "直接告诉用户那在你的范围外，换项目点菜单里的「📂 换项目」。"
        "不要让用户输入任何以 / 开头的命令。"
    )
    if policy.mode == "readonly":
        # 🔴 2026-11-06：按**真实行为**说（用户 11-05 拍板）。原来是「你不能改文件、不能提交」——
        # 而只读档下点同意文件真的会被改；模型拿这句当事实原样转告用户，等于替我们撒那个谎。
        lines.append(
            "当前是只读档：默认只看。确实需要改文件时可以去做，系统会先弹卡片问用户，"
            "他点同意才执行 —— 不要告诉用户「改不了」。"
        )
    return " ".join(lines)


_BRIDGE_CONTEXT = (
    "你在通过飞书机器人跟用户对话，他多半在手机上看。"
    "回答要短、直接、能一眼看完；长输出先给结论再给细节。"
    "用户问「怎么做」「是什么」「有哪些」时，他要的是答案，不是让你去执行或修改；"
    "没有明确要求就不要改文件、不要装依赖、不要新建文件。"
    "需要动手时先说清你要做什么。"
)


# 一轮里前后几段文字（中间隔着工具调用）之间的分隔。流式累加和最终文本必须用同一个。
_BLOCK_SEP = "\n\n"


@dataclass
class TurnResult:
    ok: bool
    text: str
    session_id: str | None = None
    cost_usd: float = 0.0
    duration_sec: float = 0.0
    denials: list[dict] = field(default_factory=list)
    error: str | None = None
    timed_out: bool = False
    # 审批钩子把某个工具调用挡下来了，进程主动退出等人点头。
    # 实测 stop_reason == "tool_deferred"；这时 text 通常是空的，**不算失败**。
    deferred: bool = False
    # 用户发了 /stop。和超时区分开：超时是故障，这个是用户的意思。
    cancelled: bool = False


@dataclass
class Progress:
    """进度信号。runner 只负责产出，怎么发给飞书是上层的事。"""

    tool_calls: int = 0
    last_event_at: float = 0.0
    last_tool: str = ""


class ClaudeRunner:
    def __init__(self, policy: Policy, config_dir: str | None = None) -> None:
        self.policy = policy
        # 用独立的 CLAUDE_CONFIG_DIR，免得桥接的会话和你自己手敲的 claude 互相串。
        self.config_dir = config_dir or os.path.expanduser("~/.claude-feishu-bridge")

    def _build_argv(
        self,
        prompt: str,
        resume: str | None,
        workspace: str = "",
        policy: Policy | None = None,
    ) -> list[str]:
        # 每轮用**传进来的那份** policy。用户能在聊天里切档位/换模型
        # （见 prefs.py），而 self.policy 是进程启动时那份。
        # 不接受"中途去读最新值"：一轮里的闸门必须是同一份，
        # 否则"这次操作按哪一档判的"事后说不清。
        policy = policy or self.policy
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            # 🔴 第 18 轮：要逐字增量才能做流式回答（用户催过多次）。
            # 这是 `--output-format stream-json` 的配套参数（`claude --help` 原话
            # 「only works with --print and --output-format=stream-json」，两个都满足）。
            # 不开它只有整块 `assistant` 事件 ⇒ 卡片上只能等一轮跑完一次性出现。
            "--include-partial-messages",
            "--verbose",  # stream-json 需要它才给逐事件输出
            "--model",
            policy.model,
            # manual = 没批准的工具直接拒，不会挂在那儿等人。
            # 刻意不用 bypassPermissions（那正是调研里那个项目的问题所在）。
            # 告诉 agent 它在哪、谁在看 —— 不说的话它会当成在终端里干活。
            # 实测过的后果：用户问"这个项目的测试怎么跑"，agent 去 `npm install`
            # 装了依赖、还新建了一个测试文件（它以为要"让测试能跑起来"）。
            # 那一轮弹了 21 张审批卡片。
            "--append-system-prompt",
            _context_for(workspace or policy.default_workspace, policy),
            "--permission-mode",
            "manual",
        ]
        for tool in policy.allowed_tools():
            argv += ["--allowed-tools", tool]
        for tool in policy.disallowed_tools():
            argv += ["--disallowed-tools", tool]
        if resume:
            argv += ["--resume", resume]
        return argv

    async def run_turn(
        self,
        prompt: str,
        workspace: str,
        resume: str | None = None,
        on_progress=None,
        approval_dir: str | None = None,
        cancel: "asyncio.Event | None" = None,
        policy: Policy | None = None,
        on_text=None,
        on_session=None,
    ) -> TurnResult:
        """跑一轮。

        `on_session(session_id)`（第 32 轮）：读到开头那条 `system/init` 就回调一次，
        让上层**跑到一半**就知道 claude 会话 id（进程被杀时靠它续跑）。
        和 `on_text` 一样必须同步、只记不发 —— 它跑在读子进程的循环里。

        `cancel` 给 `/stop` 用：置位后杀掉子进程并如实回报「被你停下了」。
        没有它的话，一句打错的请求会占住会话到 `turn_timeout_sec`（默认 15 分钟），
        用户唯一的办法是去电脑上杀进程 —— Windows 那边还有重启循环会把它拉回来。

        `policy` 是这一轮生效的那份（用户可能在聊天里切过档位/换过模型）。
        不传就用进程启动时那份。
        """
        started = time.monotonic()
        effective = policy or self.policy
        argv = self._build_argv(prompt, resume, workspace, effective)

        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = self.config_dir
        if approval_dir:
            # 审批钩子是独立进程，靠这个环境变量找到**本会话**的批准目录。
            # 必须按会话分，否则 A 会话的批准会让 B 会话的同样操作放行。
            env["CARHER_APPROVAL_DIR"] = approval_dir

        # 🔴 `limit` 必须调大。asyncio 的 StreamReader 默认上限 64KiB，
        # 而 `--output-format stream-json` 的**单行**事件可以远超这个 ——
        # 读一个大文件、贴一段长 diff 都会让 `readline()` 抛
        # `ValueError: Separator is found, but chunk is longer than limit`，
        # 整轮任务直接失败。2026-11-04 实测复现（逐个读 10 个 py 文件就触发）。
        try:
            proc = await asyncio.create_subprocess_exec(
                limit=_STREAM_LIMIT,
                *argv,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            # 🔴 第 25 轮（P-newbie 复现）：后台服务的 PATH 是装机那一刻写死的；claude 后来换了位置
            # （换安装方式、升级）⇒ 这里抛异常 ⇒ 上层兜成「出错了，点新话题再试」，点了也没用。
            return TurnResult(ok=False, text="",
                              error="后台找不到 Claude Code，重新双击一次安装包就能修好（设置都会保留）。")

        # ↓↓↓ 这里到 gather 之间不要加任何 await —— 见文件头第 1 条。
        progress = Progress(last_event_at=time.monotonic())
        collected: list[str] = []
        # 流式那条路自己累加（`collected` 走完整 `assistant` 事件，一个字不动）。
        # 两者可能有极小差异（delta 丢包）⇒ 终态卡一律用 `result.text` 重写，不信累加值。
        streamed: list[str] = []
        final: dict | None = None
        stderr_tail: list[str] = []

        async def pump_stdout() -> None:
            nonlocal final
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    return
                progress.last_event_at = time.monotonic()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if (kind == "system" and event.get("subtype") == "init"
                        and on_session is not None and event.get("session_id")):
                    # 🔴 同步回调、不 await（文件头第 1 条）。回调抛异常不能把这一轮读断。
                    try:
                        on_session(str(event["session_id"]))
                    except Exception:  # noqa: BLE001
                        logger.exception("on_session 回调出错（只影响重启后续跑）")
                if kind == "stream_event":
                    # 逐字增量。🔴 只认 `text_delta`：`input_json_delta`（工具参数）
                    # 也是 `content_block_delta`，不按 `delta.type` 过滤就会把工具参数的
                    # JSON 推到用户卡片上（第 17 轮角色 F 实测的事件形状）。
                    inner = event.get("event") or {}
                    if (inner.get("type") == "content_block_start"
                            and (inner.get("content_block") or {}).get("type") == "text"
                            and streamed):
                        # 第 19 轮 S1-A5：前后两段文字中间隔着工具调用，原来直接粘在一起
                        # （「…并总结。看到了：…」）。和下面 `collected` 用同一个分隔符。
                        streamed.append(_BLOCK_SEP)
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            streamed.append(delta["text"])
                            if on_text is not None:
                                # 🔴 **必须同步、只记不发**：这里一个 await 都不能有
                                # （见本文件头第 1 条：spawn 到 gather 之间插 await 会让
                                # 队列永久堵死）。真正发飞书由 app.py 那个独立协程做。
                                on_text("".join(streamed))
                elif kind == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            collected.append(block["text"])
                        elif block.get("type") == "tool_use":
                            progress.tool_calls += 1
                            progress.last_tool = block.get("name", "")
                            if on_progress:
                                on_progress(progress)
                elif kind == "result":
                    final = event

        async def pump_stderr() -> None:
            assert proc.stderr is not None
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    stderr_tail.append(line)
                    del stderr_tail[:-20]

        work = asyncio.gather(pump_stdout(), pump_stderr(), proc.wait())
        waiters = [asyncio.ensure_future(work)]
        cancel_task = None
        if cancel is not None:
            cancel_task = asyncio.ensure_future(cancel.wait())
            waiters.append(cancel_task)

        done, _ = await asyncio.wait(
            waiters,
            timeout=effective.turn_timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )

        def _stop_child() -> None:
            if proc.returncode is None:
                proc.kill()

        if not done:
            # 超时
            logger.info("一轮超时 resume=%s 工具=%d 上一个工具=%s 限时=%ss", bool(resume),
                        progress.tool_calls, progress.last_tool or "-", effective.turn_timeout_sec)
            _stop_child()
            await proc.wait()
            for w in waiters:
                w.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiters[0]
            return TurnResult(
                ok=False,
                text="",
                duration_sec=time.monotonic() - started,
                timed_out=True,
                error=(
                    f"跑了 {effective.turn_timeout_sec // 60} 分钟还没完，先停下了。"
                    "可以把问题拆小一点再问，或者说得更具体些。"
                ),
            )

        if cancel_task is not None and cancel_task in done:
            # 用户发了 /stop
            logger.info("一轮被停下 resume=%s 工具=%d 耗时=%.1fs", bool(resume),
                        progress.tool_calls, time.monotonic() - started)
            _stop_child()
            await proc.wait()
            # 取消那三个 pump，并**等它们收尾**：不等的话 CancelledError 会在
            # 事件循环里裸抛出来，把日志刷脏（实测见过）。
            waiters[0].cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await waiters[0]
            partial = _BLOCK_SEP.join(collected).strip()
            return TurnResult(
                ok=False,
                text=partial,
                session_id=(final or {}).get("session_id") or resume,
                duration_sec=time.monotonic() - started,
                cancelled=True,
                error="已按你的要求停下",
            )

        if cancel_task is not None:
            cancel_task.cancel()

        duration = time.monotonic() - started
        text = _BLOCK_SEP.join(collected).strip()
        denials = (final or {}).get("permission_denials") or []
        session_id = (final or {}).get("session_id") or resume
        cost = float((final or {}).get("total_cost_usd") or 0.0)
        # 一轮一行：怎么结束的、调了几次工具、有没有丢结果。不记 prompt / 回复正文。
        logger.info(
            "一轮结束 会话=%s resume=%s stop=%s subtype=%s 工具=%d 拒绝=%d 回复字数=%d "
            "耗时=%.1fs 退出码=%s",
            str(session_id or "")[:8] or "-", bool(resume), (final or {}).get("stop_reason") or "-",
            (final or {}).get("subtype") or "-", progress.tool_calls,
            len(denials), len(text), duration, proc.returncode,
        )

        # 审批钩子挡下来了 ⇒ 正常停顿，不是故障。必须在下面"零输出=失败"之前判，
        # 因为 defer 时 text 往往是空的。
        if (final or {}).get("stop_reason") == "tool_deferred":
            return TurnResult(
                ok=True,
                text=text,
                session_id=session_id,
                cost_usd=cost,
                duration_sec=duration,
                denials=denials,
                deferred=True,
            )

        # 见文件头第 2 条：人发了消息，零回复 = 故障，不能静默成功。
        if not text:
            # 技术细节只进日志 —— 用户看 stderr 原文没有任何帮助，
            # 而且那里面可能带着路径、token 之类不该外发的东西。
            hint = "; ".join(stderr_tail[-3:]) if stderr_tail else ""
            subtype = (final or {}).get("subtype") or ""
            logger.error(
                "零输出：subtype=%s returncode=%s stderr=%s",
                subtype, proc.returncode, hint[:400],
            )
            # 给用户的话要能操作。最常见的真实原因是上一轮被中断导致
            # 这一轮 resume 到一个不完整的轮次（实测 2.7 秒就退出、根本没调模型）。
            return TurnResult(
                ok=False,
                text="",
                session_id=session_id,
                cost_usd=cost,
                duration_sec=duration,
                denials=denials,
                error="这一轮没出结果。点下面「🆕 新话题」再问一次，通常就好了。",
            )

        return TurnResult(
            ok=True,
            text=text,
            session_id=session_id,
            cost_usd=cost,
            duration_sec=duration,
            denials=denials,
        )
