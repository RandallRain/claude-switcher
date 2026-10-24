#!/bin/bash
# 个人版独立安装:不覆盖原版脚本、账号库、服务或桌面入口。
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
H="$HOME"
CORE="$H/.local/bin/claude-switch-fork.sh"
BASE="$H/.claude-switcher-fork"
APP="$H/Desktop/Claude切换器-fork.app"
SYSTEM_DIR="/Library/Application Support/claude-switcher-fork"
echo "Claude 账号切换器-fork · 安装程序"
[ "$(uname -s)" = Darwin ] || { echo "需要 macOS"; exit 1; }
[ -d /Applications/Claude.app ] || { echo "请先安装 /Applications/Claude.app"; exit 1; }
python3 --version >/dev/null
command -v swiftc >/dev/null || { echo "请先安装 Xcode 命令行工具"; exit 1; }

# 安装前核验出口,只保留国家代码,不记录真实地址。
CTRY=$(curl -fsS --max-time 20 https://www.cloudflare.com/cdn-cgi/trace | awk -F= '$1=="loc" {print $2}')
case "$CTRY" in ''|CN) echo "出口国家未取得或为 CN,请先配置代理"; exit 1;; esac
[[ "$CTRY" =~ ^[A-Z][A-Z]$ ]] || { echo "出口国家格式异常"; exit 1; }

# 升级沿用个人版配置;首次只读取旧安装的五项非账号配置。
CONFIG="$CORE"
[ -f "$CONFIG" ] || CONFIG="$H/.local/bin/claude-switch.sh"
get_cfg() { if [ -f "$CONFIG" ]; then sed -n "s/^$1=\"\([^\"]*\)\".*/\1/p" "$CONFIG" | head -1; fi; }
CLASH_DIR="$H/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
MODE=$(get_cfg PROXY_MODE)
if [ -z "$MODE" ]; then
    if [ -d "$CLASH_DIR" ]; then MODE=clash-verge
    elif scutil --nc list | grep -q com.liguangming.Shadowrocket; then MODE=shadowrocket
    else read -r -p "代理客户端(clash-verge/shadowrocket/sing-box/clashx/surge/auto): " MODE; fi
fi
case "$MODE" in clash-verge|shadowrocket|sing-box|clashx|surge|auto) ;; *) echo "不支持的代理客户端"; exit 1;; esac
GRP="${CLAUDE_SWITCHER_PROXY_GROUP:-$(get_cfg PROXY_GROUP)}"
if [ "$MODE" = clash-verge ] && [ -z "$GRP" ]; then
    GRPS=()
    while IFS= read -r g; do GRPS+=("$g"); done < <(awk '
        /^proxy-groups:/ {f=1; next}
        f && /^[a-zA-Z]/ {f=0}
        f && /- name:/ { n=$0; sub(/.*- name: */, "", n); gsub(/"/, "", n) }
        f && /type: *select/ { print n }
    ' "$CLASH_DIR/clash-verge.yaml")
    if [ "${#GRPS[@]}" -eq 1 ]; then GRP="${GRPS[0]}"
    else
        printf '可用代理组:\n'; printf '  %s\n' "${GRPS[@]}"
        read -r -p "请输入 Claude 代理组名称: " GRP
    fi
    [ -n "$GRP" ] || { echo "代理组不能为空"; exit 1; }
fi
OLD_COUNTRY=$(get_cfg COUNTRY_TARGET)
if [ -n "$OLD_COUNTRY" ] && [ "$OLD_COUNTRY" != "$CTRY" ]; then
    echo "出口国家 $CTRY 与现有目标 $OLD_COUNTRY 不一致,请先用现有版本 set-region"; exit 1
fi
TZV=$(get_cfg TZ_TARGET)
LOCALE=$(get_cfg LOCALE_TARGET)
if [ -z "$TZV" ] || [ -z "$LOCALE" ]; then
    REGION=$(curl -fsS --max-time 20 https://ipinfo.io/json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("country","")); print(d.get("timezone",""))')
    [ "$(printf '%s\n' "$REGION" | head -1)" = "$CTRY" ] || { echo "出口国家交叉核验失败"; exit 1; }
    TZV=$(printf '%s\n' "$REGION" | tail -1)
    case "$CTRY" in
        US) LOCALE=en_US;; JP) LOCALE=ja_JP;; TW) LOCALE=zh_TW;; HK) LOCALE=zh_HK;;
        SG) LOCALE=en_SG;; GB) LOCALE=en_GB;; DE) LOCALE=de_DE;; FR) LOCALE=fr_FR;;
        KR) LOCALE=ko_KR;; AU) LOCALE=en_AU;; CA) LOCALE=en_CA;; *) LOCALE=en_US;;
    esac
fi
[ -n "$TZV" ] || { echo "未取得目标时区"; exit 1; }
echo "配置: $MODE / $CTRY / $TZV / $LOCALE"

# 先编译及物化所有文件;编译失败不会覆盖现有 App/核心。
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
bash "$REPO/scripts/build-macos-app.sh" "$REPO" "$STAGE/Claude切换器-fork.app" fork
python3 - "$REPO" "$STAGE" "$H" "$MODE" "$GRP" "$CTRY" "$TZV" "$LOCALE" "$(id -u)" <<'PY'
import pathlib,re,sys
repo,stage=map(pathlib.Path,sys.argv[1:3]); home,mode,group,country,tz,locale,uid=sys.argv[3:]
values=dict(PROXY_MODE=mode,PROXY_GROUP=group,COUNTRY_TARGET=country,TZ_TARGET=tz,LOCALE_TARGET=locale)
text=(repo/'core/claude-switch.sh').read_text()
for key,value in values.items():
    if any(c in value for c in '\n\r"`$\\'): raise SystemExit('配置含不支持的 shell 字符: '+key)
    text=re.sub(r'^'+key+r'="[^"]*"',lambda _:key+'="'+value+'"',text,flags=re.M)
(stage/'claude-switch-fork.sh').write_text(text)
for name in ('claude-bodyguard.sh','tz-watchdog.sh','local.claude-update.plist'):
    text=(repo/'daemons'/name).read_text().replace('__HOME__',home).replace('__PROXY_MODE__',mode).replace('__TZ_TARGET__',tz).replace('__USER_UID__',uid)
    (stage/name).write_text(text)
PY
if [ -n "${DRYRUN:-}" ]; then echo "演练完成,未修改安装态"; exit 0; fi

# 系统级设置无法分成两份;两套看门狗目标冲突时先中止安装。
OTHER_WD='/Library/Application Support/claude-switcher/tz-watchdog.sh'
if launchctl print system/local.tz-watchdog >/dev/null 2>&1 && [ -f "$OTHER_WD" ]; then
    OTHER_TZ=$(sed -n 's/^TARGET=//p' "$OTHER_WD" | head -1)
    OTHER_TZ=${OTHER_TZ#\"}; OTHER_TZ=${OTHER_TZ%\"}
    if [ -n "$OTHER_TZ" ] && [ "$OTHER_TZ" != "$TZV" ]; then echo "两版时区看门狗目标冲突,请先统一目标"; exit 1; fi
fi
cat > "$STAGE/install-system.sh" <<'ROOT'
#!/bin/bash
set -euo pipefail
stage=$1; repo=$2; target=$3
install -d -o root -g wheel -m 755 '/Library/Application Support/claude-switcher-fork'
for name in claude-bodyguard.sh tz-watchdog.sh; do
    install -o root -g wheel -m 755 "$stage/$name" "/Library/Application Support/claude-switcher-fork/$name"
done
for label in local.claude-bodyguard local.tz-watchdog; do
    install -o root -g wheel -m 644 "$repo/daemons/$label.plist" "/Library/LaunchDaemons/$label-fork.plist"
    launchctl bootout "system/$label-fork" 2>/dev/null || true
    launchctl bootstrap system "/Library/LaunchDaemons/$label-fork.plist"
done
case "$(readlink /etc/localtime)" in */"$target") ;; *) /usr/sbin/systemsetup -settimezone "$target";; esac
if ! networksetup -getinfo Wi-Fi | grep -qi '^IPv6: *Off'; then networksetup -setv6off Wi-Fi; fi
ROOT
# 管理员授权只执行已物化的安装步骤;日常与自动更新均为用户态。
if sudo -n true 2>/dev/null; then
    sudo /bin/bash "$STAGE/install-system.sh" "$STAGE" "$REPO" "$TZV"
else
    osascript - "$STAGE/install-system.sh" "$STAGE" "$REPO" "$TZV" <<'AS'
on run argv
    set cmd to "/bin/bash "
    repeat with arg in argv
        set cmd to cmd & quoted form of (arg as text) & " "
    end repeat
    do shell script cmd with prompt "安装 fork 版独立后台服务" with administrator privileges
end run
AS
fi
echo "fork 独立系统服务已安装"

mkdir -p "$H/.local/bin" "$BASE" "$H/Desktop" "$H/Library/LaunchAgents" "$H/Library/Logs"
pkill -x ClaudeSwitcher-fork 2>/dev/null || true
[ ! -f "$CORE" ] || cp -p "$CORE" "$CORE.bak"
install -m 755 "$STAGE/claude-switch-fork.sh" "$CORE"
for helper in stun-probe.py clash-rules.py clash-global-rules.js; do
    install -m 600 "$REPO/core/$helper" "$BASE/$helper"
done
if [ -d "$APP" ]; then
    rm -rf "$APP.bak"
    mv "$APP" "$APP.bak"
fi
mv "$STAGE/Claude切换器-fork.app" "$APP"
install -m 600 "$STAGE/local.claude-update.plist" "$H/Library/LaunchAgents/local.claude-update-fork.plist"
launchctl bootout "gui/$(id -u)/local.claude-update-fork" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$H/Library/LaunchAgents/local.claude-update-fork.plist"
echo "fork App、核心与每12小时更新任务已安装"

# TUN/代理规则是系统共享资源;已生效时只读核验,不重启代理。
if [ "$MODE" = clash-verge ]; then
    if ! python3 "$BASE/clash-rules.py" check --directory "$CLASH_DIR" --group "$GRP" >/dev/null 2>&1; then
        "$CORE" write-global-rules
        # 原版 Claude 活跃时 write-global-rules 会拒绝,不会中断原版会话。
        if pgrep -f '/Applications/Claude.app/Contents/MacOS/Claude' >/dev/null; then
            echo "请退出 Claude 后运行 fork 一键修复以重载规则"; exit 1
        fi
        osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1 || true
        sleep 2
        open -a 'Clash Verge'
        sleep 3
    fi
else
    echo "请在代理客户端中添加 README 所列域名、STUN 端口与参考出口规则"
fi

# 完整详情仅写私有缓存;安装输出只显示状态,不打印地址。
DETAILS=$("$CORE" checkdetail)
printf '%s\n' "$DETAILS" | awk -F= '/^[^#=]+=(ok|warn|fail)(:|$)/ {split($2,a,":"); print $1 "=" a[1]}'
if [ "$(printf '%s\n' "$DETAILS" | grep -Ec '^[^#=]+=ok(:|$)')" -ne 14 ]; then
    echo "fork 已安装,部分安检为警告或失败,请打开桌面 App 查看详情"; exit 1
fi
echo "安装完成: 双击桌面 Claude切换器-fork;新环境需要独立登录"
