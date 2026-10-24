#!/bin/bash
# 仅协调共享目录;上游核心保持原样,其自更新写入独立后端位置。
set -euo pipefail
exec "${CLAUDE_SWITCHER_COORDINATOR_PYTHON:-python3}" \
    "$HOME/.claude-switcher-sessions/session-coordinator.py" run upstream \
    "$HOME/.local/share/claude-switcher-upstream/claude-switch.sh" "$@"
