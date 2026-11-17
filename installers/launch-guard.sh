#!/bin/bash
# Her remote 的启动守卫：launchd -> 本脚本 -> exec python -m bridge.app
#
# 为什么要有（第 34 轮）：bridge 一启动就崩（依赖坏了、配置坏了、升级坏了）时，
# launchd 的 KeepAlive 会每 10 秒（launchd 默认节流，man launchd.plist 的 ThrottleInterval）
# 拉起一次，永远转下去；用户看到的只是「机器人不理我」，外面也没人知道。
# 崩的恰恰是 Python 主程序，在它里面记账可能根本跑不到（崩在 import）⇒ 记账放在这一层，
# 只用 /bin/bash 3.2 + 系统自带命令，不 import 任何 Python。
#
# 做三件事：
#   1. 数「连续几次启动后很快就退出」：这次启动离上次 exec python 不到 QUICK_SEC 秒 ⇒ 连击 +1，
#      否则清零。（exec 之后本脚本就没了，看不到退出码 ⇒ 用「两次启动的间隔」判断上一次活了多久。
#      看门狗自杀是静默 15 分钟之后，远大于 QUICK_SEC，不会被算进来。）
#   2. 连击到 CRASH_STREAK 次 ⇒ 降级：exec 之前先睡一段（60s 起翻倍，封顶 1800s），
#      睡的时候进程活着 ⇒ launchd 不再狂拉；睡醒照样试一次，原因消失了就自己好。
#   3. 每段降级只上报一次：GET latest.json，带且只带 v/id/os/u/app 5 个键，u=crashloop。
#      🔴 隐私红线：只发枚举词，不发错误内容、路径、用户名。域名写死，不从环境变量读。
#
# 刻意不做：自动换回 bridge.prev。prev 只在安装器跑着的那几十秒里存在（连上就删），
# 那段由安装器 UPDATE-ROLLBACK/UPDATE-VERDICT 负责；这里再动目录会和安装器抢。
#
# 用法（plist 的 ProgramArguments）：/bin/bash launch-guard.sh <python> -m bridge.app
# 状态文件：<本脚本所在目录>/crashloop.state（安装器每次装完会删掉它，重新数）

HOME_DIR="${CARHER_GUARD_HOME:-$(cd "$(dirname "$0")" && pwd)}"
STATE="$HOME_DIR/crashloop.state"
QUICK_SEC=60          # 活不到这么久就算「很快就退出」
CRASH_STREAK=5        # 连续这么多次 ⇒ 降级（launchd 10 秒一拉 ⇒ 约 40~50 秒后进入）
BACKOFF_FIRST=60
BACKOFF_MAX=1800
REPORT_URL="https://chat.auto-link.com.cn/ccdl/feishu-agent/latest.json"

log() { printf '%s [launch-guard] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >&2; }

[ "$#" -ge 1 ] || { log "no command given"; exit 64; }

# ── 读状态（坏了就当没有） ──
streak=0; last_exec=0; reported=0
if [ -f "$STATE" ]; then
  while IFS='=' read -r k v; do
    case "$v" in ''|*[!0-9]*) continue ;; esac
    case "$k" in
      streak) streak="$v" ;;
      last_exec) last_exec="$v" ;;
      reported) reported="$v" ;;
    esac
  done < "$STATE"
fi

now="$(date +%s)"
gap=$((now - last_exec))
if [ "$last_exec" -gt 0 ] && [ "$gap" -ge 0 ] && [ "$gap" -lt "$QUICK_SEC" ]; then
  streak=$((streak + 1))
else
  # 上一次活够了 / 第一次 / 重启过 / 时钟往回跳 ⇒ 重新数
  streak=1; reported=0
fi

save() {
  local tmp="$STATE.tmp.$$"
  printf 'streak=%s\nlast_exec=%s\nreported=%s\n' "$streak" "$1" "$reported" > "$tmp" 2>/dev/null \
    && mv -f "$tmp" "$STATE" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  return 0
}

# ── 上报：只拼 5 个键，每个值先按白名单形状过一遍，不像就留空 ──
report_crashloop() {
  # StarNexus（有 .local-deploy 标记）不向官方服务器上报任何东西（1.2.0）
  [ -f "$HOME_DIR/.local-deploy" ] && return 0
  local v id app qs
  v="$(sed -n 's/^BUILD_ID = "\(.*\)"$/\1/p' "$HOME_DIR/bridge/buildinfo.py" 2>/dev/null | head -1 \
       | tr -cd 'A-Za-z0-9._-' | cut -c1-60)"
  id="$(head -c 64 "$HOME_DIR/install_id" 2>/dev/null | tr -d '[:space:]')"
  case "$id" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) id="" ;;
  esac
  app="$(sed -n 's/^app_id *= *"\(.*\)"/\1/p' "$HOME_DIR/config.toml" 2>/dev/null | head -1)"
  case "$app" in
    cli_*[!A-Za-z0-9]*|cli_) app="" ;;   # cli_ 后面要有、且只能是字母数字
    cli_*) [ "${#app}" -le 44 ] || app="" ;;
    *) app="" ;;
  esac
  qs="v=${v}"
  [ -n "$id" ] && qs="${qs}&id=${id}"
  qs="${qs}&os=mac&u=crashloop&app=${app}"
  curl -s -o /dev/null -m 10 --proto '=https' "${REPORT_URL}?${qs}" >/dev/null 2>&1
}

if [ "$streak" -ge "$CRASH_STREAK" ]; then
  # 第 CRASH_STREAK 次起：60, 120, 240 … 封顶 BACKOFF_MAX
  wait_sec="$BACKOFF_FIRST"
  i="$CRASH_STREAK"
  while [ "$i" -lt "$streak" ] && [ "$wait_sec" -lt "$BACKOFF_MAX" ]; do
    wait_sec=$((wait_sec * 2)); i=$((i + 1))
  done
  [ "$wait_sec" -le "$BACKOFF_MAX" ] || wait_sec="$BACKOFF_MAX"
  log "started ${streak} times in a row and each exited within ${QUICK_SEC}s; waiting ${wait_sec}s before next try"
  if [ "$reported" != 1 ]; then
    if report_crashloop; then
      reported=1; log "reported crashloop"
    else
      log "crashloop report failed, will retry next round"
    fi
  fi
  save "$last_exec"
  # 停服务（bootout）时别让这一觉拖住：收到 TERM 就连 sleep 一起结束
  sleep "$wait_sec" &
  sleep_pid=$!
  trap 'kill "$sleep_pid" 2>/dev/null; exit 0' TERM INT HUP
  wait "$sleep_pid"
  trap - TERM INT HUP
  now="$(date +%s)"
fi

save "$now"
exec "$@"
