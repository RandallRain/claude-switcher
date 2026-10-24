"""Global script preservation, per-subscription policies and live-rule checks."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clash_rules", ROOT / "core/clash-rules.py")
rules = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rules)
NODE = shutil.which("node")


def evaluate_script(script, config, profile="Subscription A", twice=False):
    harness = r'''
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const context = {config: input.config, profile: input.profile};
vm.createContext(context);
vm.runInContext(input.script, context, {timeout: 1000});
vm.runInContext("config = main(config, profile)", context, {timeout: 1000});
if (input.twice) vm.runInContext("config = main(config, profile)", context, {timeout: 1000});
process.stdout.write(JSON.stringify(context.config));
'''
    result = subprocess.run([NODE, "-e", harness], input=json.dumps(
        {"script": script, "config": config, "profile": profile, "twice": twice}),
        text=True, capture_output=True, timeout=5)
    if result.returncode:
        raise RuntimeError("generated JavaScript failed")
    return json.loads(result.stdout)


def config(group="Proxy A"):
    return {"proxy-groups": [{"name": group, "type": "select", "proxies": ["SG"]}],
            "proxies": [{"name": "SG", "type": "trojan", "udp": True}],
            "rules": ["DOMAIN-SUFFIX,bilibili.com,DIRECT", "MATCH," + group]}


def runtime_proxies(policy="Proxy A"):
    return {policy: {"type": "Selector", "udp": True, "now": "SG"},
            "SG": {"type": "Trojan", "udp": True},
            "DIRECT": {"type": "Direct", "udp": True}}


def api_rules(values):
    types = {"DOMAIN-SUFFIX": "DomainSuffix", "IP-CIDR": "IPCIDR", "IP-CIDR6": "IPCIDR",
             "DST-PORT": "DstPort", "NETWORK": "Network"}
    result = []
    for value in values:
        parts = value.split(",")
        if parts[0] == "MATCH":
            result.append({"type": "Match", "payload": "", "proxy": parts[1]})
        else:
            result.append({"type": types[parts[0]], "payload": parts[1], "proxy": parts[2]})
    return result


@unittest.skipUnless(NODE, "Node.js is required to execute the generated Global Extend Script")
class GlobalScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "profiles/Script.js"
        self.path.parent.mkdir()

    def test_existing_main_helpers_and_profile_argument_are_preserved(self):
        original = 'const label = "kept";\nfunction helper(x) { return x + "!"; }\nfunction main(config, profileName) { config.custom = helper(label + profileName); return config; }\n'
        output = evaluate_script(rules.render_script(original, "Proxy A"), config())
        self.assertEqual(output["custom"], "keptSubscription A!")
        self.assertIn("DOMAIN-SUFFIX,bilibili.com,DIRECT", output["rules"])
        self.assertLess(output["rules"].index("DST-PORT,3478,Proxy A"), output["rules"].index("DOMAIN-SUFFIX,bilibili.com,DIRECT"))

    def test_const_arrow_entrypoint_also_survives_wrapper(self):
        original = 'const main = (cfg, name) => { cfg.profile = name; return cfg; };\n'
        output = evaluate_script(rules.render_script(original, "Proxy A"), config(), "B")
        self.assertEqual(output["profile"], "B")

    def test_user_script_error_is_not_silently_discarded(self):
        with self.assertRaises(RuntimeError):
            evaluate_script(rules.render_script('function main() { throw new Error("user error"); }\n', "Proxy A"), config())

    def test_provider_based_subscription_group_does_not_need_inline_nodes(self):
        original = {"proxy-groups": [{"name": "Provider group", "type": "select", "use": ["provider"]}],
                    "proxy-providers": {"provider": {}}, "rules": ["MATCH,DIRECT"]}
        output = evaluate_script(rules.render_script("", "missing"), original)
        self.assertIn("DST-PORT,3478,Provider group", output["rules"])

    def test_each_subscription_resolves_its_own_policy(self):
        script = rules.render_script("", "Proxy A")
        for group in ("Proxy A", "另一订阅的节点组"):
            with self.subTest(group=group):
                output = evaluate_script(script, config(group))
                for port in (3478, 5349, 19302):
                    self.assertIn("DST-PORT,%d,%s" % (port, group), output["rules"])
                    self.assertIn("DST-PORT,%d,REJECT" % port, output["rules"])

    def test_policy_names_with_quotes_and_backslashes_are_not_executed_as_javascript(self):
        group = '节点 "quoted" \\path'
        output = evaluate_script(rules.render_script("", group), config(group))
        self.assertIn("DST-PORT,3478," + group, output["rules"])

    def test_direct_group_is_not_selected_as_fallback(self):
        original = config("Other")
        original["proxy-groups"].insert(0, {"name": "🎯 全球直连", "type": "select", "proxies": ["DIRECT"]})
        output = evaluate_script(rules.render_script("", "missing"), original)
        self.assertIn("DST-PORT,3478,Other", output["rules"])

    def test_leaf_node_fallback_and_no_proxy_fail_closed(self):
        original = config()
        original["proxy-groups"] = []
        output = evaluate_script(rules.render_script("", "missing"), original)
        self.assertIn("DST-PORT,3478,SG", output["rules"])
        output = evaluate_script(rules.render_script("", "missing"), {"rules": ["MATCH,DIRECT"]})
        self.assertEqual(output["rules"][0], "DOMAIN-SUFFIX,claude.ai,REJECT")
        self.assertIn("DST-PORT,3478,REJECT", output["rules"])

    def test_direct_only_or_cyclic_groups_cannot_become_protected_policy(self):
        original = config()
        original["proxy-groups"] = [{"name": "Proxy A", "type": "select", "proxies": ["DIRECT"]},
                                    {"name": "cycle", "type": "select", "proxies": ["cycle"]}]
        output = evaluate_script(rules.render_script("", "Proxy A"), original)
        self.assertIn("DST-PORT,3478,SG", output["rules"])

    def test_install_is_idempotent_backed_up_and_private(self):
        original = "function main(config) { return config; }\n"
        self.path.write_text(original)
        self.assertTrue(rules.install(self.root, "Proxy A"))
        once = self.path.read_text()
        self.assertFalse(rules.install(self.root, "Proxy A"))
        self.assertEqual(self.path.read_text(), once)
        backups = list(self.path.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(rules.user_script(once), original)

    def test_repeated_main_does_not_duplicate_rules(self):
        script = rules.render_script("", "Proxy A")
        once = evaluate_script(script, config())
        twice = evaluate_script(script, config(), twice=True)
        self.assertEqual(once["rules"], twice["rules"])

    def test_reinstall_can_update_policy_without_nesting_or_losing_user_code(self):
        original = 'function main(config) { config.custom = 42; return config; }\n'
        self.path.write_text(original)
        rules.install(self.root, "Proxy A")
        rules.install(self.root, "Other")
        script = self.path.read_text()
        self.assertEqual(script.count(rules.USER_BEGIN), 1)
        self.assertEqual(rules.user_script(script), original)
        self.assertEqual(evaluate_script(script, config("Other"))["custom"], 42)

    def test_conflicting_markers_do_not_overwrite_user_script(self):
        original = "// custom\n" + rules.BEGIN
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            rules.install(self.root, "Proxy A")
        self.assertEqual(self.path.read_text(), original)
        self.assertEqual(list(self.path.parent.glob("*.bak")), [])

    def test_runtime_requires_all_ports_fallbacks_and_real_udp_proxy(self):
        generated = evaluate_script(rules.render_script("", "Proxy A"), config())
        active = api_rules(generated["rules"])
        proxies = runtime_proxies()
        self.assertTrue(rules.rules_loaded(active, proxies))
        for payload, policy in (("5349", "Proxy A"), ("19302", "REJECT")):
            incomplete = [r for r in active if not (r["type"] == "DstPort" and r["payload"] == payload and r["proxy"] == policy)]
            self.assertFalse(rules.rules_loaded(incomplete, proxies))
        direct = copy.deepcopy(proxies)
        direct["Proxy A"]["now"] = "DIRECT"
        self.assertFalse(rules.rules_loaded(active, direct))
        no_udp = copy.deepcopy(proxies)
        no_udp["SG"]["udp"] = False
        self.assertFalse(rules.rules_loaded(active, no_udp))

    def test_subscriber_override_before_global_rules_is_not_accepted(self):
        active = api_rules(evaluate_script(rules.render_script("", "Proxy A"), config())["rules"])
        for preceding in ({"type": "Network", "payload": "udp", "proxy": "DIRECT"},
                          {"type": "DomainSuffix", "payload": "bilibili.com", "proxy": "DIRECT"},
                          {"type": "IPCIDR", "payload": "0.0.0.0/0", "proxy": "DIRECT"}):
            with self.subTest(preceding=preceding):
                self.assertFalse(rules.rules_loaded([preceding] + active, runtime_proxies()))

    def test_private_lan_bypass_before_global_rules_is_preserved(self):
        active = api_rules(evaluate_script(rules.render_script("", "Proxy A"), config())["rules"])
        bypass = {"type": "IPCIDR", "payload": "100.64.0.0/10", "proxy": "DIRECT"}
        self.assertTrue(rules.rules_loaded([bypass] + active, runtime_proxies()))

    def test_preexisting_subscription_copies_of_protected_rules_are_compatible(self):
        active = api_rules(evaluate_script(rules.render_script("", "Proxy A"), config())["rules"])
        older = api_rules(["DST-PORT,3478,Proxy A", "DST-PORT,5349,Proxy A", "DST-PORT,19302,Proxy A",
                           "DST-PORT,3478,REJECT", "DST-PORT,5349,REJECT", "DST-PORT,19302,REJECT"])
        self.assertTrue(rules.rules_loaded(older + active, runtime_proxies()))

    def test_verify_never_accepts_old_merge_text_as_live_rules(self):
        (self.path.parent / "Merge.yaml").write_text("prepend-rules:\n  - DST-PORT,3478,Proxy A\n")
        class Controller:
            def get(self, path):
                return {"rules": [], "proxies": runtime_proxies()}
        with self.assertRaises(OSError):
            rules.verify(self.root, "Proxy A", Controller())
        rules.install(self.root, "Proxy A")
        self.assertFalse(rules.verify(self.root, "Proxy A", Controller()))

    def test_same_country_direct_selection_cannot_be_a_trusted_proxy_reference(self):
        class Controller:
            def get(self, path):
                proxies = runtime_proxies()
                proxies["Proxy A"]["now"] = "DIRECT"
                return {"rules": [], "proxies": proxies}
        self.assertFalse(rules.proxy_check(self.root, "Proxy A", Controller()))


class ControllerParsingTests(unittest.TestCase):
    def test_controller_auth_stays_in_local_header_and_connection_is_closed(self):
        controller = object.__new__(rules.Controller)
        controller.unix = "/fake-controller.sock"
        controller.address = ""
        controller.secret = "test-secret"
        conn = mock.MagicMock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = b'{"rules": []}'
        with mock.patch.object(rules, "UnixConnection", return_value=conn):
            self.assertEqual(controller.get("/rules"), {"rules": []})
        conn.request.assert_called_once_with("GET", "/rules", headers={"Authorization": "Bearer test-secret"})
        conn.close.assert_called_once_with()
        self.assertEqual(rules.UnixConnection("/fake-controller.sock").timeout, 2)

    def test_top_level_controller_values_do_not_parse_nested_or_echo_credentials(self):
        text = 'secret: "a:b#c"\nexternal-controller: 127.0.0.1:9090\ndns:\n  secret: other\n'
        self.assertEqual(rules.yaml_scalar(text, "secret"), "a:b#c")
        self.assertEqual(rules.yaml_scalar("secret: 'a''b'\n", "secret"), "a'b")
        self.assertEqual(rules.yaml_scalar("  secret: nested\n", "secret"), "")
        self.assertEqual(rules.yaml_scalar("secret:\nmode: rule\n", "secret"), "")
        self.assertEqual(rules.yaml_scalar("secret: null\n", "secret"), "")


if __name__ == "__main__":
    unittest.main()
