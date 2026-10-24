"""Exercise check/checkdetail/fix with isolated HOME and fake system tools."""
import hashlib
import os
import re
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "core/claude-switch.sh"


class WebRTCCommandTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "_trash").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "_trash")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.home, self.bin = self.folder / "home", self.folder / "bin"
        self.home.mkdir(); self.bin.mkdir()
        self.audit = self.folder / "audit-result"
        self.audit.write_text("ok:IPv4 UDP出口与同版本参考一致\n")
        self.loaded = self.folder / "loaded"
        self.loaded.write_text("1")
        self.claude = self.folder / "claude"
        self.claude.write_text("0")
        self.events = self.folder / "events"
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.defpath,
                        TEST_AUDIT=str(self.audit), TEST_LOADED=str(self.loaded),
                        TEST_CLAUDE=str(self.claude), TEST_EVENTS=str(self.events),
                        TEST_LOAD_ON_OPEN="1")
        self.write_tool("python3", r'''
case "$1" in
  */stun-probe.py)
    [ "$2" = --snapshot ] || [ "$2" = --egress ] || exit 99
    [ "$3" = --country ] || exit 99
    printf 'primary_ip=2001:4860:4860::8888\nprimary_country=US\n'
    printf 'egress=%s\n' "${TEST_EGRESS:-ok:IPv4: 8.8.4.4 / US | IPv6: 2001:4860:4860::8888 / US}"
    printf 'webrtc='; /bin/cat "$TEST_AUDIT"
    case "$(/bin/cat "$TEST_AUDIT")" in fail:*) exit 1;; esac ;;
  */clash-rules.py)
    case "$2" in
      check) [ "$(/bin/cat "$TEST_LOADED")" = 1 ];;
      proxy-check) [ "${TEST_PROXY_VALID:-1}" = 1 ];;
      install) printf 'install-global\n' >> "$TEST_EVENTS"; printf 'changed\n';;
      *) exit 99;;
    esac ;;
  *) exit 99;;
esac
''')
        self.write_tool("curl", r'''
for arg do last=$arg; done
case "$*" in
  *http_code*) case "$last" in *api.anthropic.com*) printf 401;; *) printf 301;; esac;;
  *) printf 'ip=2001:4860:4860::8888\nloc=US\n';;
esac
''')
        self.write_tool("pgrep", r'''
case "$*" in
  *Applications/Claude.app*) [ "$(/bin/cat "$TEST_CLAUDE")" = 1 ];;
  *verge-mihomo*) exit 0;;
  *) exit 1;;
esac
''')
        self.write_tool("ifconfig", "printf '    inet 198.18.0.1 netmask 0xffff0000\\n'\n")
        self.write_tool("dig", "printf '198.18.0.2\\n'\n")
        self.write_tool("readlink", "printf '/var/db/timezone/zoneinfo/America/New_York\\n'\n")
        self.write_tool("networksetup", "printf 'IPv6: Off\\n'\n")
        self.write_tool("launchctl", "exit 0\n")
        self.write_tool("sleep", "exit 0\n")
        self.write_tool("defaults", r'''
case "$*" in
  *AppleLocale*) printf 'en_US\n';;
  *AppleLanguages*) printf '(\n    "en-US"\n)\n';;
  *) exit 99;;
esac
''')
        self.write_tool("osascript", r'''
case "$*" in
  *'application "Claude"'*) printf 'quit-claude\n' >> "$TEST_EVENTS"; printf 0 > "$TEST_CLAUDE";;
  *'application "Clash Verge"'*) printf 'quit-verge\n' >> "$TEST_EVENTS";;
  *) printf 'unexpected-osascript\n' >> "$TEST_EVENTS"; exit 99;;
esac
''')
        self.write_tool("open", r'''
[ "$1" = -a ] && [ "$2" = 'Clash Verge' ] || exit 99
printf 'open-verge\n' >> "$TEST_EVENTS"
[ "$TEST_LOAD_ON_OPEN" = 1 ] && printf 1 > "$TEST_LOADED"
exit 0
''')

    def write_tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_cli(self, command, env=None):
        return subprocess.run(["/bin/bash", str(SCRIPT), command],
                              env=self.env if env is None else env,
                              capture_output=True, text=True, timeout=15)

    def test_all_three_commands_accept_ipv4_audit_with_ipv6_web_egress(self):
        for command in ("check", "checkdetail", "fix"):
            with self.subTest(command=command):
                result = self.run_cli(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("WebRTC=fail", result.stdout)
                self.assertNotIn("WebRTC ✗", result.stdout)
        self.assertFalse(self.events.exists(), "valid egress caused an unnecessary proxy restart")

    def test_malformed_udp_outcome_is_not_accepted_by_any_command(self):
        self.audit.write_text("fail:检测异常：STUN响应格式错误\n")
        check = self.run_cli("check")
        detail = self.run_cli("checkdetail")
        fix = self.run_cli("fix")
        self.assertEqual(check.returncode, 1)
        self.assertIn("WebRTC=fail:", detail.stdout)
        self.assertEqual(fix.returncode, 1)
        self.assertIn("WebRTC ✗", fix.stdout)
        self.assertNotIn("零暴露", check.stdout + detail.stdout + fix.stdout)

    def test_partial_dual_stack_is_green_with_a_visible_description(self):
        self.audit.write_text("ok:仅见代理IP（IPv4）；IPv6未取得结果\n")
        self.assertEqual(self.run_cli("check").returncode, 0)
        self.assertIn("WebRTC=ok:仅见代理IP（IPv4）；IPv6未取得结果", self.run_cli("checkdetail").stdout)
        self.assertIn("WebRTC ✓", self.run_cli("fix").stdout)

    def test_no_stun_result_is_green_without_claiming_udp_is_blocked(self):
        self.audit.write_text("ok:未发现非代理IP；STUN均无结果（UDP出口未验证）\n")
        self.assertEqual(self.run_cli("check").returncode, 0)
        self.assertIn("WebRTC=ok:未发现非代理IP", self.run_cli("checkdetail").stdout)
        self.assertIn("WebRTC ✓", self.run_cli("fix").stdout)

    def test_old_probe_output_fails_closed_without_echoing_addresses(self):
        self.audit.write_text("RESULT:8.8.8.8\n")
        for command in ("check", "checkdetail", "fix"):
            with self.subTest(command=command):
                result = self.run_cli(command)
                self.assertIn("请更新探针", result.stdout)
                self.assertNotIn("8.8.8.8", result.stdout)

    def test_fix_quits_claude_then_installs_and_reloads_global_script_once(self):
        self.claude.write_text("1")
        self.loaded.write_text("0")
        result = self.run_cli("fix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events.read_text().splitlines(),
                         ["quit-claude", "install-global", "quit-verge", "open-verge"])
        self.assertIn("全局置顶规则 ✓", result.stdout)

    def test_fix_does_not_claim_rules_loaded_when_subscriber_overrides_them(self):
        self.loaded.write_text("0")
        env = dict(self.env, TEST_LOAD_ON_OPEN="0")
        result = self.run_cli("fix", env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("全局置顶规则 ✗", result.stdout)
        self.assertNotIn("全局置顶规则 ✓", result.stdout)

    def test_old_installer_entrypoint_uses_new_global_script_writer(self):
        for command in ("write-global-rules", "write-merge-rules"):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command).returncode, 0)
        self.assertEqual(self.events.read_text().splitlines(), ["install-global", "install-global"])

    def test_dns_behavior_alone_warns_that_global_rules_are_not_verified(self):
        self.loaded.write_text("0")
        result = self.run_cli("checkdetail")
        self.assertIn("规则置顶=warn:", result.stdout)

    def test_detail_exposes_both_addresses_countries_timezone_and_locale(self):
        result = self.run_cli("checkdetail")
        self.assertIn("出口IP=ok:IPv4: 8.8.4.4 / US | IPv6: 2001:4860:4860::8888 / US", result.stdout)
        self.assertIn("时区=ok:America/New_York", result.stdout)
        self.assertIn("地区=ok:en_US", result.stdout)
        self.assertEqual(len([x for x in result.stdout.splitlines() if re.match(r"^[^=]+=\b(?:ok|warn|fail):?", x)]), 14)

    def test_second_family_country_error_is_not_hidden_by_good_primary_country(self):
        env=dict(self.env, TEST_EGRESS="fail:IPv4: 8.8.4.4 / US | IPv6: 2001:4860:4860::8888 / JP")
        self.assertEqual(self.run_cli("check", env).returncode, 1)
        self.assertIn("出口IP=fail:", self.run_cli("checkdetail", env).stdout)
        self.assertEqual(self.run_cli("fix", env).returncode, 1)

    def test_address_caches_remain_private_when_upgrading_existing_files(self):
        base = self.home / ".claude-switcher"
        base.mkdir()
        caches = [base / name for name in ("last-check.txt", "lastip", "iphistory.log")]
        for path in caches:
            path.write_text("")
            path.chmod(0o644)
        self.assertEqual(self.run_cli("checkdetail").returncode, 0)
        for path in caches:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_timezone_and_region_errors_report_actual_and_expected_values(self):
        self.write_tool("readlink", "printf '/usr/share/zoneinfo/Asia/Singapore\\n'\n")
        self.write_tool("defaults", "printf 'en_SG\\n'\n")
        result=self.run_cli("checkdetail")
        self.assertIn("时区=fail:Asia/Singapore (期望 America/New_York)", result.stdout)
        self.assertIn("地区=fail:en_SG (期望 en_US)", result.stdout)

    def test_direct_or_unverified_proxy_policy_blocks_launch_despite_matching_ip(self):
        result = self.run_cli("check", dict(self.env, TEST_PROXY_VALID="0"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("无法核验Clash的UDP代理策略", result.stdout)

    def prepare_update(self, old_core_suffix=""):
        # Redirect the fixed daemon directory as well as HOME: no installed
        # daemon, app, account data, proxy or network is used by these tests.
        body = SCRIPT.read_text().replace('APPDIR="/Library/Application Support/claude-switcher"',
                                          'APPDIR="$HOME/test-system-daemons"')
        installed = self.home / ".local/bin/claude-switch.sh"
        installed.parent.mkdir(parents=True)
        installed.write_text(body + old_core_suffix)
        installed.chmod(0o755)
        base = self.home / ".claude-switcher"
        base.mkdir()
        files = {"core/claude-switch.sh": body.encode()}
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            files["core/" + name] = (ROOT / "core" / name).read_bytes()
        archive = self.folder / "remote.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for path, data in files.items():
                package.writestr("claude-switcher-main/" + path, data)
        sums = self.folder / "remote-sums"
        sums.write_text("".join(hashlib.sha256(data).hexdigest() + "  " + path + "\n"
                                for path, data in files.items()))
        self.env.update(TEST_ARCHIVE=str(archive), TEST_SUMS=str(sums))
        old_curl = (self.bin / "curl").read_text().split("\n", 1)[1]
        self.write_tool("curl", r'''
output=""; previous=""
for value do
  [ "$previous" = -o ] && output=$value
  previous=$value
done
case "$*" in
  *codeload.github.com*) /bin/cp "$TEST_ARCHIVE" "$output"; exit $?;;
  *raw.githubusercontent.com*) /bin/cp "$TEST_SUMS" "$output"; exit $?;;
esac
''' + old_curl)
        self.write_tool("id", '[ "$1" = -u ] && printf "501\\n"\n')
        return installed, base, files

    def test_helper_only_update_is_not_skipped_and_distributes_all_artifacts(self):
        installed, base, files = self.prepare_update()
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            (base / name).write_text("old helper\n")
        result = subprocess.run(["/bin/bash", str(installed), "update"], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("更新完成", result.stdout)
        for path, data in files.items():
            if path != "core/claude-switch.sh":
                self.assertEqual((base / Path(path).name).read_bytes(), data)

    def test_failed_update_restores_core_and_helpers_and_removes_new_artifacts(self):
        installed, base, _ = self.prepare_update("\n# installed previous version\n")
        previous = installed.read_bytes()
        for name in ("stun-probe.py", "clash-rules.py"):
            (base / name).write_text("old " + name)
        self.audit.write_text("fail:STUN无法验证\n")
        result = subprocess.run(["/bin/bash", str(installed), "update"], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("已自动回滚", result.stdout)
        self.assertEqual(installed.read_bytes(), previous)
        for name in ("stun-probe.py", "clash-rules.py"):
            self.assertEqual((base / name).read_text(), "old " + name)
        self.assertFalse((base / "clash-global-rules.js").exists())

    def test_partial_helper_copy_failure_rolls_back_already_replaced_files(self):
        installed, base, _ = self.prepare_update()
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            (base / name).write_text("old " + name)
        self.write_tool("cp", r'''
case "$1" in */claude-switcher-main/core/clash-rules.py) exit 1;; esac
exec /bin/cp "$@"
''')
        result = subprocess.run(["/bin/bash", str(installed), "update"], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            self.assertEqual((base / name).read_text(), "old " + name)

    def test_partial_core_copy_failure_restores_the_entire_artifact_set(self):
        installed, base, _ = self.prepare_update("\n# previous core\n")
        previous = installed.read_bytes()
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            (base / name).write_text("old " + name)
        self.write_tool("cp", r'''
case "$1" in
  */claude-switcher-main/core/claude-switch.sh) printf 'partial' > "$2"; exit 1;;
esac
exec /bin/cp "$@"
''')
        result = subprocess.run(["/bin/bash", str(installed), "update"], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(installed.read_bytes(), previous)
        for name in ("stun-probe.py", "clash-rules.py", "clash-global-rules.js"):
            self.assertEqual((base / name).read_text(), "old " + name)


if __name__ == "__main__":
    unittest.main()
