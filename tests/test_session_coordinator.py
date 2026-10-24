"""Shared default-directory transfers, using isolated profiles and no real apps."""
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "core/session-coordinator.py"
spec = importlib.util.spec_from_file_location("session_coordinator", SOURCE)
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "_trash").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "_trash")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = sessions.SessionStore(self.home)
        self.store.setup()
        self.store.owner_file.write_text("upstream\n")
        self.store.data.mkdir(parents=True)
        (self.store.data / "marker").write_text("upstream live")
        self.store.parked["fork"].mkdir()
        (self.store.parked["fork"] / "marker").write_text("fork live")

    def assert_original_active(self):
        self.assertEqual(self.store.owner(), "upstream")
        self.assertEqual((self.store.data / "marker").read_text(), "upstream live")
        self.assertEqual((self.store.parked["fork"] / "marker").read_text(), "fork live")
        self.assertFalse(self.store.parked["upstream"].exists())

    def test_roundtrip_preserves_both_live_profiles_and_account_libraries(self):
        for name in (".claude-switcher", ".claude-switcher-fork"):
            p = self.home / name / "accounts/A"
            p.mkdir(parents=True); (p / "marker").write_text(name)
            (p.parent.parent / "current").write_text("A\n")
        self.store.activate("fork")
        self.assertEqual((self.store.data / "marker").read_text(), "fork live")
        self.assertEqual((self.store.parked["upstream"] / "marker").read_text(), "upstream live")
        self.store.activate("upstream")
        self.assert_original_active()
        for name in (".claude-switcher", ".claude-switcher-fork"):
            self.assertEqual((self.home / name / "accounts/A/marker").read_text(), name)
            self.assertEqual((self.home / name / "current").read_text(), "A\n")

    def test_same_edition_does_not_move_or_recreate_profile(self):
        inode = self.store.data.stat().st_ino
        self.assertFalse(self.store.activate("upstream"))
        self.assertEqual(self.store.data.stat().st_ino, inode)

    def test_prepare_moves_legacy_fork_only_after_quitting(self):
        shutil.rmtree(self.store.parked["fork"])
        self.store.legacy.mkdir()
        (self.store.legacy / "marker").write_text("fork live")
        self.store.owner_file.unlink()
        def quit_app():
            self.assertTrue(self.store.legacy.exists())
            self.assertEqual((self.store.data / "marker").read_text(), "upstream live")
        self.store.prepare(quit_app)
        self.assertFalse(self.store.legacy.exists())
        self.assert_original_active()

    def test_prepare_never_overwrites_an_existing_legacy_or_parked_profile(self):
        self.store.legacy.mkdir()
        (self.store.legacy / "marker").write_text("legacy retained")
        with self.assertRaises(sessions.SessionError):
            self.store.prepare(lambda: self.fail("should not quit"))
        self.assertEqual((self.store.legacy / "marker").read_text(), "legacy retained")
        self.assert_original_active()

    def test_unmigrated_legacy_directory_blocks_switch(self):
        self.store.legacy.mkdir()
        with self.assertRaises(sessions.SessionError): self.store.activate("fork")
        self.assert_original_active()

    def test_existing_parked_source_is_not_deleted(self):
        self.store.parked["upstream"].mkdir()
        (self.store.parked["upstream"] / "marker").write_text("recovery copy")
        with self.assertRaises(sessions.SessionError): self.store.activate("fork")
        self.assertEqual((self.store.data / "marker").read_text(), "upstream live")
        self.assertEqual((self.store.parked["upstream"] / "marker").read_text(), "recovery copy")

    def test_restore_failure_rolls_back_before_committing_owner(self):
        rename = Path.rename
        def injected(path, target):
            if path == self.store.parked["fork"] and target == self.store.data:
                raise OSError("injected restore failure")
            return rename(path, target)
        with patch.object(Path, "rename", injected):
            with self.assertRaises(sessions.SessionError): self.store.activate("fork")
        self.assert_original_active()
        self.assertFalse(self.store.journal.exists())

    def test_owner_write_failure_restores_both_profiles(self):
        write = self.store.atomic_text
        def injected(path, value):
            if path == self.store.owner_file and value == "fork\n": raise OSError("injected metadata failure")
            return write(path, value)
        with patch.object(self.store, "atomic_text", injected):
            with self.assertRaises(sessions.SessionError): self.store.activate("fork")
        self.assert_original_active()
        self.assertFalse(self.store.journal.exists())

    def test_interrupted_transfer_is_not_silently_retried(self):
        self.store.journal.write_text('{"from":"upstream","to":"fork"}')
        with self.assertRaises(sessions.SessionError): self.store.activate("fork")
        self.assert_original_active()
        self.assertTrue(self.store.journal.exists())

    def test_global_lock_blocks_another_invocation(self):
        with self.store.lock():
            result = subprocess.run([sys.executable, str(SOURCE), "prepare"],
                                    env=dict(os.environ, HOME=str(self.home)), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("另一个版本", result.stdout)
        self.assert_original_active()

    def test_failed_preflight_does_not_quit_or_move_anything(self):
        with patch.object(sessions, "other_ui_running", return_value=False), \
             patch.object(sessions, "backend_command", return_value=SimpleNamespace(returncode=1, stdout="FAIL:blocked\n")), \
             patch.object(sessions, "quit_desktop") as quit_app:
            with contextlib.redirect_stdout(io.StringIO()):
                result = sessions.run(self.store, "fork", Path("unused"), ["switch", "A"])
        self.assertEqual(result, 1)
        quit_app.assert_not_called()
        self.assert_original_active()

    def test_failed_quit_never_moves_the_profile(self):
        with patch.object(sessions, "other_ui_running", return_value=False), \
             patch.object(sessions, "backend_command", return_value=SimpleNamespace(returncode=0)), \
             patch.object(sessions, "quit_desktop", side_effect=sessions.SessionError("quit failed")):
            with self.assertRaises(sessions.SessionError):
                sessions.run(self.store, "fork", Path("unused"), ["switch", "A"])
        self.assert_original_active()

    def test_backend_only_receives_switch_after_target_profile_is_active(self):
        events = []
        def backend(path, args, lock_fd=None, capture=False):
            events.append(args[0])
            if args[0] == "switch":
                self.assertEqual(self.store.owner(), "fork")
                self.assertEqual((self.store.data / "marker").read_text(), "fork live")
            return SimpleNamespace(returncode=0)
        with patch.object(sessions, "other_ui_running", return_value=False), \
             patch.object(sessions, "backend_command", side_effect=backend), \
             patch.object(sessions, "quit_desktop", side_effect=lambda: events.append("quit")):
            self.assertEqual(sessions.run(self.store, "fork", Path("unused"), ["switch", "A"]), 0)
        self.assertEqual(events, ["check", "quit", "switch"])

    def test_release_restores_upstream_without_checking_or_launching_it(self):
        self.store.activate("fork")
        with patch.object(sessions, "other_ui_running", return_value=False), \
             patch.object(sessions, "backend_command", side_effect=AssertionError("must not launch")), \
             patch.object(sessions, "quit_desktop"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sessions.run(self.store, "fork", Path("unused"), ["release"]), 0)
        self.assert_original_active()

    def test_upstream_preflight_uses_detail_and_does_not_repeat_address_failures(self):
        self.store.activate('fork')
        output = ''.join('item%d=ok\n' % i for i in range(13)) + 'WebRTC=fail:address 8.8.8.8\n'
        captured = io.StringIO()
        with patch.object(sessions, 'other_ui_running', return_value=False), \
             patch.object(sessions, 'backend_command', return_value=SimpleNamespace(returncode=0, stdout=output)) as backend, \
             patch.object(sessions, 'quit_desktop') as quit_app, contextlib.redirect_stdout(captured):
            self.assertEqual(sessions.run(self.store, 'upstream', Path('unused'), ['switch','A']), 1)
        self.assertEqual(backend.call_args.args[1], ['checkdetail'])
        self.assertNotIn('8.8.8.8', captured.getvalue())
        self.assertIn('WebRTC', captured.getvalue())
        quit_app.assert_not_called()
        self.assertEqual(self.store.owner(), 'fork')

    def test_upstream_self_update_replaces_backend_but_not_coordinating_entry(self):
        helper = self.store.root / "session-coordinator.py"; shutil.copyfile(SOURCE, helper)
        entry = self.home / ".local/bin/claude-switch.sh"; entry.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "core/upstream-launcher.sh", entry)
        original_entry = entry.read_bytes()
        backend = self.home / ".local/share/claude-switcher-upstream/claude-switch.sh"
        backend.parent.mkdir(parents=True)
        backend.write_text('#!/bin/bash\n[ "$1" = update ] || exit 3\nprintf "# new upstream core\\n" > "$0"\n')
        result = subprocess.run(["/bin/bash", str(entry), "update"],
            env=dict(os.environ, HOME=str(self.home), CLAUDE_SWITCHER_COORDINATOR_PYTHON=sys.executable),
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(backend.read_text(), "# new upstream core\n")
        self.assertEqual(entry.read_bytes(), original_entry)
        self.assert_original_active()


if __name__ == "__main__":
    unittest.main()
