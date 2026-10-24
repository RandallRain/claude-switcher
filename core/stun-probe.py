#!/usr/bin/env python3
"""Bounded IPv4/IPv6 STUN probes and same-family HTTPS egress verification.

--audit emits a redacted ok|warn|fail result shared by check/checkdetail/fix.
The legacy RESULT output is retained for older callers; no response is UNKNOWN.
"""
import argparse
import ipaddress
import os
import socket
import struct
import subprocess
import threading
import time


COOKIE = 0x2112A442
SERVERS = (
    ("stun.miwifi.com", 3478),
    ("stun.chat.bilibili.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
)
REFERENCES = {
    4: "https://1.1.1.1/cdn-cgi/trace",
    6: "https://[2606:4700:4700::1111]/cdn-cgi/trace",
}
DEFAULT_REFERENCE = "https://www.cloudflare.com/cdn-cgi/trace"


def normalized_ip(value):
    address = ipaddress.ip_address(value)
    return getattr(address, "ipv4_mapped", None) or address


def parse_response(data, transaction):
    """Validate the response and decode IPv4/IPv6 MAPPED/XOR-MAPPED-ADDRESS."""
    if len(data) < 20:
        return None
    kind, size, cookie = struct.unpack("!HHI", data[:8])
    if (kind != 0x0101 or cookie != COOKIE or data[8:20] != transaction
            or size % 4 or len(data) < 20 + size):
        return None
    end, pos, mapped = 20 + size, 20, None
    while pos + 4 <= end:
        atype, length = struct.unpack("!HH", data[pos:pos + 4])
        if pos + 4 + length > end:
            return None
        value = data[pos + 4:pos + 4 + length]
        if atype in (0x0001, 0x0020) and len(value) >= 4:
            count = {1: 4, 2: 16}.get(value[1])
            if count is None or length != count + 4:
                return None
            packed = value[4:]
            if atype == 0x0020:
                mask = struct.pack("!I", COOKIE) + transaction
                packed = bytes(a ^ b for a, b in zip(packed, mask[:count]))
            address = normalized_ip(ipaddress.ip_address(packed))
            if not address.is_global:
                return None
            if atype == 0x0020:
                return address
            mapped = address
        pos += 4 + length + (-length % 4)
    return mapped


def stun_public_ip(host, port, family, timeout=2.0):
    try:
        addresses = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    except OSError:
        return {"state": "unavailable"}
    if not addresses:
        return {"state": "unavailable"}
    transaction = os.urandom(12)
    request = struct.pack("!HHI", 0x0001, 0, COOKIE) + transaction
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as channel:
            channel.settimeout(timeout)
            # Connected UDP accepts replies only from the queried endpoint.
            channel.connect(addresses[0][4])
            channel.send(request)
            address = parse_response(channel.recv(2048), transaction)
        return {"state": "ok", "address": address} if address else {"state": "invalid_response"}
    except OSError:
        return {"state": "no_response"}


def reference_egress(version):
    keep = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "SYSTEMROOT"}
    environment = {k: v for k, v in os.environ.items() if k in keep}
    try:
        args = ["curl", "-q", "-fsS"]
        if version:
            args.append("-" + str(version))
        result = subprocess.run(
            args + ["--noproxy", "*",
             "--proto", "=https", "--connect-timeout", "3", "--max-time", "5",
             REFERENCES[version] if version else DEFAULT_REFERENCE],
            env=environment, capture_output=True, text=True, timeout=6)
        if result.returncode:
            return {"state": "unavailable"}
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        address = normalized_ip(values["ip"])
        country = values["loc"]
        if ((version and address.version != version) or not address.is_global
                or len(country) != 2 or not country.isascii() or not country.isalpha()):
            return {"state": "invalid_response"}
        return {"state": "ok", "address": address, "country": country.upper()}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {"state": "unavailable"}


def collect(audit=False, deadline=8.0, include_default=False, probe_stun=True):
    """Daemon workers bound DNS stalls too; no signal handlers at import time."""
    records, references, lock = [], {}, threading.Lock()

    def stun_worker(host, port, family):
        record = stun_public_ip(host, port, family)
        record.update(transport=4 if family == socket.AF_INET else 6)
        with lock:
            records.append(record)

    def reference_worker(version):
        value = reference_egress(version)
        with lock:
            references[version] = value

    workers = [threading.Thread(target=stun_worker, args=(host, port, family), daemon=True)
               for host, port in SERVERS for family in (socket.AF_INET, socket.AF_INET6)] if probe_stun else []
    if audit:
        versions = (0, 4, 6) if include_default else (4, 6)
        workers += [threading.Thread(target=reference_worker, args=(v,), daemon=True) for v in versions]
    stop = time.monotonic() + deadline
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0, stop - time.monotonic()))
    with lock:
        return list(records), dict(references)


def egress_summary(references, country):
    """Keep both families, including a proxy-side IPv6 observed via DNS routing."""
    selected = {v: references.get(v, {"state": "unavailable"}) for v in (4, 6)}
    automatic = references.get(0, {})
    if automatic.get("state") == "ok":
        version = normalized_ip(automatic["address"]).version
        if selected[version].get("state") != "ok":
            selected[version] = automatic
    available = [ref for ref in selected.values() if ref.get("state") == "ok"]
    primary = automatic if automatic.get("state") == "ok" else next(iter(available), {})
    observed = available + ([automatic] if automatic.get("state") == "ok" else [])
    invalid_country = any(country and ref.get("country") != country for ref in observed)
    status = "fail" if not available or invalid_country else "ok"
    lines = []
    for version, ref in selected.items():
        if ref.get("state") == "ok":
            lines.append("IPv%d: %s / %s" % (version, normalized_ip(ref["address"]), ref["country"]))
        else:
            lines.append("IPv%d: 未获取" % version)
    if invalid_country:
        lines.append("出口国家不一致，期望 " + country)
    return {"status": status, "detail": " | ".join(lines), "references": selected,
            "primary_ip": str(primary.get("address", "")), "primary_country": primary.get("country", "")}


def evaluate(records, references, country):
    """Never compare IPv4 with IPv6 or label a timeout 'zero exposure'."""
    addresses = {normalized_ip(record["address"]) for record in records if record.get("state") == "ok"}
    if not addresses:
        return "fail", "STUN均无有效响应，无法验证UDP出口（可能阻断、超时或解析失败）"
    versions = sorted({address.version for address in addresses})
    for version in versions:
        reference = references.get(version, {})
        if reference.get("state") != "ok":
            return "fail", "IPv%d参考出口不可达，无法验证UDP出口" % version
        if country and reference.get("country") != country:
            return "fail", "IPv%d参考出口国家与目标不一致" % version
        expected = normalized_ip(reference["address"])
        if expected.version != version:
            return "fail", "IPv%d参考出口格式异常" % version
        if any(address != expected for address in addresses if address.version == version):
            return "fail", "IPv%d UDP出口与同版本参考不一致（疑似直连或代理分流）" % version
    verified = "/".join("IPv" + str(version) for version in versions)
    missing = [v for v in (4, 6) if v not in versions and references.get(v, {}).get("state") == "ok"]
    if missing:
        return "warn", verified + " UDP出口一致；IPv%d STUN无响应，未验证该路径" % missing[0]
    return "ok", verified + " UDP出口与同版本参考一致"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--snapshot", action="store_true", help="egress and UDP audit in one bounded round")
    mode.add_argument("--egress", action="store_true", help="egress only, without sending STUN packets")
    parser.add_argument("--country", default="")
    args = parser.parse_args()
    country = args.country.upper()
    if country and (len(country) != 2 or not country.isalpha()):
        print("fail:目标国家配置异常")
        return 1
    try:
        snapshot = args.snapshot or args.egress
        records, references = collect(audit=args.audit or snapshot, include_default=snapshot,
                                      probe_stun=not args.egress)
        if snapshot:
            result = egress_summary(references, country)
            # Private IPC for the local GUI; never include these addresses in logs.
            print("primary_ip=" + result["primary_ip"])
            print("primary_country=" + result["primary_country"])
            print("egress=" + result["status"] + ":" + result["detail"])
            if args.snapshot:
                status, detail = evaluate(records, result["references"], country)
                print("webrtc=" + status + ":" + detail)
            return 1 if result["status"] == "fail" or (args.snapshot and status == "fail") else 0
        if args.audit:
            status, detail = evaluate(records, references, country)
            print(status + ":" + detail)
            return 1 if status == "fail" else 0
        addresses = sorted({str(r["address"]) for r in records if r.get("state") == "ok"})
        print("RESULT:" + (",".join(addresses) if addresses else "UNKNOWN"))
        return 0 if addresses else 1
    except (OSError, ValueError, KeyError, TypeError):
        print("fail:STUN检测异常" if args.audit else "RESULT:UNKNOWN")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
