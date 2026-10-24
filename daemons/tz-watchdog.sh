#!/bin/bash
# 时区看门狗 (参考模板) —— 安装程序会把 __TZ_TARGET__ 和 __HOME__ 替换后安装
# 作用: 系统时区偏离目标值时自动改回,并记录日志
TARGET="__TZ_TARGET__"
LOG="__HOME__/Library/Logs/tz-watchdog-fork.log"

# 只在共享默认目录归 fork 且个人版正在使用时维护时区。
owner=$(cat "__HOME__/.claude-switcher-sessions/active-edition" 2>/dev/null)
[ "$owner" = fork ] || exit 0
if ! pgrep -U "__USER_UID__" -x ClaudeSwitcher-fork >/dev/null 2>&1; then
    pgrep -U "__USER_UID__" -f '^/Applications/Claude.app/Contents/MacOS/Claude($| )' >/dev/null 2>&1 || exit 0
fi
# 原版看门狗也在运行且目标冲突时拒绝互相改写。
other='/Library/Application Support/claude-switcher/tz-watchdog.sh'
if launchctl print system/local.tz-watchdog >/dev/null 2>&1 && [ -f "$other" ]; then
    value=$(sed -n 's/^TARGET=//p' "$other" | head -1)
    value=${value#\"}; value=${value%\"}
    [ -z "$value" ] || [ "$value" = "$TARGET" ] || exit 0
fi

current=$(readlink /etc/localtime 2>/dev/null)
if [ "$current" != "/var/db/timezone/zoneinfo/$TARGET" ]; then
    /usr/sbin/systemsetup -settimezone "$TARGET" >>"$LOG" 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z') 时区异常: [$current] 已自动恢复为 $TARGET" >>"$LOG"
fi
