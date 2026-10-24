"""Wire-format and dual-stack regressions; all networking is mocked."""
import contextlib
import importlib.util
import io
import ipaddress
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("stun_probe", ROOT / "core/stun-probe.py")
stun = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stun)
V4 = "8.8.4.4"
V6 = "2001:4860:4860::8888"
TXN = bytes(range(12))


def reply(address, xor=True, transaction=TXN, prefix=b""):
    ip = ipaddress.ip_address(address)
    packed = ip.packed
    if xor:
        mask = struct.pack("!I", stun.COOKIE) + transaction
        packed = bytes(a ^ b for a, b in zip(packed, mask))
    value = struct.pack("!BBH", 0, 1 if ip.version == 4 else 2, 12345) + packed
    payload = prefix + struct.pack("!HH", 0x0020 if xor else 0x0001, len(value)) + value
    return struct.pack("!HHI", 0x0101, len(payload), stun.COOKIE) + transaction + payload


def records(*addresses):
    return [{"state": "ok", "address": ipaddress.ip_address(x)} for x in addresses]


def references(*versions):
    return {v: {"state": "ok", "address": ipaddress.ip_address(V4 if v == 4 else V6),
                "country": "SG"} for v in versions}


class StunWireTests(unittest.TestCase):
    def test_ipv4_and_ipv6_xor_and_plain_mapping(self):
        for address in (V4, V6):
            for xor in (False, True):
                with self.subTest(address=address, xor=xor):
                    self.assertEqual(stun.parse_response(reply(address, xor), TXN), ipaddress.ip_address(address))

    def test_padding_unknown_attribute_is_skipped(self):
        prefix = struct.pack("!HH", 0x8022, 3) + b"abc\0"
        self.assertEqual(stun.parse_response(reply(V6, prefix=prefix), TXN), ipaddress.ip_address(V6))

    def test_corrupt_cookie_transaction_and_truncated_attributes_are_rejected(self):
        valid = reply(V6)
        variants = [valid[:19], valid[:-1], valid[:4] + b"\0\0\0\0" + valid[8:],
                    valid[:8] + b"x" * 12 + valid[20:], valid[:22] + b"\x00\x40" + valid[24:]]
        for data in variants:
            with self.subTest(data=data.hex()):
                self.assertIsNone(stun.parse_response(data, TXN))

    def test_private_and_fake_ip_are_not_accepted_as_public_mappings(self):
        for address in ("127.0.0.1", "192.168.1.2", "198.18.0.1", "fd00::1"):
            with self.subTest(address=address):
                self.assertIsNone(stun.parse_response(reply(address), TXN))

    def test_probe_uses_requested_family_and_connected_udp(self):
        channel = mock.MagicMock()
        channel.__enter__.return_value = channel
        channel.recv.return_value = reply(V6)
        with mock.patch.object(stun.socket, "getaddrinfo", return_value=[(socket.AF_INET6, 2, 17, "", (V6, 3478, 0, 0))]) as resolve, \
             mock.patch.object(stun.socket, "socket", return_value=channel) as create, \
             mock.patch.object(stun.os, "urandom", return_value=TXN):
            result = stun.stun_public_ip("stun.example", 3478, socket.AF_INET6)
        resolve.assert_called_once_with("stun.example", 3478, socket.AF_INET6, socket.SOCK_DGRAM)
        create.assert_called_once_with(socket.AF_INET6, socket.SOCK_DGRAM)
        channel.connect.assert_called_once_with((V6, 3478, 0, 0))
        self.assertEqual(result["address"], ipaddress.ip_address(V6))


class DualStackAuditTests(unittest.TestCase):
    def test_egress_retains_each_family_address_and_country(self):
        refs = references(4, 6)
        refs[0] = refs[6]
        result = stun.egress_summary(refs, "SG")
        self.assertEqual(result["status"], "ok")
        self.assertIn("IPv4: " + V4 + " / SG", result["detail"])
        self.assertIn("IPv6: " + V6 + " / SG", result["detail"])
        self.assertEqual(result["primary_ip"], V6)

    def test_proxy_side_ipv6_from_default_request_fills_missing_literal_path(self):
        refs = references(4)
        refs[0] = references(6)[6]
        result = stun.egress_summary(refs, "SG")
        self.assertEqual(result["status"], "ok")
        self.assertIn("IPv6: " + V6, result["detail"])
        self.assertEqual(stun.evaluate(records(V4), result["references"], "SG")[0], "warn")

    def test_unavailable_family_is_explicit_and_all_missing_fails(self):
        result = stun.egress_summary(references(4), "SG")
        self.assertEqual(result["status"], "ok")
        self.assertIn("IPv6: 未获取", result["detail"])
        self.assertEqual(stun.egress_summary({}, "SG")["status"], "fail")

    def test_any_observed_country_mismatch_fails_including_default_request(self):
        for key in (0, 4, 6):
            refs = references(4, 6)
            refs[0] = dict(refs[6])
            refs[key] = dict(refs[key], country="US")
            with self.subTest(key=key):
                self.assertEqual(stun.egress_summary(refs, "SG")["status"], "fail")

    def test_snapshot_collects_three_references_once_without_repeating_udp(self):
        with mock.patch.object(stun, "stun_public_ip", return_value={"state": "no_response"}) as udp, \
             mock.patch.object(stun, "reference_egress", side_effect=lambda v: references(v or 6)[v or 6]) as http:
            _, reference = stun.collect(audit=True, include_default=True)
        self.assertEqual(udp.call_count, 8)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(set(reference), {0, 4, 6})

    def test_egress_only_does_not_send_stun_when_proxy_policy_unverified(self):
        with mock.patch.object(stun, "stun_public_ip") as udp, \
             mock.patch.object(stun, "reference_egress", return_value={"state": "unavailable"}):
            stun.collect(audit=True, include_default=True, probe_stun=False)
        udp.assert_not_called()

    def test_default_reference_is_not_forced_to_ipv4_or_ipv6(self):
        process = subprocess.CompletedProcess([], 0, "ip=" + V6 + "\nloc=SG\n", "")
        with mock.patch.object(stun.subprocess, "run", return_value=process) as run:
            self.assertEqual(stun.reference_egress(0)["state"], "ok")
        cmd=run.call_args.args[0]
        self.assertEqual(cmd[-1], stun.DEFAULT_REFERENCE)
        self.assertNotIn("-0", cmd)
        self.assertNotIn("-4", cmd)
        self.assertNotIn("-6", cmd)

    def test_snapshot_exports_ipc_fields_and_fails_if_udp_audit_fails(self):
        refs=references(4, 6); refs[0]=refs[6]
        with mock.patch.object(stun, "collect", return_value=([], refs)), \
             mock.patch("sys.argv", ["stun-probe.py", "--snapshot", "--country", "SG"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code=stun.main()
        self.assertEqual(code, 1)
        lines=output.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[2].startswith("egress=ok:"))
        self.assertTrue(lines[3].startswith("webrtc=fail:"))

    def test_both_proxy_families_pass_independently(self):
        status, detail = stun.evaluate(records(V4, V6), references(4, 6), "SG")
        self.assertEqual(status, "ok")
        self.assertIn("IPv4/IPv6", detail)
        self.assertNotIn(V4, detail)
        self.assertNotIn(V6, detail)

    def test_ipv6_web_egress_does_not_make_correct_ipv4_stun_fail(self):
        status, detail = stun.evaluate(records(V4), references(4, 6), "SG")
        self.assertEqual(status, "warn")
        self.assertIn("IPv6", detail)
        self.assertNotIn("不一致", detail)

    def test_single_stack_networks_can_pass(self):
        for v, address in ((4, V4), (6, V6)):
            with self.subTest(version=v):
                self.assertEqual(stun.evaluate(records(address), references(v), "SG")[0], "ok")

    def test_equivalent_ipv6_notation_is_normalized(self):
        expanded = "2001:4860:4860:0000:0000:0000:0000:8888"
        self.assertEqual(stun.evaluate(records(expanded), references(6), "SG")[0], "ok")

    def test_any_foreign_mapping_fails_even_if_other_server_matches(self):
        status, detail = stun.evaluate(records(V4, "9.9.9.9", V6), references(4, 6), "SG")
        self.assertEqual(status, "fail")
        self.assertIn("IPv4", detail)
        self.assertNotIn("9.9.9.9", detail)

    def test_no_reply_or_missing_same_family_reference_never_passes(self):
        for observed, reference in (([], references(4, 6)),
                                    ([{"state": "no_response"}], references(4)),
                                    (records(V4), references(6)), (records(V6), references(4))):
            with self.subTest(observed=observed):
                status, detail = stun.evaluate(observed, reference, "SG")
                self.assertEqual(status, "fail")
                self.assertNotIn("零暴露", detail)

    def test_reference_country_must_match_locked_target(self):
        self.assertEqual(stun.evaluate(records(V4), references(4), "JP")[0], "fail")

    def test_collect_attempts_both_families_without_real_network(self):
        with mock.patch.object(stun, "stun_public_ip", return_value={"state": "no_response"}) as udp, \
             mock.patch.object(stun, "reference_egress", side_effect=lambda v: references(v)[v]):
            observed, reference = stun.collect(audit=True)
        self.assertEqual(len(observed), 8)
        self.assertEqual({call.args[2] for call in udp.call_args_list}, {socket.AF_INET, socket.AF_INET6})
        self.assertEqual(set(reference), {4, 6})

    def test_dns_stalls_do_not_exceed_global_deadline(self):
        def stalled(*args):
            time.sleep(.15)
            return {"state": "unavailable"}
        with mock.patch.object(stun, "stun_public_ip", side_effect=stalled):
            started = time.monotonic()
            observed, _ = stun.collect(deadline=.01)
            self.assertLess(time.monotonic() - started, .12)
        self.assertEqual(observed, [])

    def test_reference_requests_are_family_specific_tls_verified_and_ignore_proxy_env(self):
        for version, address in ((4, V4), (6, V6)):
            process = subprocess.CompletedProcess([], 0, "ip=" + address + "\nloc=SG\n", "")
            with mock.patch.object(stun.subprocess, "run", return_value=process) as run, \
                 mock.patch.dict(os.environ, HTTP_PROXY="private-proxy", NO_PROXY="*", ANTHROPIC_AUTH_TOKEN="secret"):
                result = stun.reference_egress(version)
            self.assertEqual(result["state"], "ok")
            cmd = run.call_args.args[0]
            self.assertIn("-" + str(version), cmd)
            self.assertEqual(cmd[-1], stun.REFERENCES[version])
            self.assertNotIn("-k", cmd)
            self.assertEqual(cmd[1], "-q")
            self.assertNotIn("HTTP_PROXY", run.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", run.call_args.kwargs["env"])

    def test_invalid_reference_and_tls_failure_do_not_become_trusted_egress(self):
        for process in (subprocess.CompletedProcess([], 60, "ip=" + V4 + "\nloc=SG\n", "secret"),
                        subprocess.CompletedProcess([], 0, "ip=" + V6 + "\nloc=SG\n", ""),
                        subprocess.CompletedProcess([], 0, "ip=198.18.0.1\nloc=SG\n", "")):
            with mock.patch.object(stun.subprocess, "run", return_value=process):
                self.assertNotEqual(stun.reference_egress(4)["state"], "ok")

    def test_main_emits_no_address_and_no_response_is_failure(self):
        with mock.patch.object(stun, "collect", return_value=([], references(4))), \
             mock.patch("sys.argv", ["stun-probe.py", "--audit", "--country", "SG"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = stun.main()
        self.assertEqual(code, 1)
        self.assertTrue(output.getvalue().startswith("fail:"))
        self.assertNotIn(V4, output.getvalue())


if __name__ == "__main__":
    unittest.main()
