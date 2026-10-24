#!/usr/bin/env python3
"""Install managed Global Extend Script rules and verify the running Mihomo core.

Only Python's standard library is required. Controller credentials and network
addresses are never printed. Existing user JavaScript is retained in a closure.
"""
import argparse
import datetime
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile


BEGIN = "// >>> claude-switcher-fork global rules v1 >>>\n"
USER_BEGIN = "// >>> claude-switcher-fork original script >>>\n"
USER_END = "// <<< claude-switcher-fork original script <<<\n"
END = "// <<< claude-switcher-fork global rules v1 <<<\n"
DOMAINS = (
    "claude.ai", "claude.com", "anthropic.com", "claudeusercontent.com",
    "challenges.cloudflare.com", "datadoghq.com", "segment.io", "stripe.com",
    "stripe.network", "stripecdn.com", "intercom.io", "intercomcdn.com",
    "intercomassets.com", "www.cloudflare.com",
)
PORTS = ("3478", "5349", "19302")


def user_script(text):
    if text.startswith(BEGIN):
        if not text.endswith(END) or text.count(USER_BEGIN) != 1 or text.count(USER_END) != 1:
            raise ValueError("managed script was edited outside its user section")
        return text.split(USER_BEGIN, 1)[1].split(USER_END, 1)[0]
    if any(marker.strip() in text for marker in (BEGIN, USER_BEGIN, USER_END, END)):
        raise ValueError("conflicting script markers")
    return text if not text or text.endswith("\n") else text + "\n"


def render_script(original, preferred):
    template = Path(__file__).with_name("clash-global-rules.js").read_text(encoding="utf-8")
    # The outer null binding prevents an empty user script from capturing the
    # wrapper's hoisted main and recursively calling itself. The inner scope
    # still permits both function main and const/let main entrypoints.
    return (BEGIN + "const __claudeSwitcherForkUserMain = (function (main) {\nreturn (function () {\n" + USER_BEGIN +
            user_script(original) + USER_END +
            "return typeof main === 'function' ? main : null;\n})();\n})(null);\n" + template +
            "\nfunction main(config, profileName) {\n" +
            "  const result = __claudeSwitcherForkUserMain ? __claudeSwitcherForkUserMain(config, profileName) : config;\n" +
            "  return __claudeSwitcherForkApply(result, " + json.dumps(preferred, ensure_ascii=True) + ");\n}\n" + END)


def install(root, preferred):
    path = root / "profiles" / "Script.js"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    result = render_script(original, preferred)
    if result == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".claude-switcher-fork-" + stamp + ".bak")
        with backup.open("x", encoding="utf-8") as handle:
            os.chmod(backup, 0o600)
            handle.write(original)
    fd, temporary = tempfile.mkstemp(prefix=".claude-switcher-fork-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(result)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def yaml_scalar(text, key):
    """Read only top-level controller scalars, not arbitrary YAML structures."""
    match = re.search(r"^" + re.escape(key) + r":[ \t]*(.*?)[ \t]*$", text, re.M)
    if not match:
        return ""
    value = match.group(1)
    if value.startswith('"'):
        return json.JSONDecoder().raw_decode(value)[0]
    if value.startswith("'"):
        match = re.match(r"'((?:[^']|'')*)'", value)
        if not match:
            raise ValueError("invalid controller scalar")
        return match.group(1).replace("''", "'")
    value = value.split(" #", 1)[0].strip()
    return "" if value in ("null", "~") else value


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=2)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Controller:
    def __init__(self, root):
        config = (root / "clash-verge.yaml").read_text(encoding="utf-8")
        self.secret = yaml_scalar(config, "secret")
        self.unix = yaml_scalar(config, "external-controller-unix")
        # Service mode overrides the socket path in the generated YAML.
        service = Path("/var/run/clash-verge-service/users") / str(os.getuid()) / "verge-mihomo.sock"
        if service.exists():
            self.unix = str(service)
        self.address = yaml_scalar(config, "external-controller")

    def get(self, path):
        if self.unix:
            conn = UnixConnection(self.unix)
        else:
            host, port = self.address.rsplit(":", 1)
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                raise ValueError("controller is not local")
            conn = http.client.HTTPConnection(host.strip("[]"), int(port), timeout=2)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + self.secret})
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError("controller unavailable")
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("controller response too large")
            return json.loads(raw)
        finally:
            conn.close()


def is_udp_proxy(name, proxies, seen=None):
    seen = set() if seen is None else seen
    if name in seen or name not in proxies:
        return False
    seen.add(name)
    proxy = proxies[name]
    if str(proxy.get("type", "")).lower() in ("direct", "reject", "rejectdrop", "pass", "compatible", "dns"):
        return False
    if not proxy.get("udp"):
        return False
    if proxy.get("now"):
        return is_udp_proxy(proxy["now"], proxies, seen)
    children = proxy.get("all") or []
    return all(is_udp_proxy(child, proxies, set(seen)) for child in children)


def rules_loaded(rules, proxies):
    """Require the complete ordered rule prefix, not a string in a source file."""
    normalized = [(re.sub(r"[^a-z]", "", str(r.get("type", "")).lower()),
                   str(r.get("payload", "")), str(r.get("proxy", ""))) for r in rules]
    for start, (kind, payload, policy) in enumerate(normalized):
        if (kind, payload) != ("domainsuffix", DOMAINS[0]) or not is_udp_proxy(policy, proxies):
            continue
        expected = [("domainsuffix", domain, policy) for domain in DOMAINS]
        expected += [("ipcidr", "1.1.1.1/32", policy),
                     ("ipcidr", "2606:4700:4700::1111/128", policy)]
        expected += [("dstport", port, policy) for port in PORTS]
        expected += [("dstport", port, "REJECT") for port in PORTS]
        actual = normalized[start:start + len(expected)]
        # Mihomo uses IPCIDR for IPv6 on some versions, IPCIDR6 on others.
        actual = [("ipcidr" if k == "ipcidr6" else k, p, v) for k, p, v in actual]
        if actual == expected:
            # Later subscription extensions can prepend rules ahead of this block.
            # Permit private-network and unrelated process exceptions, but refuse
            # rules which can intercept the probe/reference destinations.
            targets = ("www.cloudflare.com", "stun.cloudflare.com", "stun.miwifi.com",
                       "stun.chat.bilibili.com", "stun.l.google.com")
            for k, p, target_policy in normalized[:start]:
                # Existing per-subscription copies of our rules can precede the
                # global block, depending on Verge's enhancement order.
                if target_policy in (policy, "REJECT"):
                    continue
                if k in ("match", "network", "dstport", "geoip", "geosite", "ruleset"):
                    return False
                if k == "domain" and p in targets:
                    return False
                if k == "domainsuffix" and any(t == p or t.endswith("." + p) for t in targets):
                    return False
                if k == "domainkeyword" and any(p in t for t in targets):
                    return False
                if k in ("domainregex", "domainwildcard", "and", "or", "not", "subrule"):
                    return False
                if k in ("ipcidr", "ipcidr6"):
                    network = ipaddress.ip_network(p, strict=False)
                    private = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                               "127.0.0.0/8", "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128")
                    if not any(network.version == ipaddress.ip_network(n).version and
                               network.subnet_of(ipaddress.ip_network(n)) for n in private):
                        return False
                if k.startswith("process") and re.search(r"python|claude|chrome|chromium|firefox|safari", p, re.I):
                    return False
                if k not in ("domain", "domainsuffix", "domainkeyword", "ipcidr", "ipcidr6", "processname", "processpath"):
                    return False
            return True
    return False


def verify(root, preferred, controller=None):
    path = root / "profiles" / "Script.js"
    original = path.read_text(encoding="utf-8")
    if not original.startswith(BEGIN) or render_script(original, preferred) != original:
        return False
    controller = Controller(root) if controller is None else controller
    return rules_loaded(controller.get("/rules").get("rules", []),
                        controller.get("/proxies").get("proxies", {}))


def proxy_check(root, preferred, controller=None):
    """Do not trust same-country direct traffic as a 'proxy' reference."""
    controller = Controller(root) if controller is None else controller
    proxies = controller.get("/proxies").get("proxies", {})
    active = controller.get("/rules").get("rules", [])
    path = root / "profiles" / "Script.js"
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original.startswith(BEGIN):
        return render_script(original, preferred) == original and rules_loaded(active, proxies)
    # Migration from an older installation: verify its existing selected policy
    # until the user runs fix to enable the managed global script.
    policies = {}
    for rule in active:
        if rule.get("type") == "DstPort" and str(rule.get("payload")) in PORTS:
            policies.setdefault(str(rule["payload"]), rule.get("proxy"))
    if policies:
        return len(policies) == len(PORTS) and len(set(policies.values())) == 1 and is_udp_proxy(next(iter(policies.values())), proxies)
    return is_udp_proxy(preferred, proxies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check", "proxy-check"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--group", required=True)
    args = parser.parse_args()
    try:
        if args.action == "install":
            changed = install(args.directory, args.group)
            print("changed" if changed else "unchanged")
            return 0
        check = verify if args.action == "check" else proxy_check
        return 0 if check(args.directory, args.group) else 1
    except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException):
        # Do not echo private config contents, paths, credentials, or addresses.
        if args.action == "install":
            print("全局脚本写入失败；已有脚本保留，请检查 Global Extend Script")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
