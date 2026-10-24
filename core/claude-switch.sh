#!/bin/bash
# Claude 账号切换器-fork - 核心脚本
# 用法: claude-switch-fork.sh check | status | switch A|B | checkdetail | fix | set-region | update [--auto] | diagnose-proxy
set -u
export LC_ALL=en_US.UTF-8
# 检测输出含用于本机 UI 的地址,缓存和历史记录只允许当前用户读取。
umask 077

# 随现有单脚本更新分发;在任何账号状态读写、审计/修复/启动之前分派。
if [ "${1:-}" = "diagnose-proxy" ]; then
    shift
    exec "${CLAUDE_SWITCHER_PYTHON:-python3}" - "$@" <<'PY_PROXY_DIAGNOSE'
"""Read-only transport diagnostics for an explicitly selected HTTP/SOCKS proxy."""
import argparse
import concurrent.futures
import ipaddress
import json
import math
import os
import subprocess
from urllib.parse import urlsplit


TARGETS = (
    ("anthropic_api", "https://api.anthropic.com/v1/models"),
    ("claude_web", "https://claude.ai/"),
)


def select_proxy(args, environ):
    if args.proxy is not None:
        value, source = args.proxy, "argument"
    elif args.config is not None:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("invalid config")
        value, source = config.get("proxy_url"), "config"
    else:
        value, source = environ.get("CLAUDE_SWITCHER_PROXY_URL"), "environment"
    if not isinstance(value, str) or not value.strip():
        raise ValueError("proxy required")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid proxy")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https", "socks5", "socks5h")
            or not parsed.hostname or parsed.port is None
            or not 0 < parsed.port < 65536
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("invalid proxy")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    # Do not include proxy addresses, credentials, or private config paths in reports.
    public = {"scheme": parsed.scheme, "port": parsed.port,
              "host_scope": "loopback" if loopback else "remote",
              "authentication_configured": parsed.username is not None,
              "source": source}
    return value, public


def probe(name, url, proxy, environ):
    # -q ignores curlrc. Pass proxy credentials over stdin, never in argv/reports.
    quoted = proxy.replace("\\", "\\\\").replace('"', '\\"')
    config = 'proxy = "' + quoted + '"\n'
    args = ["curl", "-q", "--config", "-", "-sS", "-o", os.devnull,
            "--proto", "=https", "--noproxy", "", "--connect-timeout", "6",
            "--max-time", "12", "-w",
            "%{http_code}\t%{http_connect}\t%{ssl_verify_result}\t%{time_appconnect}\t%{time_total}",
            url]
    keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "SYSTEMROOT"}
    clean_env = {k: v for k, v in environ.items() if k in keep}
    result = {"name": name, "url": url, "transport_reached": False,
              "access": "unknown"}
    try:
        process = subprocess.run(args, input=config, env=clean_env, capture_output=True,
                                 text=True, timeout=15)
        fields = process.stdout.strip().split()
        status, connect, verify = map(int, fields[:3])
        tls, elapsed = map(float, fields[3:5])
        if (not 0 <= status <= 599 or not 0 <= connect <= 599 or verify < 0
                or not math.isfinite(tls) or not math.isfinite(elapsed)
                or tls < 0 or elapsed < 0):
            raise ValueError("invalid probe metrics")
        reached = process.returncode == 0 and status > 0 and verify == 0 and tls > 0
        access = ("unauthenticated" if status == 401 else
                  "access_restricted" if status in (403, 407) else
                  "server_error" if status >= 500 else "http_response")
        result.update(curl_exit=process.returncode, http_status=status,
                      proxy_connect_status=connect, tls_verified=reached,
                      elapsed_seconds=elapsed, transport_reached=reached,
                      access=access if reached else "unknown")
    except subprocess.TimeoutExpired:
        result["error"] = "probe_timeout"
    except OSError:
        result["error"] = "curl_unavailable"
    except (ValueError, IndexError):
        result["error"] = "invalid_probe_output"
    return result


class DiagnosticParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo private URLs or paths from argv.
        raise ValueError("invalid arguments")


def main(argv=None, environ=None):
    parser = DiagnosticParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--proxy", help="explicit proxy URL including a port")
    choice.add_argument("--config", help="private JSON file containing proxy_url")
    environ = os.environ if environ is None else environ
    report = {"schema_version": 1, "mode": "proxy_diagnostics",
              "scope": "unauthenticated_transport_only",
              "account_status": "not_evaluated", "leak_protection": "not_evaluated",
              "launch_allowed": None, "application_started": False,
              "system_modified": False}
    try:
        args = parser.parse_args(argv)
        proxy, public = select_proxy(args, environ)
    except (OSError, ValueError, TypeError):
        report.update(state="configuration_error", error="invalid_or_missing_proxy",
                      hint="Use --proxy, --config, or CLAUDE_SWITCHER_PROXY_URL; an explicit port is required.")
        print(json.dumps(report, ensure_ascii=False))
        return 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(probe, name, url, proxy, environ) for name, url in TARGETS]
        checks = [f.result() for f in futures]
    reached = all(check["transport_reached"] for check in checks)
    report.update(proxy=public, checks=checks,
                  state="transport_reached" if reached else "transport_failed")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if reached else 1


if __name__ == "__main__":
    raise SystemExit(main())
PY_PROXY_DIAGNOSE
fi

# ============ 安装时可配置项(安装程序会自动改写) ============
PROXY_MODE="clash-verge"       # 代理客户端: clash-verge|shadowrocket|sing-box|clashx|surge|auto
PROXY_GROUP="IpEqual"          # Clash 代理组名(安装时自动探测)
COUNTRY_TARGET="US"            # 目标出口国家(安装时按出口自动识别)
TZ_TARGET="America/New_York"   # 锁定时区,应与出口IP所在城市一致
LOCALE_TARGET="en_US"          # 锁定地区格式
# ============================================================

EDITION="fork"
UPDATE_REPO="RandallRain/claude-switcher"
UPDATE_BRANCH="main-fork"
DATA="$HOME/Library/Application Support/Claude-fork"
# 个人版的活动数据、账号存档、探针、缓存和更新均独立。
BASE="$HOME/.claude-switcher-fork"
STATE_BASE="$BASE"
LOG="$HOME/Library/Logs/claude-switch-fork.log"
CLASH_DIR="$HOME/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
ROUTE_MISSED=""
HELPER_DIR="$BASE"
[ -f "$(dirname "$0")/stun-probe.py" ] && HELPER_DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$STATE_BASE/accounts" "$BASE" "$HOME/Library/Logs"

# fork 内部串行修改;原版 CLI 不识别此锁,系统设置另做冲突检查。
acquire_operation_lock() {
    OP_LOCK="$BASE/operation.lock"
    if ! mkdir "$OP_LOCK" 2>/dev/null; then
        echo "FAIL:fork 版另一个操作正在执行;如上次异常退出,确认无操作后删除 $OP_LOCK"
        return 1
    fi
    trap 'rmdir "$OP_LOCK" 2>/dev/null' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

# fork 包含带专用目录的辅助进程,保证交换数据前完全退出;原版只识别主进程。
claude_pids() {
    ps -ww -U "$(id -u)" -o pid=,command= | awk -v app='/Applications/Claude.app/Contents/MacOS/Claude' \
        -v root='/Applications/Claude.app/Contents/' \
        -v flag="--user-data-dir=$DATA" -v edition="${1:-fork}" '
        {pid=$1; sub(/^[ \t]*[0-9]+[ \t]+/, "")}
        index($0,root)==1 {
            pos=index($0,flag); nextchar=substr($0,pos+length(flag),1)
            own=pos>0 && (nextchar=="" || nextchar==" ")
            if ((edition=="fork" && own) || (edition=="upstream" && !own && index($0,app)==1 && index($0,"--type=")==0)) print pid
        }'
}

require_exclusive_system() {
    if [ -n "$(claude_pids upstream)" ] || pgrep -x ClaudeSwitcher >/dev/null 2>&1; then
        echo "FAIL:系统设置为 macOS 共用,请先退出原版切换器及原版 Claude"
        return 1
    fi
}

region_compatible() {
    local other='/Library/Application Support/claude-switcher/tz-watchdog.sh' value=""
    if launchctl print system/local.tz-watchdog >/dev/null 2>&1 && [ -f "$other" ]; then
        value=$(sed -n 's/^TARGET=//p' "$other" | head -1)
        value=${value#\"}; value=${value%\"}
        if [ -n "$value" ] && [ "$value" != "$1" ]; then
            echo "FAIL:原版看门狗目标为 $value,与 fork 的 $1 冲突;请先统一目标或停用原版看门狗"
            return 1
        fi
    fi
}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >>"$LOG"; }

# TUN 在线判定:存在 fake-ip 网段地址即视为 TUN 已接管(兼容各种 TUN 设备名)
tun_up() { ifconfig 2>/dev/null | grep -q 'inet 198\.18\.'; }

# 代理客户端显示名(供检查细节输出)
proxy_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        shadowrocket) echo "Shadowrocket" ;;
        sing-box)     echo "sing-box" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        auto)         echo "代理客户端" ;;
        *)            echo "$PROXY_MODE" ;;
    esac
}

# 可 open -a 启动的客户端 App 名(sing-box/auto 无标准 App,返回空)
proxy_app_name() {
    case "$PROXY_MODE" in
        clash-verge)  echo "Clash Verge" ;;
        shadowrocket) echo "Shadowrocket" ;;
        clashx)       echo "ClashX" ;;
        surge)        echo "Surge" ;;
        *)            echo "" ;;
    esac
}

# Shadowrocket 在 Mac 上表现为 VPN 配置;launchd/非图形环境下 scutil 可能查不到该行,此时以 TUN 判定为准(避免误杀)
shadowrocket_up() {
    nc=$(scutil --nc list 2>/dev/null)
    if echo "$nc" | grep -q 'com.liguangming.Shadowrocket'; then
        echo "$nc" | grep 'com.liguangming.Shadowrocket' | grep -q '(Connected)'
    else
        tun_up
    fi
}

# 代理内核在线判定:按安装时识别的客户端分派;auto 时任一命中即活
proxy_alive() {
    case "$PROXY_MODE" in
        clash-verge)  pgrep -f verge-mihomo >/dev/null 2>&1 ;;
        shadowrocket) shadowrocket_up ;;
        sing-box)     pgrep -f sing-box >/dev/null 2>&1 ;;
        clashx)       pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 ;;
        surge)        pgrep -f Surge >/dev/null 2>&1 ;;
        auto)         pgrep -f verge-mihomo >/dev/null 2>&1 || shadowrocket_up \
                      || pgrep -f sing-box >/dev/null 2>&1 \
                      || pgrep -x ClashX >/dev/null 2>&1 || pgrep -f clash >/dev/null 2>&1 \
                      || pgrep -f Surge >/dev/null 2>&1 ;;
        *)            pgrep -f verge-mihomo >/dev/null 2>&1 ;;
    esac
}

# 全局脚本和内核中的完整置顶规则必须同时通过(不能 grep 旧 Merge.yaml)。
global_rules_ok() {
    python3 "$HELPER_DIR/clash-rules.py" check --directory "$CLASH_DIR" --group "$PROXY_GROUP" >/dev/null 2>&1
}

# 置顶规则行为检测:三个生态域名全部解析到 fake-ip 段(198.18.x.x)即视为已置顶;未命中域名记入 ROUTE_MISSED
route_ok() {
    ROUTE_MISSED=""
    local d a
    for d in claude.ai api.anthropic.com claudeusercontent.com; do
        a=$(dig +short +time=3 +tries=1 "$d" 2>/dev/null | head -1)
        case "$a" in
            198.18.*) ;;
            *) ROUTE_MISSED="$ROUTE_MISSED $d" ;;
        esac
    done
    [ -z "$ROUTE_MISSED" ]
}

# 国家代码 → 地区格式映射(未收录的国家回退 en_US)
locale_for() {
    case "$1" in
        US) echo en_US ;; JP) echo ja_JP ;; TW) echo zh_TW ;; HK) echo zh_HK ;;
        SG) echo en_SG ;; GB) echo en_GB ;; DE) echo de_DE ;; FR) echo fr_FR ;;
        KR) echo ko_KR ;; AU) echo en_AU ;; CA) echo en_CA ;; *) echo en_US ;;
    esac
}

# IP 审查:记录出口IP并统计最近48h内不同IP个数(账号画像风险指标)
iphist_count() {
    [ -z "$1" ] && { echo 0; return; }
    local HIST="$BASE/iphistory.log"
    local now=$(date +%s)
    [ ! -f "$HIST" ] || chmod 600 "$HIST"
    echo "$1 $now" >>"$HIST"
    # 裁掉48小时(172800秒)以前的记录
    awk -v cutoff=$((now-172800)) '$2>=cutoff' "$HIST" > "$HIST.tmp" && mv "$HIST.tmp" "$HIST"
    # 统计48h内不同IP个数
    awk '{print $1}' "$HIST" | sort -u | wc -l | tr -d ' '
}

# ---------- 安全检查 ----------
check() {
    fails=""

    proxy_alive || fails="$fails;代理内核未运行"
    tun_up || fails="$fails;TUN网卡不在线"

    load_network_snapshot
    case "$egress_result" in ok:*) ;; *) fails="$fails;双栈出口未获取或国家异常(期望$COUNTRY_TARGET)" ;; esac
    ipwarn=""
    lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
    if [ -n "$ip" ] && [ -n "$lastip" ] && [ "$ip" != "$lastip" ]; then
        ipwarn="注意:出口IP与上次不同($lastip -> $ip)"
    fi
    if [ -n "$ip" ]; then
        [ ! -f "$BASE/lastip" ] || chmod 600 "$BASE/lastip"
        echo "$ip" > "$BASE/lastip"
    fi

    # IP 审查:48h 内出口 IP 变化 ≥3 次追加警告(只 warn 不 fail)
    ipn=$(iphist_count "$ip")
    if [ "$ipn" -ge 3 ] 2>/dev/null; then
        ipwarn="${ipwarn:+$ipwarn; }注意:48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
    fi

    tz=$(actual_timezone)
    [ "$tz" = "$TZ_TARGET" ] || fails="$fails;时区异常($tz)"

    locale=$(defaults read -g AppleLocale 2>/dev/null)
    [ "$locale" = "$LOCALE_TARGET" ] || fails="$fails;地区异常($locale)"

    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    echo "$v6" | grep -qi 'off' || fails="$fails;IPv6未关闭"

    dnsip=$(dig +short +time=3 +tries=1 claude.ai 2>/dev/null | head -1)
    case "$dnsip" in
        198.18.*) ;;
        *) fails="$fails;DNS未走隧道(返回${dnsip:-空})" ;;
    esac

    case "$wr" in
        ok:*|warn:*) ;;
        fail:*) fails="$fails;WebRTC:${wr#fail:}" ;;
        *) fails="$fails;WebRTC检测异常(请更新探针)" ;;
    esac

    launchctl print system/local.claude-bodyguard-fork >/dev/null 2>&1 || fails="$fails;保镖未运行"

    # 兼容升级前的规则;WebRTC 仍须核验实际 UDP 代理策略,全局脚本未核验时界面提示修复。
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        global_rules_ok || route_ok || fails="$fails;全局置顶规则未生效"
    else
        route_ok || fails="$fails;置顶规则缺失(未命中:${ROUTE_MISSED# })"
    fi

    defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | grep -q 'en-US' || {
        syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
        [[ "$syslang" == en* ]] || fails="$fails;客户端语言为中文"
    }

    r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
    r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
    { [ "$r1" = "000" ] || [ "$r2" = "000" ]; } && fails="$fails;Anthropic生态不可达($r1/$r2)"

    if [ -n "$fails" ]; then
        echo "FAIL:${fails#;}"
        return 1
    fi
    echo "OK:$ip${ipwarn:+|$ipwarn}"
    return 0
}

# 每轮并发取得默认/IPv4/IPv6出口和STUN结果,三个入口共用同一份快照。
load_network_snapshot() {
    local network_mode="--snapshot" proxy_valid=1 key value network_output
    if [ "$PROXY_MODE" = "clash-verge" ] && \
       ! python3 "$HELPER_DIR/clash-rules.py" proxy-check --directory "$CLASH_DIR" --group "$PROXY_GROUP" >/dev/null 2>&1; then
        network_mode="--egress"; proxy_valid=0
    fi
    ip=""; loc=""; egress_result="fail:IPv4: 未获取 | IPv6: 未获取"
    wr="fail:检测异常(请更新探针)"
    network_output=$(python3 "$HELPER_DIR/stun-probe.py" "$network_mode" --country "$COUNTRY_TARGET" 2>/dev/null)
    while IFS='=' read -r key value; do
        case "$key" in
            primary_ip) ip="$value" ;;
            primary_country) loc="$value" ;;
            egress) egress_result="$value" ;;
            webrtc) wr="$value" ;;
        esac
    done <<< "$network_output"
    [ "$proxy_valid" -eq 1 ] || wr='fail:无法核验Clash的UDP代理策略(可能直连、不支持UDP或规则被覆盖)'
    return 0
}

actual_timezone() {
    local path
    path=$(readlink /etc/localtime 2>/dev/null)
    case "$path" in
        */zoneinfo/*) echo "${path#*/zoneinfo/}" ;;
        *) echo '未知' ;;
    esac
}

# 逐项输出检查结果(供UI使用): 名称=ok|fail:细节
checkdetail() {
    if proxy_alive; then
        case "$PROXY_MODE" in
            shadowrocket) echo '代理内核=ok:Shadowrocket 已连接' ;;
            *) echo "代理内核=ok:$(proxy_name) 运行中" ;;
        esac
    else
        echo "代理内核=fail:$(proxy_name) 未运行"
    fi
    if tun_up; then echo 'TUN网卡=ok'; else echo 'TUN网卡=fail:不在线'; fi
    load_network_snapshot
    echo "出口IP=$egress_result"
    lastip=""; [ -f "$BASE/lastip" ] && lastip=$(cat "$BASE/lastip")
    ipst=""; ipdt=""
    if [ -z "$ip" ]; then echo 'IP一致=fail:未获取到出口IP'
    elif [ -z "$lastip" ]; then ipst="ok"; ipdt="首次记录"
    elif [ "$ip" = "$lastip" ]; then ipst="ok"
    else ipst="warn"; ipdt="$lastip → $ip"; fi
    if [ -n "$ip" ]; then
        [ ! -f "$BASE/lastip" ] || chmod 600 "$BASE/lastip"
        echo "$ip" > "$BASE/lastip"
    fi
    # IP 审查:48h 内出口 IP 变化 ≥3 次提示账号画像风险(只 warn 不 fail)
    if [ -n "$ipst" ]; then
        ipn=$(iphist_count "$ip")
        if [ "$ipn" -ge 3 ] 2>/dev/null; then
            if [ "$ipst" = "ok" ]; then
                ipst="warn"; ipdt="48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            else
                ipdt="$ipdt;48h内出口IP变化${ipn}次,账号画像有风险,建议固定节点"
            fi
        fi
        if [ -z "$ipdt" ]; then echo "IP一致=$ipst"; else echo "IP一致=$ipst:$ipdt"; fi
    fi
    tz=$(actual_timezone)
    if [ "$tz" = "$TZ_TARGET" ]; then echo "时区=ok:$tz"; else echo "时区=fail:$tz (期望 $TZ_TARGET)"; fi
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" = "$LOCALE_TARGET" ]; then echo "地区=ok:$locale"; else echo "地区=fail:${locale:-未获取} (期望 $LOCALE_TARGET)"; fi
    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    if echo "$v6" | grep -qi 'off'; then echo 'IPv6=ok:已关闭'; else echo 'IPv6=fail:未关闭'; fi
    dnsip=$(dig +short +time=3 +tries=1 claude.ai 2>/dev/null | head -1)
    case "$dnsip" in
        198.18.*) echo 'DNS=ok:走隧道' ;;
        *) echo "DNS=fail:未走隧道(${dnsip:-空})" ;;
    esac
    case "$wr" in
        ok:*|warn:*|fail:*) echo "WebRTC=$wr" ;;
        *) echo 'WebRTC=fail:检测异常(请更新探针)' ;;
    esac
    if launchctl print system/local.claude-bodyguard-fork >/dev/null 2>&1; then
        lastkill=$(tail -1 "$HOME/Library/Logs/claude-bodyguard-fork.log" 2>/dev/null | cut -c1-40)
        echo "保镖=ok:${lastkill:-待命}"
    else
        echo '保镖=fail:未安装'
    fi
    # 全局脚本与实际规则通过才显示绿色;旧配置仅域名行为通过时提示黄色迁移。
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if global_rules_ok; then
            echo '规则置顶=ok:全局脚本及内核规则已核验'
        elif route_ok; then
            echo '规则置顶=warn:域名行为通过,全局脚本未核验,请运行一键修复'
        else
            echo '规则置顶=fail:未生效'
        fi
    else
        if route_ok; then
            echo '规则置顶=ok:行为检测通过'
        else
            echo "规则置顶=fail:未命中(${ROUTE_MISSED# }),请在 $(proxy_name) 中自行加置顶规则"
        fi
    fi
    # 12. Claude 客户端语言(单独设为英文,或系统首选语言为英文均可;防止 JS 层读出中文)
    clang=$(defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | head -2)
    syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
    if echo "$clang" | grep -q 'en-US'; then
        echo '客户端语言=ok:en-US'
    elif [[ "$syslang" == en* ]]; then
        echo '客户端语言=ok:跟随系统(英文)'
    else
        echo '客户端语言=fail:跟随系统(中文)'
    fi
    # 13. ASN 网络属性(住宅宽带 vs 机房,按目标地区收录主流运营商)
    org=$(curl -s --max-time 6 https://ipinfo.io/json 2>/dev/null | grep '"org"' | cut -d'"' -f4)
    case "$org" in
        '') echo 'ASN属性=warn:未获取' ;;
        *Comcast*|*AT\&T*|*Verizon*|*Spectrum*|*Charter*|*Cox*|*T-Mobile*|*Cable*|*esidential*|\
        *NTT*|*KDDI*|*SoftBank*|*IIJ*|*OCN*|*So-net*|\
        *HiNet*|*Chunghwa*|*CHT*|*kbro*|*Taiwan*|*FarEasTone*|*Data\ Communication*|\
        *Singtel*|*StarHub*|*M1*|\
        *HKT*|*PCCW*|*HKBN*|*HGC*|\
        *BT*|*Sky*|*Virgin*|*TalkTalk*|*Vodafone*|\
        *Deutsche\ Telekom*|*1\&1*|*O2*) echo "ASN属性=ok:$org" ;;
        *) echo "ASN属性=warn:疑似机房($org)" ;;
    esac
    # 14. Anthropic 生态可达性
    r1=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://claude.ai/ 2>/dev/null)
    r2=$(curl -s --max-time 8 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)
    if [ "$r1" != "000" ] && [ "$r2" != "000" ]; then
        echo "生态可达性=ok:claude.ai:$r1 api:$r2"
    else
        echo "生态可达性=fail:claude.ai:$r1 api:$r2"
    fi
}

# 全局扩展脚本覆盖所有订阅;保留已有 main,按订阅动态选择代理组。
write_global_rules() {
    python3 "$HELPER_DIR/clash-rules.py" install --directory "$CLASH_DIR" --group "$PROXY_GROUP"
}

# 一键修复:按项自动修复,每步输出结果
fix() {
    echo "FIX:开始一键修复"
    region_compatible "$TZ_TARGET" || return 1
    # 修复会重载代理;先完全退出 Claude,避免重载期间重新直连。
    quit_claude || { echo "FIX:Claude 未能完全退出,修复已中止"; return 1; }
    fix_failed=0

    # A. 代理内核 + TUN
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! pgrep -f verge-mihomo >/dev/null 2>&1; then
            echo "FIX:正在启动 Clash Verge..."
            open -a "Clash Verge"
        fi
        for i in $(seq 1 40); do
            pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up && break
            sleep 1
        done
        if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            V="$CLASH_DIR/verge.yaml"
            if [ -f "$V" ] && grep -q '^enable_tun_mode: false' "$V"; then
                echo "FIX:检测到 TUN 开关被关闭,正在恢复..."
                osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
                sleep 3
                sed -i '' 's/^enable_tun_mode: false/enable_tun_mode: true/' "$V"
                open -a "Clash Verge"
                for i in $(seq 1 40); do
                    tun_up && break
                    sleep 1
                done
            fi
            if pgrep -f verge-mihomo >/dev/null 2>&1 && tun_up; then
                echo "FIX:代理内核+TUN ✓(已恢复TUN开关)"
            else
                echo "FIX:代理内核/TUN ✗(请打开 Clash Verge 检查节点/线路)"
            fi
        fi
    else
        app=$(proxy_app_name)
        if ! proxy_alive && [ -n "$app" ]; then
            echo "FIX:正在启动 $app..."
            open -a "$app" >/dev/null 2>&1
        fi
        for i in $(seq 1 40); do
            proxy_alive && tun_up && break
            sleep 1
        done
        if proxy_alive && tun_up; then
            echo "FIX:代理内核+TUN ✓"
        else
            echo "FIX:代理内核/TUN ✗(请打开 $(proxy_name) 手动检查节点/线路及TUN模式)"
        fi
    fi

    # B. 时区/IPv6/DNS缓存/保镖(需管理员权限,会弹一次密码框)
    tz=$(actual_timezone)
    v6=$(networksetup -getinfo Wi-Fi 2>/dev/null | grep -i '^IPv6:')
    adminCmds=""
    [ "$tz" != "$TZ_TARGET" ] && adminCmds="${adminCmds}/usr/sbin/systemsetup -settimezone $TZ_TARGET; "
    echo "$v6" | grep -qi 'off' || adminCmds="${adminCmds}networksetup -setv6off Wi-Fi; "
    if ! launchctl print system/local.claude-bodyguard-fork >/dev/null 2>&1; then
        # 系统守护脚本由安装器以 root 所有权安装,日常修复只重新注册现有系统 plist。
        adminCmds="${adminCmds}launchctl bootstrap system /Library/LaunchDaemons/local.claude-bodyguard-fork.plist; "
    fi
    if [ -n "$adminCmds" ]; then
        adminCmds="${adminCmds}dscacheutil -flushcache; killall -HUP mDNSResponder"
        echo "FIX:修复时区/IPv6/DNS/保镖(需要密码)..."
        if osascript <<ASEOF >/dev/null 2>&1
do shell script "$adminCmds" with prompt "一键修复需要管理员权限" with administrator privileges
ASEOF
        then
            echo "FIX:时区/IPv6/DNS/保镖 ✓"
        else
            echo "FIX:时区/IPv6/保镖 ✗(取消或失败)"
        fi
    else
        echo "FIX:时区/IPv6/保镖 无需修复 ✓"
    fi

    # B3. Claude 客户端单独设英文(用户级,无需密码;系统首选语言已是英文则跳过)
    syslang=$(defaults read -g AppleLanguages 2>/dev/null | sed -n '2p' | tr -d ' ,"')
    if ! defaults read com.anthropic.claudefordesktop AppleLanguages 2>/dev/null | grep -q 'en-US' && [[ "$syslang" != en* ]]; then
        defaults write com.anthropic.claudefordesktop AppleLanguages -array en-US
        echo "FIX:Claude 客户端语言 → en-US ✓(重启 Claude 后生效)"
    fi

    # B4. 地区格式(用户级,无需密码)
    locale=$(defaults read -g AppleLocale 2>/dev/null)
    if [ "$locale" != "$LOCALE_TARGET" ]; then
        defaults write -g AppleLocale "$LOCALE_TARGET"
        echo "FIX:地区格式 → $LOCALE_TARGET ✓"
    fi

    # B2. Global Extend Script:保留用户逻辑,重载后核验实际内核,不只看源文件。
    if [ "$PROXY_MODE" = "clash-verge" ]; then
        if ! global_rules_ok; then
            echo "FIX:更新 Global Extend Script(保留已有脚本,对所有订阅生效)..."
            if write_global_rules; then
                osascript -e 'tell application "Clash Verge" to quit' >/dev/null 2>&1
                sleep 3
                open -a "Clash Verge"
                for i in $(seq 1 12); do
                    global_rules_ok && tun_up && break
                    sleep 1
                done
                if global_rules_ok; then
                    echo "FIX:全局置顶规则 ✓(三端口代理/阻断后备及双栈参考路由已加载)"
                else
                    echo "FIX:全局置顶规则 ✗(请检查节点UDP支持、代理组及订阅扩展脚本是否覆盖规则)"
                    fix_failed=1
                fi
            else
                echo "FIX:全局脚本写入失败,已有配置保留"
                fix_failed=1
            fi
        else
            echo "FIX:全局置顶规则已生效 ✓"
        fi
    elif ! route_ok; then
        echo "FIX:置顶规则 ✗(未命中:${ROUTE_MISSED# }) — 请在 $(proxy_name) 中添加域名及STUN端口规则"
        fix_failed=1
    fi

    # C. WebRTC:使用与启动/界面相同的双栈判定,不因跨版本比较反复重启。
    load_network_snapshot
    case "$wr" in
        ok:*) echo "FIX:WebRTC ✓ (${wr#ok:})" ;;
        warn:*) echo "FIX:WebRTC ⚠ (${wr#warn:})" ;;
        fail:*) echo "FIX:WebRTC ✗ (${wr#fail:}),请检查节点UDP转发及出口策略"; fix_failed=1 ;;
        *) echo "FIX:WebRTC ✗ 检测异常(请更新探针)"; fix_failed=1 ;;
    esac

    # D. 出口IP最终验证
    if [[ "$egress_result" == ok:* ]]; then
        echo "FIX:双栈出口IP ✓ (已取得的出口国家均为 $COUNTRY_TARGET)"
    else
        echo "FIX:双栈出口IP ✗ (期望$COUNTRY_TARGET) — 请到 $(proxy_name) 里检查线路"
        fix_failed=1
    fi
    echo "FIX:修复流程结束"
    return "$fix_failed"
}

# 地区自适应:按当前出口国家一键重设 国家/时区/地区格式(换了国家节点后运行)
set_region() {
    trace=$(curl -s --max-time 8 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null)
    loc=$(echo "$trace" | grep '^loc=' | cut -d= -f2)
    [ -z "$loc" ] && { echo "FAIL:无法获取出口国家(cloudflare trace 无结果,请先挂代理)"; return 1; }
    info=$(curl -s --max-time 8 https://ipinfo.io/json 2>/dev/null)
    ctry=$(echo "$info" | grep '"country"' | cut -d'"' -f4)
    ntz=$(echo "$info" | grep '"timezone"' | cut -d'"' -f4)
    if [ -z "$ctry" ] || [ -z "$ntz" ]; then
        echo "FAIL:ipinfo 未返回国家或时区"; return 1
    fi
    if [ "$loc" != "$ctry" ]; then
        echo "FAIL:出口国家不一致(cloudflare=$loc ipinfo=$ctry),已中止"; return 1
    fi
    region_compatible "$ntz" || return 1
    nlocale=$(locale_for "$ctry")

    # 改写脚本自身头部三个配置项
    sed -i '' \
        -e "s|^COUNTRY_TARGET=\".*\"|COUNTRY_TARGET=\"$ctry\"|" \
        -e "s|^TZ_TARGET=\".*\"|TZ_TARGET=\"$ntz\"|" \
        -e "s|^LOCALE_TARGET=\".*\"|LOCALE_TARGET=\"$nlocale\"|" \
        "$0"
    COUNTRY_TARGET="$ctry"; TZ_TARGET="$ntz"; LOCALE_TARGET="$nlocale"

    # 地区格式(用户级,无需密码)
    defaults write -g AppleLocale "$nlocale"

    # 时区(需管理员权限,会弹一次授权框);看门狗脚本在 root 目录下,顺道用同一次授权同步目标值
    WD="/Library/Application Support/claude-switcher-fork/tz-watchdog.sh"
    adminCmds="/usr/sbin/systemsetup -settimezone $ntz"
    wdsync=0
    if [ -f "$WD" ] && [ ! -w "$WD" ]; then
        adminCmds="$adminCmds; /usr/bin/sed -i '' 's|^TARGET=.*|TARGET=$ntz|' '$WD'"
        wdsync=1
    fi
    if ! osascript <<ASEOF >/dev/null 2>&1
do shell script "$adminCmds" with prompt "set-region 需要管理员权限以锁定时区" with administrator privileges
ASEOF
    then
        echo "FAIL:时区设置被取消或失败(国家/地区格式已更新为 $ctry/$nlocale)"
        return 1
    fi
    # 看门狗目标同步兜底(可写的老版本安装直接 sed;失败则提示运行安装器刷新)
    if [ -f "$WD" ] && [ "$wdsync" -eq 0 ]; then
        sed -i '' "s|^TARGET=.*|TARGET=$ntz|" "$WD" 2>/dev/null || echo "WARN:看门狗目标未同步,请运行安装器刷新"
    fi

    echo "OK:region=$loc tz=$ntz locale=$nlocale"
}

# 自动更新:只跟踪个人仓库 main-fork,不写入原版程序/辅助文件。
self_update() {
    if [ "${1:-}" = "--auto" ]; then auto=1; else auto=0; fi
    # 路径全部从脚本自身位置推导,兼容旧版 LaunchDaemon(root) 混跑时 $HOME 不可靠的场景
    BIN=$(cd "$(dirname "$0")" && pwd)
    UHOME="${BIN%/.local/bin}"
    [ "$UHOME" = "$BIN" ] && UHOME="$HOME"
    if [ "$BIN" != "$UHOME/.local/bin" ] || [ "$(basename "$0")" != claude-switch-fork.sh ]; then
        echo "FAIL:请通过 ~/.local/bin/claude-switch-fork.sh update 更新已安装的 fork 版"
        return 1
    fi
    if [ "$auto" -eq 1 ]; then
        mkdir -p "$BASE"
        exec >>"$BASE/update.log" 2>&1
        echo "# $(date '+%F %T') update --auto 开始"
    fi

    tmpd=$(mktemp -d)
    if ! curl -fsSL --max-time 30 "https://codeload.github.com/$UPDATE_REPO/zip/refs/heads/$UPDATE_BRANCH" -o "$tmpd/main.zip" \
        || ! unzip -q "$tmpd/main.zip" -d "$tmpd" 2>/dev/null; then
        echo "FAIL:下载或解压失败(网络问题,稍后再试)"; rm -rf "$tmpd"; return 1
    fi
    NEW="$tmpd/claude-switcher-$UPDATE_BRANCH"
    if [ ! -f "$NEW/core/claude-switch.sh" ]; then
        echo "FAIL:远程包结构异常,已中止"; rm -rf "$tmpd"; return 1
    fi

    # 工件完整性校验:逐项比对仓库 SHA256SUMS,任一不符即中止(防传输篡改;信任根为 GitHub 仓库本身)
    if ! curl -fsSL --max-time 15 "https://raw.githubusercontent.com/$UPDATE_REPO/$UPDATE_BRANCH/SHA256SUMS" -o "$tmpd/SHA256SUMS" \
        || [ ! -s "$tmpd/SHA256SUMS" ]; then
        echo "FAIL:无法获取校验清单(SHA256SUMS),已中止"; rm -rf "$tmpd"; return 1
    fi
    bad=0
    while read -r sum rel; do
        [ -z "$sum" ] && continue
        case "$rel" in /*|../*|*/../*|*/..) bad=1; break;; esac
        if [ ! -f "$NEW/$rel" ] || [ "$(shasum -a 256 "$NEW/$rel" | awk '{print $1}')" != "$sum" ]; then
            bad=1; break
        fi
    done < "$tmpd/SHA256SUMS"
    if [ "$bad" -eq 1 ]; then
        echo "FAIL:工件校验失败,已中止(不排除网络篡改,可稍后重试或手动 update)"; rm -rf "$tmpd"; return 1
    fi

    # 四个实际部署工件必须全部列入清单;拒绝误发布的原版或旧版包。
    for artifact in core/claude-switch.sh core/stun-probe.py core/clash-rules.py core/clash-global-rules.js; do
        if ! awk -v path="$artifact" '$2 == path && length($1) == 64 {found=1} END {exit !found}' "$tmpd/SHA256SUMS"; then
            echo "FAIL:校验清单缺少必要工件,已中止"; rm -rf "$tmpd"; return 1
        fi
    done
    if ! grep -qx 'EDITION="fork"' "$NEW/core/claude-switch.sh" || \
       ! grep -qx 'UPDATE_REPO="RandallRain/claude-switcher"' "$NEW/core/claude-switch.sh" || \
       ! grep -qx 'UPDATE_BRANCH="main-fork"' "$NEW/core/claude-switch.sh"; then
        echo "FAIL:更新包不是 fork/main-fork 版本,已中止"; rm -rf "$tmpd"; return 1
    fi

    # 跳过判断同时比较辅助工件;探针/全局脚本单独更新也必须分发。
    strip_cfg() { grep -vE '^(PROXY_MODE|PROXY_GROUP|COUNTRY_TARGET|TZ_TARGET|LOCALE_TARGET)="' "$1"; }
    UPDATE_BASE="$UHOME/.claude-switcher-fork"
    CORE_PATH="$BIN/claude-switch-fork.sh"
    helpers_changed=0
    for helper in stun-probe.py clash-rules.py clash-global-rules.js; do
        cmp -s "$UPDATE_BASE/$helper" "$NEW/core/$helper" || helpers_changed=1
    done
    if [ "$helpers_changed" -eq 0 ] && [ -f "$CORE_PATH" ] && \
       diff <(strip_cfg "$CORE_PATH") <(strip_cfg "$NEW/core/claude-switch.sh") >/dev/null 2>&1; then
        echo "OK:已是最新版本"; rm -rf "$tmpd"; return 0
    fi

    # 备份现有核心脚本
    if [ -f "$CORE_PATH" ] && ! cp "$CORE_PATH" "$CORE_PATH.bak"; then
        echo "FAIL:核心脚本备份失败,未执行更新"; rm -rf "$tmpd"; return 1
    fi
    mkdir -p "$UPDATE_BASE" "$tmpd/helper-backups"
    restore_update_helpers() {
        for helper in stun-probe.py clash-rules.py clash-global-rules.js; do
            if [ -f "$tmpd/helper-backups/$helper" ]; then
                cp "$tmpd/helper-backups/$helper" "$UPDATE_BASE/$helper"
            else
                rm -f "$UPDATE_BASE/$helper"
            fi
        done
    }
    for helper in stun-probe.py clash-rules.py clash-global-rules.js; do
        if [ -f "$UPDATE_BASE/$helper" ] && ! cp "$UPDATE_BASE/$helper" "$tmpd/helper-backups/$helper"; then
            echo "FAIL:辅助工件备份失败,未执行更新"; rm -rf "$tmpd"; return 1
        fi
    done
    for helper in stun-probe.py clash-rules.py clash-global-rules.js; do
        if ! cp "$NEW/core/$helper" "$UPDATE_BASE/$helper"; then
            restore_update_helpers
            echo "FAIL:辅助工件更新失败,已回滚"; rm -rf "$tmpd"; return 1
        fi
    done
    if [ "$(id -u)" -eq 0 ]; then
        owner=$(stat -f '%Su' "$UHOME" 2>/dev/null)
        [ -n "$owner" ] && chown "$owner" "$UPDATE_BASE/stun-probe.py" "$UPDATE_BASE/clash-rules.py" "$UPDATE_BASE/clash-global-rules.js"
    fi

    # 替换核心脚本 + 从 .bak 读出5个配置值注入新脚本
    if ! cp "$NEW/core/claude-switch.sh" "$CORE_PATH"; then
        [ -f "$CORE_PATH.bak" ] && cp "$CORE_PATH.bak" "$CORE_PATH"
        restore_update_helpers
        echo "FAIL:核心脚本更新失败,已回滚"; rm -rf "$tmpd"; return 1
    fi
    PM=""; PG=""; CT=""; TZV=""; LCT=""
    if [ -f "$CORE_PATH.bak" ]; then
        get_cfg() { grep "^$1=\"" "$CORE_PATH.bak" | head -1 | cut -d'"' -f2; }
        PM=$(get_cfg PROXY_MODE); PG=$(get_cfg PROXY_GROUP); CT=$(get_cfg COUNTRY_TARGET)
        TZV=$(get_cfg TZ_TARGET); LCT=$(get_cfg LOCALE_TARGET)
        inject_cfg() {
            local value="$2"
            value=${value//\\/\\\\}; value=${value//&/\\&}; value=${value//|/\\|}
            grep -q "^$1=\"" "$CORE_PATH.bak" && sed -i '' "s|^$1=\".*\"|$1=\"$value\"|" "$CORE_PATH"
        }
        inject_cfg PROXY_MODE "$PM"; inject_cfg PROXY_GROUP "$PG"; inject_cfg COUNTRY_TARGET "$CT"
        inject_cfg TZ_TARGET "$TZV"; inject_cfg LOCALE_TARGET "$LCT"
    fi
    chmod +x "$CORE_PATH"

    # 守护进程脚本(root 所有,用户态不可写):与远程模板比对,有差异只提示不自动改
    APPDIR="/Library/Application Support/claude-switcher-fork"
    if [ -f "$APPDIR/claude-bodyguard.sh" ] || [ -f "$APPDIR/tz-watchdog.sh" ]; then
        [ -z "$PM" ] && PM="clash-verge"
        [ -z "$TZV" ] && TZV="America/New_York"
        user_uid=$(stat -f '%u' "$UHOME")
        sed -e "s|__HOME__|$UHOME|g" -e "s|__USER_UID__|$user_uid|g" -e "s|__PROXY_MODE__|$PM|g" "$NEW/daemons/claude-bodyguard.sh" > "$tmpd/bg.new"
        sed -e "s|__HOME__|$UHOME|g" -e "s|__USER_UID__|$user_uid|g" -e "s|__TZ_TARGET__|$TZV|g" "$NEW/daemons/tz-watchdog.sh" > "$tmpd/tz.new"
        if ! diff -q "$tmpd/bg.new" "$APPDIR/claude-bodyguard.sh" >/dev/null 2>&1 || \
           ! diff -q "$tmpd/tz.new" "$APPDIR/tz-watchdog.sh" >/dev/null 2>&1; then
            echo "INFO:守护进程脚本有更新,请运行 installer/安装.command 刷新(需要密码)"
        fi
    fi
    # 更新后自检:root(旧版 LaunchDaemon)下切回真实用户执行,避免 defaults 读到 root 的配置
    if [ "$(id -u)" -eq 0 ]; then
        owner=$(stat -f '%Su' "$UHOME" 2>/dev/null)
        if [ -n "$owner" ]; then out=$(sudo -H -u "$owner" "$CORE_PATH" checkdetail 2>&1); else out=""; fi
    else
        out=$("$CORE_PATH" checkdetail 2>&1)
    fi
    count=$(echo "$out" | grep -Ec '^[^#=]+=(ok|warn|fail)(:|$)')
    if [ "$count" -ne 14 ] || echo "$out" | grep -q '=fail'; then
        [ -f "$CORE_PATH.bak" ] && cp "$CORE_PATH.bak" "$CORE_PATH"
        restore_update_helpers
        rm -rf "$tmpd"
        echo "FAIL:新版自检未通过,已自动回滚"
        return 1
    fi
    rm -rf "$tmpd"
    echo "OK:更新完成 $(date '+%Y-%m-%d')"
    return 0
}

# 设备指纹体检:字体地区特征 + GPU/虚拟机特征(Canvas/WebGL 由 Web 检测页覆盖)
fingerprint() {
    echo "# 设备指纹体检 $(date '+%F %T')"
    hits=""
    for d in /Library/Fonts "$HOME/Library/Fonts"; do
        [ -d "$d" ] || continue
        while IFS= read -r f; do
            b=$(basename "$f" | tr '[:upper:]' '[:lower:]')
            case "$b" in
                *simsun*|*simhei*|*simkai*|*simfang*|*msyh*|*msjh*|*dengxian*|*wqy*|*fzshu*|*fzkai*|*fzhei*|*fangsong*) hits="$hits $b" ;;
            esac
        done < <(find "$d" -type f \( -iname '*.ttf' -o -iname '*.ttc' -o -iname '*.otf' \) 2>/dev/null)
    done
    if [ -n "$hits" ]; then
        echo "字体=warn:检测到地区特征字体:${hits# }"
        echo "建议=上述字体通常随中文办公软件安装,美区环境很少出现;不用的可在字体册停用"
    else
        echo "字体=ok:未发现中文办公字体(仅系统自带字体)"
    fi

    gpu=$(system_profiler SPDisplaysDataType 2>/dev/null)
    chip=$(echo "$gpu" | grep -m1 'Chipset Model' | sed 's/.*: //')
    res=$(echo "$gpu" | grep -m1 'Resolution' | sed 's/.*: //')
    [ -z "$chip" ] && chip="未知"
    echo "GPU=ok:${chip}"
    echo "显示=ok:${res:-未知}"
    virt=$(sysctl -n kern.hv_vmm_present 2>/dev/null)
    case "$chip" in
        *Virtual*|*VMware*|*Parallels*|*VirtualBox*|*SwiftShader*|*llvmpipe*)
            echo "虚拟机=warn:检测到虚拟 GPU($chip),这是强指纹" ;;
        *)
            [ "$virt" = "1" ] && echo "虚拟机=warn:运行在虚拟机中" || echo "虚拟机=ok:物理机" ;;
    esac
    echo "CanvasWebGL=info:原生检查读不到 Claude Electron 的渲染特征,请打开 Web 检测页 https://yoursjeyo.github.io/claude-switcher/ 查看 Canvas/WebGL 指纹"
}

status() {
    cur="none"; [ -f "$STATE_BASE/current" ] && cur=$(cat "$STATE_BASE/current")
    hasA=0; [ -d "$STATE_BASE/accounts/A" ] && hasA=1
    hasB=0; [ -d "$STATE_BASE/accounts/B" ] && hasB=1
    echo "current=$cur hasA=$hasA hasB=$hasB"
}

quit_claude() {
    local pids
    pids=$(claude_pids)
    [ -n "$pids" ] || return 0
    # 按进程 ID 关闭本版实例;不向共享 bundle ID 发送会影响原版的 quit。
    for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
    for i in $(seq 1 15); do
        [ -n "$(claude_pids)" ] || return 0
        sleep 1
    done
    return 1
}

launch_claude() {
    open -na /Applications/Claude.app --args "--user-data-dir=$DATA"
}

archive_current() {
    local cur="$1"
    local t0 staged current previous moved=0 had_old=0
    t0=$(date +%s)
    staged="$STATE_BASE/accounts/$cur.new"
    current="$STATE_BASE/accounts/$cur"
    previous="$STATE_BASE/accounts/$cur.previous"

    # An interrupted archive may be the only remaining copy. Never clear it
    # automatically, or delete the previous snapshot before the new one commits.
    if [ -e "$staged" ] || [ -L "$staged" ] || [ -e "$previous" ] || [ -L "$previous" ]; then
        log "FAIL:账号$cur 存在待恢复快照，拒绝覆盖"
        return 1
    fi
    if mv "$DATA" "$staged" 2>/dev/null; then
        moved=1
    else
        # A cross-volume move may leave a partial destination on failure.
        if [ -e "$staged" ] || [ -L "$staged" ]; then
            log "FAIL:账号$cur 归档移动不完整，保留现场"
            return 1
        fi
        if ! copy_tree "$DATA" "$staged"; then
            log "FAIL:账号$cur 归档复制失败，保留现场"
            return 1
        fi
    fi

    if [ -e "$current" ] || [ -L "$current" ]; then
        if ! mv "$current" "$previous"; then
            if [ "$moved" -eq 1 ]; then
                mv "$staged" "$DATA" || log "WARN:账号$cur 数据保留在暂存位置"
            fi
            log "FAIL:账号$cur 旧快照无法安全转存"
            return 1
        fi
        had_old=1
    fi
    if ! mv "$staged" "$current"; then
        if [ "$had_old" -eq 1 ]; then
            mv "$previous" "$current" || log "WARN:账号$cur 旧快照保留在恢复位置"
        fi
        if [ "$moved" -eq 1 ]; then
            mv "$staged" "$DATA" || log "WARN:账号$cur 当前数据保留在暂存位置"
        fi
        log "FAIL:账号$cur 归档提交失败"
        return 1
    fi
    if [ "$had_old" -eq 1 ]; then
        rm -rf "$previous" || log "WARN:账号$cur 旧快照副本需手动清理"
    fi
    log "已存档账号$cur ($(( $(date +%s) - t0 ))s)"
}

# 快照复制:APFS 上用 clonefile(COW) 近乎瞬时,不改写已有快照;不支持时回退 ditto 全量复制
copy_tree() {
    src="$1"; dst="$2"
    if [ "${CLAUDE_SWITCHER_NO_CLONE:-0}" != "1" ] && cp -cRp "$src" "$dst" 2>/dev/null; then
        return 0
    fi
    rm -rf "$dst"
    ditto "$src" "$dst"
}

switch_to() {
    target="$1"
    case "$target" in A|B) ;; *) echo "FAIL:账号只能为 A 或 B"; return 1;; esac
    check || { echo "FAIL:启动安检未通过,未切换账号"; return 1; }
    cur="none"; [ -f "$STATE_BASE/current" ] && cur=$(cat "$STATE_BASE/current")
    just_archived=0

    # 首次运行:把当前登录中的状态收编为账号A
    if [ "$cur" = "none" ] && [ -d "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
        quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }
        archive_current A || { echo "FAIL:首次账号归档失败,已停止切换"; return 1; }
        just_archived=1
        cur="A"; echo "$cur" > "$STATE_BASE/current"
        log "首次运行:当前登录状态已收编为账号A"
        echo "NOTE:已将当前登录收编为账号A"
    fi

    if [ "$cur" = "$target" ]; then
        # 归档是移动语义,首次收编后需把当前账号还原回 DATA
        if [ "$just_archived" = "1" ] && [ ! -d "$DATA" ] && [ -d "$STATE_BASE/accounts/$target" ]; then
            copy_tree "$STATE_BASE/accounts/$target" "$DATA" || { echo "FAIL:恢复账号失败,快照已保留"; return 1; }
        fi
        launch_claude
        echo "INFO:当前已是账号$target"
        return 0
    fi

    quit_claude || { echo "FAIL:Claude 未能退出，请手动退出后重试"; return 1; }

    if [ "$just_archived" = "0" ] && [ "$cur" != "none" ] && [ -d "$DATA" ]; then
        archive_current "$cur" || { echo "FAIL:账号归档失败,已停止切换"; return 1; }
    fi

    rm -rf "$DATA.new"
    t0=$(date +%s)
    if [ -d "$STATE_BASE/accounts/$target" ]; then
        copy_tree "$STATE_BASE/accounts/$target" "$DATA.new" || { echo "FAIL:载入账号失败,快照已保留"; return 1; }
        log "已载入账号$target 存档 ($(( $(date +%s) - t0 ))s)"
    else
        mkdir -p "$DATA.new"
        log "账号$target 无存档，将以全新状态初始化"
    fi
    if ! { rm -rf "$DATA" && mv "$DATA.new" "$DATA"; }; then
        echo "FAIL:活动目录提交失败,账号快照和暂存数据已保留"; return 1
    fi
    echo "$target" > "$STATE_BASE/current"

    launch_claude
    log "已切换到账号$target 并启动 ($(( $(date +%s) - t0 ))s)"
    echo "OK:已切换到账号$target ($(( $(date +%s) - t0 ))s)"
    return 0
}

case "${1:-}" in
    check)  check ;;
    checkdetail)
        OUT_FILE="$BASE/last-check.txt"
        [ ! -f "$OUT_FILE" ] || chmod 600 "$OUT_FILE"
        {
            echo "# $(date '+%F %T') pid=$$ 开始"
            checkdetail
            echo "# 结束"
        } > "$OUT_FILE" 2>&1
        cat "$OUT_FILE"
        ;;
    fix) acquire_operation_lock && require_exclusive_system && fix ;;
    status) status ;;
    switch) acquire_operation_lock && switch_to "${2:-}" ;;
    set-region) acquire_operation_lock && require_exclusive_system && set_region ;;
    update) acquire_operation_lock && self_update "${2:-}" ;;
    write-global-rules|write-merge-rules) acquire_operation_lock && require_exclusive_system && write_global_rules ;;  # 旧入口仅作兼容,统一写全局脚本
    fingerprint) fingerprint ;;
    *) echo "用法: claude-switch-fork.sh check|checkdetail|fix|status|switch A|B|set-region|update [--auto]|fingerprint|diagnose-proxy [--proxy URL|--config FILE]"; exit 2 ;;
esac
