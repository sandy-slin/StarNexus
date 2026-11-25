#!/bin/bash
# 把仓库里的 bridge/ 同步到本机正在服务的实例，并重启。
#
# 为什么要有这个脚本（2026-11-05 一晚手工做了 8 次，栽了三种）：
#   1. 改完源码以为上线了 —— 跑着的还是几小时前的旧进程（改完源码 ≠ 上线）
#   2. 把 fleet/ 下的文件拷到了 bridge/ 顶层（多出一个孤儿 groupsetup.py）
#   3. 只跑了 py_compile —— 它查不出漏 import，进程会起不来
#
# 用法：  bash scripts/deploy_local.sh            # 测试 → 备份 → 同步 → import → 重启 → 等连上
#         bash scripts/deploy_local.sh --no-test  # 跳过全量测试（只在刚跑过时用）
#
# 只动本机的 ~/.carher-feishu-agent/bridge/，不碰配置、不碰飞书应用、不 build 安装包。
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${CARHER_AGENT_HOME:-$HOME/.carher-feishu-agent}"
LABEL="com.carher.feishu-agent"
RUN_TESTS=1
[ "${1:-}" = "--no-test" ] && RUN_TESTS=0

[ -d "$DEST/bridge" ] || { echo "✗ 没找到部署目录 ${DEST}/bridge（还没装过？）"; exit 2; }

if [ "$RUN_TESTS" = 1 ]; then
  echo "▶ 全量测试（约 1 分钟）"
  # 只跑一遍：输出落临时文件，退出码单独拿（管道接 tail 会把 pytest 的退出码吃掉）
  TEST_LOG="$(mktemp -t carher-deploy-test)"
  set +e
  "$SRC/.venv/bin/python" -m pytest "$SRC/tests" -q -p no:cacheprovider >"$TEST_LOG" 2>&1
  TEST_RC=$?
  set -e
  tail -1 "$TEST_LOG"
  if [ "$TEST_RC" -ne 0 ]; then
    grep -E "^FAILED" "$TEST_LOG" | head -10
    rm -f "$TEST_LOG"
    echo "✗ 测试没过，不上线（旧进程照常在跑）"
    exit 1
  fi
  rm -f "$TEST_LOG"
fi

# 要同步哪些：源码和部署不一样的 .py（含新增的）。按相对路径算，fleet/ 就落进 fleet/。
CHANGED=()
while IFS= read -r rel; do CHANGED+=("$rel"); done < <(
  cd "$SRC" && find bridge -name '*.py' -not -path '*/__pycache__/*' | sort | while read -r rel; do
    cmp -s "$rel" "$DEST/$rel" || echo "$rel"
  done
)
if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "✓ 部署那份已经和源码一致，没有要同步的"
else
  STAMP="$(date +%Y%m%dT%H%M%S)"
  BACKUP="$DEST/backups/bridge-$STAMP"
  echo "▶ 备份并同步 ${#CHANGED[@]} 个文件 → ${BACKUP}"
  for rel in "${CHANGED[@]}"; do
    mkdir -p "$BACKUP/$(dirname "$rel")" "$DEST/$(dirname "$rel")"
    [ -f "$DEST/$rel" ] && cp "$DEST/$rel" "$BACKUP/$rel"
    cp "$SRC/$rel" "$DEST/$rel"
    echo "    $rel"
  done
fi

# 部署那边多出来、源码里没有的 .py（拷错层留下的孤儿）只报不删 —— 删之前要人看一眼
ORPHANS="$(cd "$DEST" && find bridge -name '*.py' -not -path '*/__pycache__/*' | while read -r rel; do
  [ -f "$SRC/$rel" ] || echo "$rel"; done)"
[ -n "$ORPHANS" ] && echo "⚠️ 部署那边有源码里没有的文件（可能是拷错层的孤儿，确认后手动删）：" && echo "$ORPHANS"

# 🔴 清掉部署那边的字节码缓存。Python 判 .pyc 过不过期只看「源文件 mtime（秒）+ 大小」——
# 同一秒内换进一个**一样大**的新文件，旧 .pyc 会被当成有效的继续用 ⇒ 跑的还是旧代码。
# 2026-11-06 实测撞上：变异验证把 dict 里两项对调（大小不变）、同一秒内改回，全量测试就一直读到变异版。
find "$DEST/bridge" -name '*.pyc' -delete 2>/dev/null || true

echo "▶ 用部署那份 venv 真 import"
( cd "$DEST" && ./venv/bin/python -c "import bridge.app" ) || { echo "✗ import 失败，没重启（旧进程还在跑）"; exit 1; }

echo "▶ 重启"
MARK="$(wc -c < "$DEST/logs/agent.log" | tr -d ' ')"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

# 等本轮日志里出现 connected / connect failed / Traceback（只看重启之后新写的）
for _ in $(seq 1 20); do
  sleep 1
  NEW="$(tail -c "+$((MARK + 1))" "$DEST/logs/agent.log" 2>/dev/null || true)"
  if printf '%s' "$NEW" | grep -q "Traceback"; then
    echo "✗ 起来就崩了，最后一类错：$(printf '%s' "$NEW" | grep -E '^[A-Za-z]+(Error|Exception)' | tail -1 | cut -d: -f1)"
    exit 1
  fi
  if printf '%s' "$NEW" | grep -q "connect failed"; then
    echo "⚠️ 进程起来了但没连上飞书（看 $DEST/logs/agent.log）"; exit 1
  fi
  if printf '%s' "$NEW" | grep -q "connected to"; then
    launchctl print "gui/$(id -u)/$LABEL" | grep -E '^\s+(pid|runs) ' | tr -s ' \t' ' '
    echo "✓ 已上线并连上飞书"
    exit 0
  fi
done
echo "⚠️ 20 秒内没等到 connected，看一眼 $DEST/logs/agent.log"
exit 1
