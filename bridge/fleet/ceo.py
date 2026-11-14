"""CEO 编排：董事长（人）在群里下一个总任务，CEO 拆成子任务、分给不同会话和模型、
仲裁汇总、继续指挥，最后交付总结果；要人拍板时在 CEO 群里问人。

和原有功能的关系：**独立**。只用总控现成的 `control.start` / `control.send` 开会话、发指令；
子任务会话照常出现在「会话」总览里，可以各自绑群（复用 chatbind），群推送照常工作。
app.py 只在两处接线：`/ceo` 命令，和「CEO 群里的话交给 CEO」。

## 一次任务（Mission）的生命周期
planning → running → (waiting_human ⇄ running) → done / failed / cancelled
- planning：CEO 大脑（Claude，结构化输出、不给任何工具）把总任务拆成 1~4 个子任务
- running：并行开子任务会话；都回来后大脑复核：继续追加指令 / 问人 / 收尾
- waiting_human：CEO 群里发「🙋 需要你拍板」；董事长在群里回的下一句就是答复
- 追加指令最多 MAX_ROUNDS 轮，到了就必须收尾或问人
- 服务重启时还在跑的任务记成 interrupted，发「/ceo 继续」接着复核

🔴 安全：子任务和平时手机上新开会话走同一套闸门（control.start：目录白名单；
Codex 一律只读；Claude 按手机档位 + 审批钩子）。CEO 大脑本身没有任何工具，只出 JSON。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger("feishu-bridge.ceo")

MAX_SUBTASKS = 4
MAX_ROUNDS = 3
RESULT_CHARS = 3000
REPORT_CHARS = 6000

CLAUDE_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")
CODEX_MODELS = ("gpt-6-sol", "gpt-6-luna")
TOOLS = ("claude", "codex")
LABELS = {"claude": "Claude", "codex": "Codex"}

ACTIVE = ("planning", "running", "waiting_human")
FINAL = ("done", "failed", "cancelled")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "subtasks": {
            "type": "array", "minItems": 1, "maxItems": MAX_SUBTASKS,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "tool": {"type": "string", "enum": list(TOOLS)},
                    "model": {"type": "string"},
                    "project": {"type": "integer"},
                    "brief": {"type": "string"},
                },
                "required": ["title", "tool", "brief"],
            },
        },
    },
    "required": ["summary", "subtasks"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decision": {"type": "string", "enum": ["continue", "ask_human", "finish"]},
        "followups": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"n": {"type": "integer"}, "instruction": {"type": "string"}},
                      "required": ["n", "instruction"]},
        },
        "question": {"type": "string"},
        "report": {"type": "string"},
    },
    "required": ["summary", "decision"],
}

ROLE = (
    "你是一个 AI 团队的 CEO。董事长（人）给你一个总任务，你手下有若干会话（Claude 或 Codex，各用不同模型），"
    "每个会话完成一个子任务。你负责：拆分任务、给每个会话写清楚的指令、汇总进展、仲裁冲突、决定下一步，"
    "最后把总结果交给董事长。只有真正需要人拍板（取舍、授权、缺信息、风险操作）时才问董事长。\n"
    "能力约束：Codex 会话是**只读**的（只能读代码、分析、调研、评审、给方案，不能改文件、不能运行会写盘的命令）；"
    "Claude 会话能改文件和运行命令（在项目目录里）。需要改代码的子任务交给 Claude；评审、分析、调研可以交给 Codex。"
    "可选模型：Claude 用 " + " / ".join(CLAUDE_MODELS) + "；Codex 用 " + " / ".join(CODEX_MODELS) + "。"
    "所有输出用简体中文。"
)


def _clip(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def short(text: Any, limit: int = 24) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return _clip(text, limit)


def clean_model(tool: str, model: Any) -> str:
    model = str(model or "").strip()
    allowed = CLAUDE_MODELS if tool == "claude" else CODEX_MODELS
    return model if model in allowed else ""


# ---------- 大脑 ----------

class BrainError(Exception):
    pass


class ClaudeBrain:
    """`claude -p --json-schema …`，不给工具、不留会话、不加载用户设置。"""

    def __init__(self, *, claude_bin: str = "claude", env: Callable[[], dict] = None,
                 settings: Callable[[], dict] = None, model: str = "claude-sonnet-5-5",
                 timeout: float = 300, run: Callable = subprocess.run) -> None:
        self.claude_bin = claude_bin
        self.env = env or (lambda: dict(os.environ))
        self.settings = settings or (lambda: {})
        self.model = model
        self.timeout = timeout
        self.run = run

    def ask(self, prompt: str, schema: dict) -> dict:
        argv = [self.claude_bin, "-p", " " + prompt, "--output-format", "json",
                "--json-schema", json.dumps(schema, ensure_ascii=False),
                "--tools", "", "--no-session-persistence", "--setting-sources", "",
                "--strict-mcp-config", "--model", self.model]
        extra = dict(self.settings() or {})
        extra.pop("permissions", None)
        extra.pop("hooks", None)
        if extra:
            argv += ["--settings", json.dumps(extra, ensure_ascii=False)]
        try:
            proc = self.run(argv, capture_output=True, text=True, timeout=self.timeout,
                            stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir(), env=self.env())
        except subprocess.TimeoutExpired as exc:
            raise BrainError("CEO 想得太久，超时了") from exc
        except OSError as exc:
            raise BrainError(f"CEO 大脑起不来：{exc}") from exc
        try:
            data = json.loads(proc.stdout or "")
        except ValueError as exc:
            raise BrainError(f"CEO 大脑输出看不懂（退出码 {proc.returncode}）") from exc
        if data.get("is_error"):
            raise BrainError(_clip(data.get("result") or "CEO 大脑报错", 200))
        out = data.get("structured_output")
        if not isinstance(out, dict):
            try:
                out = json.loads(data.get("result") or "")
            except ValueError as exc:
                raise BrainError("CEO 大脑没按格式回答") from exc
        if not isinstance(out, dict):
            raise BrainError("CEO 大脑没按格式回答")
        return out


def plan_prompt(goal: str, projects: list, human: list) -> str:
    lines = [ROLE, "", "## 总任务", goal, "", "## 可用项目（project 填序号）"]
    for i, (name, _path) in enumerate(projects):
        lines.append(f"{i}. {name}")
    if human:
        lines += ["", "## 董事长补充"] + [f"- {h}" for h in human]
    lines += ["", f"把总任务拆成 1~{MAX_SUBTASKS} 个可以并行的子任务。简单任务就 1~2 个，不要为拆而拆。"
              "每个子任务给：title（短名）、tool、model、project（序号）、brief（发给那个会话的完整指令，"
              "说明目标、范围、交付物，并要求它最后用几句话总结结果）。summary 用两三句话向董事长说明分工。"]
    return "\n".join(lines)


def review_prompt(mission: dict) -> str:
    lines = [ROLE, "", "## 总任务", mission["goal"], "",
             f"## 现在是第 {mission['round']} 轮复核（最多追加 {MAX_ROUNDS} 轮指令）", "", "## 各子任务"]
    for sub in mission["subtasks"]:
        lines.append(f"### 子任务 {sub['n']}：{sub['title']}（{LABELS.get(sub['tool'], sub['tool'])}"
                     f"{' ' + sub['model'] if sub.get('model') else ''}，状态 {sub['state']}）")
        lines.append(_clip(sub.get("result") or "（没有结果）", RESULT_CHARS))
    if mission.get("human"):
        lines += ["", "## 董事长说过的话（按时间）"] + [f"- {h}" for h in mission["human"]]
    last = mission["round"] >= MAX_ROUNDS
    lines += ["", "决定下一步：",
              "- continue：还没达到总任务目标，在 followups 里给某些子任务（n 是子任务编号）追加指令；",
              "- ask_human：需要董事长拍板或补充信息，question 写清楚要他决定什么、有哪些选项、你的建议；",
              "- finish：总任务完成（或做不下去），report 写给董事长的最终交付：结论、各子任务产出、遗留问题。",
              "summary 用两三句话向董事长汇报这一轮的进展。"]
    if last:
        lines.append("🔴 已经到最后一轮：只能选 finish 或 ask_human。")
    return "\n".join(lines)


# ---------- 落盘 ----------

class MissionStore:
    """`ceo_missions.json`：CEO 群列表 + 每个任务。只存任务文字和子任务结果摘要，不存凭据。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.chats: dict = {}  # chat_id → {"name":…, "at":…}
        self.missions: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.chats = dict(data.get("chats") or {})
            self.missions = {m["id"]: m for m in data.get("missions") or [] if isinstance(m, dict) and m.get("id")}

    def save(self) -> bool:
        with self.lock:
            keep = sorted(self.missions.values(), key=lambda m: m.get("created_at", 0))[-50:]
            self.missions = {m["id"]: m for m in keep}
            data = {"chats": self.chats, "missions": keep}
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".ceo-")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                return True
            except OSError:
                logger.exception("CEO 任务记录存不下来")
                return False

    def latest(self, chat_id: str) -> Optional[dict]:
        with self.lock:
            rows = [m for m in self.missions.values() if m.get("chat_id") == chat_id]
            return max(rows, key=lambda m: m.get("created_at", 0)) if rows else None

    def active(self, chat_id: str) -> Optional[dict]:
        m = self.latest(chat_id)
        return m if m is not None and m.get("state") in ACTIVE else None


# ---------- 编排 ----------

class Orchestrator:
    """CEO 本体。所有对外动作都是注入的：大脑、总控、发消息、档位、项目列表 —— 测试里全换成假的。"""

    def __init__(self, store: MissionStore, brain: Any, control: Any, say: Callable[[str, str], Any], *,
                 policy: Callable[[], Any], projects: Callable[[], list],
                 approval_dir: Callable[[str, str], str] = lambda tool, key: "",
                 timeout: Callable[[], float] = lambda: 600.0,
                 spawn: Optional[Callable[[Callable], Any]] = None,
                 now: Callable[[], float] = time.time) -> None:
        self.store = store
        self.brain = brain
        self.control = control
        self.say = say
        self.policy = policy
        self.projects = projects
        self.approval_dir = approval_dir
        self.timeout = timeout
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="ceo").start())
        self.now = now

    # --- CEO 群 ---
    def is_ceo_chat(self, chat_id: str) -> bool:
        return bool(chat_id) and chat_id in self.store.chats

    def mark_chat(self, chat_id: str, name: str = "") -> None:
        with self.store.lock:
            self.store.chats[chat_id] = {"name": name, "at": self.now()}
            self.store.save()

    def unmark_chat(self, chat_id: str) -> bool:
        with self.store.lock:
            gone = self.store.chats.pop(chat_id, None) is not None
            self.store.save()
            return gone

    # --- 入口 ---
    def start(self, chat_id: str, goal: str) -> tuple:
        goal = str(goal or "").strip()
        if not goal:
            return False, "要给 CEO 的任务是空的。发「/ceo 任务内容」。"
        with self.store.lock:
            busy = self.store.active(chat_id)
            if busy is not None:
                return False, f"这个群里还有一个任务没结束（{short(busy['goal'])}）。先发「/ceo 停」或等它做完。"
            mid = uuid.uuid4().hex[:8]
            self.store.missions[mid] = {
                "id": mid, "chat_id": chat_id, "goal": goal, "state": "planning", "round": 0,
                "limit": MAX_ROUNDS, "subtasks": [], "human": [], "question": "", "report": "",
                "events": [], "created_at": self.now(), "updated_at": self.now(),
            }
            if chat_id not in self.store.chats:
                self.store.chats[chat_id] = {"name": "", "at": self.now()}
            self.store.save()
        self.spawn(lambda: self._safe_run(mid))
        return True, mid

    def handle_text(self, chat_id: str, text: str) -> bool:
        """CEO 群里的一句普通话：在等拍板 ⇒ 当答复；在跑 ⇒ 记下来下一轮带上；没任务 ⇒ 当新任务。"""
        text = str(text or "").strip()
        if not text or not self.is_ceo_chat(chat_id):
            return False
        with self.store.lock:
            m = self.store.active(chat_id)
            if m is None:
                pass
            elif m["state"] == "waiting_human":
                m["human"].append(text)
                m["question"] = ""
                m["state"] = "running"
                if m["round"] >= m["limit"]:
                    m["limit"] = m["round"] + MAX_ROUNDS
                self._event(m, "董事长答复")
                self.store.save()
                mid = m["id"]
                self.say(chat_id, "👌 收到你的决定，CEO 接着安排。")
                self.spawn(lambda: self._safe_run(mid))
                return True
            else:
                m["human"].append(text)
                self.store.save()
                self.say(chat_id, "📝 记下了，CEO 下一次复核时会带上这句。")
                return True
        ok, msg = self.start(chat_id, text)
        if not ok:
            self.say(chat_id, msg)
        return True

    def cancel(self, chat_id: str) -> str:
        with self.store.lock:
            m = self.store.active(chat_id) or self._interrupted(chat_id)
            if m is None:
                return "这个群里现在没有在跑的 CEO 任务。"
            m["state"] = "cancelled"
            self._event(m, "董事长叫停")
            self.store.save()
            subs = [s for s in m["subtasks"] if s.get("state") == "running" and s.get("session_id")]
        for sub in subs:
            try:
                self.control.stop(sub["tool"], sub["session_id"])
            except Exception:  # noqa: BLE001
                logger.debug("停子任务失败", exc_info=True)
        return f"🛑 已叫停：{short(m['goal'], 40)}。正在跑的子任务会尽量停下，之后的结果不再处理。"

    def resume(self, chat_id: str) -> str:
        with self.store.lock:
            m = self._interrupted(chat_id)
            if m is None:
                return "没有被打断的 CEO 任务可以继续。"
            m["state"] = "running" if m["subtasks"] else "planning"
            for sub in m["subtasks"]:
                if sub.get("state") == "running":
                    sub["state"] = "done" if sub.get("result") else "pending"
            self._event(m, "继续")
            self.store.save()
            mid = m["id"]
        self.spawn(lambda: self._safe_run(mid))
        return f"▶️ 接着做：{short(m['goal'], 40)}"

    def recover(self) -> int:
        """服务重启：还在跑的任务记成 interrupted（线程已经没了），等董事长发「/ceo 继续」。"""
        n = 0
        with self.store.lock:
            for m in self.store.missions.values():
                if m.get("state") in ("planning", "running"):
                    m["state"] = "interrupted"
                    n += 1
            if n:
                self.store.save()
        return n

    def _interrupted(self, chat_id: str) -> Optional[dict]:
        m = self.store.latest(chat_id)
        return m if m is not None and m.get("state") == "interrupted" else None

    def status_text(self, chat_id: str) -> str:
        m = self.store.latest(chat_id)
        if m is None:
            return ("这个群还没有 CEO 任务。发「/ceo 任务内容」交给 CEO：它会拆成子任务、分给不同的 Claude / Codex 会话，"
                    "汇总进展，要你拍板时在这里问你，最后交总结果。")
        names = {"planning": "拆分中", "running": "执行中", "waiting_human": "等你拍板", "done": "已完成",
                 "failed": "失败", "cancelled": "已叫停", "interrupted": "被重启打断（发「/ceo 继续」）"}
        lines = [f"🧭 **CEO 任务**：{short(m['goal'], 60)}", f"状态：{names.get(m['state'], m['state'])}"
                 f" · 第 {m['round']} 轮"]
        for sub in m["subtasks"]:
            lines.append(f"{self._icon(sub)} {sub['n']}. {sub['title']}（{self._who(sub)}）")
        if m["state"] == "waiting_human" and m.get("question"):
            lines.append(f"🙋 在等你：{m['question']}")
        return "\n".join(lines)

    # --- 内部 ---
    @staticmethod
    def _icon(sub: dict) -> str:
        return {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}.get(sub.get("state"), "•")

    @staticmethod
    def _who(sub: dict) -> str:
        return LABELS.get(sub["tool"], sub["tool"]) + (f" {sub['model']}" if sub.get("model") else "") + \
            (f" · {sub['project']}" if sub.get("project") else "")

    def _event(self, m: dict, what: str) -> None:
        m["updated_at"] = self.now()
        m["events"] = (m.get("events") or [])[-40:] + [[round(self.now()), what]]

    def _get(self, mid: str) -> Optional[dict]:
        return self.store.missions.get(mid)

    def _alive(self, mid: str) -> bool:
        m = self._get(mid)
        return m is not None and m.get("state") in ("planning", "running")

    def _safe_run(self, mid: str) -> None:
        try:
            self._run(mid)
        except Exception as exc:  # noqa: BLE001 —— 一个任务炸了不能拖垮桥
            logger.exception("CEO 任务出错 id=%s", mid)
            with self.store.lock:
                m = self._get(mid)
                if m is not None and m.get("state") in ("planning", "running"):
                    m["state"] = "failed"
                    self._event(m, f"出错：{short(exc, 80)}")
                    self.store.save()
            if m is not None:
                self.say(m["chat_id"], f"❌ CEO 这个任务做不下去了：{_clip(exc, 200)}\n可以换个说法再发一次「/ceo 任务」。")

    def _run(self, mid: str) -> None:
        m = self._get(mid)
        if m is None:
            return
        chat = m["chat_id"]
        if m["state"] == "planning" and not m["subtasks"]:
            if not self._plan(m):
                return
        while self._alive(mid):
            pending = [s for s in m["subtasks"] if s["state"] == "pending"]
            if pending:
                self._dispatch(m, pending)
                if not self._alive(mid):
                    return
            m["round"] += 1
            try:
                review = self.brain.ask(review_prompt(m), REVIEW_SCHEMA)
            except BrainError as exc:
                with self.store.lock:
                    m["state"] = "failed"
                    self._event(m, f"复核失败：{exc}")
                    self.store.save()
                self.say(chat, f"❌ CEO 复核没做成：{exc}\n发「/ceo 继续」再试一次。")
                return
            if not self._alive(mid):
                return
            decision = review.get("decision")
            summary = _clip(review.get("summary"), 800)
            if decision == "continue" and m["round"] >= m["limit"]:
                decision = "finish" if not review.get("question") else "ask_human"
            if decision == "ask_human":
                question = _clip(review.get("question") or summary or "需要你拍板下一步怎么做。", 1500)
                with self.store.lock:
                    m["state"] = "waiting_human"
                    m["question"] = question
                    self._event(m, "问董事长")
                    self.store.save()
                self.say(chat, f"🙋 **需要你拍板**（CEO · 第 {m['round']} 轮）\n{summary}\n\n{question}\n\n"
                               "直接在群里回一句就行；发「/ceo 停」叫停。")
                return
            if decision == "finish":
                report = _clip(review.get("report") or summary, REPORT_CHARS)
                with self.store.lock:
                    m["state"] = "done"
                    m["report"] = report
                    self._event(m, "交付")
                    self.store.save()
                self.say(chat, f"🏁 **CEO 交付总结果**：{short(m['goal'], 60)}\n\n{report}\n\n"
                               + self._roster(m))
                return
            followups = review.get("followups") or []
            picked = 0
            with self.store.lock:
                for f in followups:
                    sub = next((s for s in m["subtasks"] if s["n"] == f.get("n")), None)
                    instr = str(f.get("instruction") or "").strip()
                    if sub is None or not instr:
                        continue
                    sub["next"] = instr
                    sub["state"] = "pending"
                    picked += 1
                self._event(m, f"第 {m['round']} 轮追加 {picked} 条指令")
                self.store.save()
            if not picked:
                with self.store.lock:
                    m["state"] = "done"
                    m["report"] = summary
                    self.store.save()
                self.say(chat, f"🏁 **CEO 交付总结果**\n\n{summary}\n\n" + self._roster(m))
                return
            self.say(chat, f"🧭 **CEO 第 {m['round']} 轮复核**\n{summary}\n继续给 {picked} 个子任务追加指令。")

    def _plan(self, m: dict) -> bool:
        chat = m["chat_id"]
        projects = list(self.projects() or [])
        if not projects:
            with self.store.lock:
                m["state"] = "failed"
                self.store.save()
            self.say(chat, "❌ 没有手机能用的项目目录，CEO 没法派活。")
            return False
        self.say(chat, f"🧭 CEO 收到任务，正在拆分：{short(m['goal'], 60)}")
        try:
            plan = self.brain.ask(plan_prompt(m["goal"], projects, m["human"]), PLAN_SCHEMA)
        except BrainError as exc:
            with self.store.lock:
                m["state"] = "failed"
                self._event(m, f"拆分失败：{exc}")
                self.store.save()
            self.say(chat, f"❌ CEO 没拆成：{exc}")
            return False
        subs = []
        for i, raw in enumerate((plan.get("subtasks") or [])[:MAX_SUBTASKS], 1):
            tool = raw.get("tool") if raw.get("tool") in TOOLS else "claude"
            try:
                idx = int(raw.get("project", 0))
            except (TypeError, ValueError):
                idx = 0
            name, path = projects[idx] if 0 <= idx < len(projects) else projects[0]
            brief = str(raw.get("brief") or raw.get("title") or "").strip()
            if not brief:
                continue
            subs.append({"n": i, "title": short(raw.get("title") or brief, 30), "tool": tool,
                         "model": clean_model(tool, raw.get("model")), "project": name, "cwd": path,
                         "brief": brief, "next": brief, "state": "pending", "session_id": "",
                         "result": "", "turns": 0})
        if not subs:
            with self.store.lock:
                m["state"] = "failed"
                self.store.save()
            self.say(chat, "❌ CEO 没拆出能做的子任务，换个说法再试。")
            return False
        with self.store.lock:
            m["subtasks"] = subs
            m["state"] = "running"
            self._event(m, f"拆成 {len(subs)} 个子任务")
            self.store.save()
        lines = [f"🧭 **CEO 分工**（{len(subs)} 个子任务）", _clip(plan.get("summary"), 800)]
        for sub in subs:
            lines.append(f"{sub['n']}. {sub['title']} → {self._who(sub)}")
        self.say(chat, "\n".join(line for line in lines if line))
        return True

    def _dispatch(self, m: dict, subs: list) -> None:
        threads = []
        for sub in subs:
            sub["state"] = "running"
            t = threading.Thread(target=self._one, args=(m, sub), daemon=True, name=f"ceo-{sub['n']}")
            threads.append(t)
        with self.store.lock:
            self.store.save()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.store.lock:
            self.store.save()

    def _one(self, m: dict, sub: dict) -> None:
        instr = sub.get("next") or sub["brief"]
        if sub["turns"] == 0:
            instr = (f"【CEO 派给你的子任务 {sub['n']}：{sub['title']}】\n总任务：{m['goal']}\n\n{instr}\n\n"
                     "做完后用几句话总结：做了什么、结果、还有什么问题。")
        policy = self.policy()
        timeout = float(self.timeout() or 600)
        try:
            if sub.get("session_id"):
                res = self.control.send(sub["tool"], sub["session_id"], instr, policy=policy,
                                        approval_dir=self.approval_dir(sub["tool"], sub["session_id"]),
                                        timeout=timeout, relay=True)
            else:
                res = self.control.start(sub["tool"], sub["cwd"], instr, policy=policy,
                                         approval_dir=self.approval_dir(sub["tool"], f"ceo-{m['id']}-{sub['n']}"),
                                         timeout=timeout, model=sub.get("model", ""))
        except Exception as exc:  # noqa: BLE001
            res = None
            err = str(exc)
        else:
            err = ""
        with self.store.lock:
            sub["turns"] += 1
            sub["next"] = ""
            if res is not None and getattr(res, "session_id", ""):
                sub["session_id"] = res.session_id
            if res is not None and getattr(res, "ok", False):
                sub["state"] = "done"
                sub["result"] = _clip(res.text, RESULT_CHARS)
            else:
                sub["state"] = "failed"
                sub["result"] = "没做成：" + _clip(err or getattr(res, "text", "") or "没有回复", 600)
        if self._alive(m["id"]):
            self.say(m["chat_id"], f"{self._icon(sub)} 子任务 {sub['n']}「{sub['title']}」（{self._who(sub)}）"
                                   f"{'回来了' if sub['state'] == 'done' else '没做成'}：\n{_clip(sub['result'], 600)}")

    def _roster(self, m: dict) -> str:
        lines = ["参与的会话（在「会话」总览里都能找到，可以单独点进去接着聊）："]
        for sub in m["subtasks"]:
            lines.append(f"{self._icon(sub)} {sub['n']}. {sub['title']}（{self._who(sub)}）")
        return "\n".join(lines)


CEO_COMMANDS = ("/ceo",)
CEO_HELP = (
    "🧭 **CEO 编排**\n"
    "- `/ceo 任务内容`：把总任务交给 CEO，它拆成子任务、分给不同的 Claude / Codex 会话和模型，"
    "汇总进展、要你拍板时问你，最后交总结果\n"
    "- `/ceo 群`：把当前群设成 CEO 群，之后在群里直接说话就是给 CEO 的\n"
    "- `/ceo 状态`：看当前任务进度\n"
    "- `/ceo 停`：叫停当前任务；`/ceo 继续`：接着做被重启打断的任务\n"
    "- `/ceo 退出`：这个群不再当 CEO 群"
)


def parse_command(text: str) -> tuple:
    """`/ceo …` → (动作, 参数)。动作：help/mark/unmark/status/stop/resume/start。"""
    body = str(text or "").strip()
    if not body.lower().startswith("/ceo"):
        return "", ""
    rest = body[4:].strip()
    if not rest:
        return "help", ""
    word = rest.split()[0]
    if rest in ("群", "开启", "设为CEO群", "设为 CEO 群", "on"):
        return "mark", ""
    if rest in ("退出", "关闭", "off"):
        return "unmark", ""
    if rest in ("状态", "进度", "status"):
        return "status", ""
    if rest in ("停", "停下", "取消", "stop", "cancel"):
        return "stop", ""
    if rest in ("继续", "resume"):
        return "resume", ""
    if word in ("帮助", "help"):
        return "help", ""
    return "start", rest
