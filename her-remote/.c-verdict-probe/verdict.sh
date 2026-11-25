# 🔴 绿灯判据：**pid 非 0** 且 **重启次数没在涨** 且 **这一轮的日志里没有 Traceback**。
#
# 原来只看 `launchctl print` 返回 0 —— 那只证明 job 注册上了。
# 2026-11-05 实测（造一个秒退的 job 真注册了一遍、跑完就卸掉）：
#   崩溃循环中：print 仍然 rc=0、`state = spawn scheduled`、**整份输出里没有 pid 这一行**、
#               `last exit code = 1`、runs 在 12 秒内从 4 涨到 5
#   真的活着：  `state = running`、`pid = 7082`、runs 不动
# 也就是说 rc 完全区分不开这两种，而 pid 在不在 + runs 涨不涨能一刀切开。
#
# 用 awk 取字段而不是 sed：那几行是**制表符**缩进的，而 BSD sed 不认 `\t`
# （实测 `sed -n 's/^\tpid = ...'` 取不到任何东西，静默为空 ⇒ 判据恒「没 pid」⇒
# 一台好机器也会被报成起不来）。awk 默认按空白切字段，不用写出那个字符。
svc_field() {  # svc_field <字段名>，取不到就返回空
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
    | awk -v want="$1" '$1==want && $2=="=" {print $3; exit}'
}

SVC_PID="$(svc_field pid)"
SVC_RUNS_1="$(svc_field runs)"
sleep 5
SVC_PID_2="$(svc_field pid)"
SVC_RUNS_2="$(svc_field runs)"

# 这一轮启动之后新写进日志的东西里有没有 Traceback。
# 🔴 只拿**错误类名**，绝不打日志原文：本机 494 行真日志里有 189 个敏感 token
# （ws ticket、open_id、真名路径），而这一屏是用户最可能截图发给 IT 的一屏。
# Traceback 的最后一行形如 `RuntimeError: ...` ⇒ 冒号前那一段就是类名。
CRASH_CLASS=""
if new_log | grep -q "^Traceback"; then
  CRASH_CLASS="$(new_log | grep -E '^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Interrupt):' \
    | tail -1 | sed 's/:.*//')"
  [ -n "$CRASH_CLASS" ] || CRASH_CLASS="启动失败"
fi

if [ -n "$CRASH_CLASS" ]; then
  # 起来就崩。这条**必须比「还在重试连接」先判** —— 崩溃时日志里既没有
  # connected 也没有 connect failed，不先拦就会落到「等两分钟」，而他永远等不出结果。
  die "✗ 它装上了，但一启动就崩了（错误类型：${CRASH_CLASS}）。

不是你哪一步做错了，是程序在这台机器上起不来。

下一步：
  1. 双击 feishu-agent-doctor-mac.command 出一份诊断
  2. 把诊断结果和上面那个错误类型一起发给 IT
  3. 想重来一次：先双击 feishu-agent-uninstall-mac.command 卸掉，再双击安装器"
elif [ -z "$SVC_PID" ] || [ "$SVC_PID" = 0 ]; then
  die "✗ 后台服务没能留在运行状态（它起来又退出了）。

开机自启已经设好了，但进程没活下来。

下一步：
  1. 双击 feishu-agent-doctor-mac.command —— 它会读日志，直接告诉你是哪一步出的问题
  2. 诊断里看不懂的部分，连同这个文件一起发给 IT：
         ${LOG_DIR}/agent.log
     （日志里不含你的 App Secret）
  3. 想重来一次：先双击 feishu-agent-uninstall-mac.command 卸掉，再双击安装器"
elif [ -n "$SVC_RUNS_1" ] && [ -n "$SVC_RUNS_2" ] && [ "$SVC_RUNS_2" -gt "$SVC_RUNS_1" ]; then
  # 5 秒内又重启了一次 ⇒ 正在崩溃循环。此刻 pid 可能恰好非 0（刚被拉起来的那个），
  # 所以 pid 那一条拦不住这种形状，必须单独数 runs。
  die "✗ 它装上了，但在反复重启（5 秒内又起了一次）。

不是你哪一步做错了，是程序在这台机器上待不住。

下一步：
  1. 双击 feishu-agent-doctor-mac.command 出一份诊断
  2. 把诊断结果发给 IT，告诉他们「服务在反复重启」
  3. 想重来一次：先双击 feishu-agent-uninstall-mac.command 卸掉，再双击安装器"
else
  ok "✓ 已经在后台跑起来了，以后开机自动启动"
fi


