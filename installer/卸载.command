#!/bin/bash
# 只卸载 fork 的程序和服务,保留两版账号资料及原版安装。
set -euo pipefail
# 先交还原版现场,再移除个人版程序;失败时保留安装和所有数据。
if [ -f "$HOME/.claude-switcher-sessions/active-edition" ] && \
   [ "$(cat "$HOME/.claude-switcher-sessions/active-edition")" = fork ]; then
    "$HOME/.local/bin/claude-switch-fork.sh" release
fi
pkill -x ClaudeSwitcher-fork 2>/dev/null || true
launchctl bootout "gui/$(id -u)/local.claude-update-fork" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/local.claude-update-fork.plist"
sudo /bin/bash <<'ROOT'
set -e
for label in local.claude-bodyguard-fork local.tz-watchdog-fork; do
    launchctl bootout "system/$label" 2>/dev/null || true
    rm -f "/Library/LaunchDaemons/$label.plist"
done
rm -rf '/Library/Application Support/claude-switcher-fork'
ROOT
rm -f "$HOME/.local/bin/claude-switch-fork.sh" "$HOME/.local/bin/claude-switch-fork.sh.bak"
rm -rf "$HOME/Desktop/Claude切换器-fork.app" "$HOME/Desktop/Claude切换器-fork.app.bak"
echo "已卸载 fork 版程序和服务。原版安装保持原位。"
echo "fork 账号/缓存保留在 ~/.claude-switcher-fork/;个人版活动现场保留在 ~/.claude-switcher-sessions/parked/fork/。"
echo "默认活动目录已交还原版;公共协调器及原版入口保留。"
echo "代理规则、时区与 IPv6 设置影响整个系统,卸载不会自动改回。"
