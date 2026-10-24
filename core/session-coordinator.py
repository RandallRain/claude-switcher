#!/usr/bin/env python3
"""Coordinate two account libraries using Claude's single default data directory.

Only profile directories are renamed; account snapshots and credentials are never
read. A journal keeps interrupted transfers visible rather than guessing ownership.
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time


APP_ROOT = "/Applications/Claude.app/Contents/"
EDITIONS = ("upstream", "fork")
MUTATIONS = {"switch", "fix", "set-region", "update", "write-global-rules", "write-merge-rules", "release"}


class SessionError(Exception):
    pass


class SessionStore:
    def __init__(self, home):
        self.home = Path(home)
        self.root = self.home / ".claude-switcher-sessions"
        self.data = self.home / "Library/Application Support/Claude"
        self.legacy = self.home / "Library/Application Support/Claude-fork"
        self.owner_file = self.root / "active-edition"
        self.journal = self.root / "transfer.json"
        self.parked = {name: self.root / "parked" / name for name in EDITIONS}

    def setup(self):
        if self.root.is_symlink():
            raise SessionError("协调目录不能是软链接")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if (self.root / "parked").is_symlink():
            raise SessionError("现场暂存目录不能是软链接")
        (self.root / "parked").mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def lock(self):
        self.setup()
        fd = os.open(self.root / "operation.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SessionError("另一个版本正在切换、修复或更新,请稍后重试") from None
            yield fd
        finally:
            os.close(fd)

    def atomic_text(self, path, value):
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".session-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def owner(self):
        try:
            value = self.owner_file.read_text().strip()
        except FileNotFoundError:
            raise SessionError("共享目录尚未初始化,请重新运行 fork 安装器") from None
        if value not in EDITIONS:
            raise SessionError("活动版本标记异常,已停止操作")
        return value

    def ready(self):
        if self.journal.exists():
            raise SessionError("发现中断的版本交接(transfer.json),现场已保留,请先恢复再继续")
        if self.legacy.exists():
            raise SessionError("旧个人版活动目录尚未迁移,请重新运行 fork 安装器")
        self.owner()

    @staticmethod
    def valid_profile(path):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise SessionError("活动或暂存数据路径异常,已停止操作")

    def prepare(self, quit_claude):
        """The pre-upgrade default profile belongs to upstream; migrate only fork."""
        if self.journal.exists():
            raise SessionError("发现未完成的版本交接,安装器未迁移任何数据")
        if self.owner_file.exists():
            self.owner()
        self.valid_profile(self.data)
        self.valid_profile(self.legacy)
        self.valid_profile(self.parked["fork"])
        if self.legacy.exists() and self.parked["fork"].exists():
            raise SessionError("旧个人版目录和暂存现场同时存在,拒绝覆盖")
        if not self.owner_file.exists() or self.legacy.exists():
            quit_claude()
        if not self.owner_file.exists():
            self.atomic_text(self.owner_file, "upstream\n")
        if self.legacy.exists():
            if self.owner() != "upstream":
                raise SessionError("迁移时的活动版本异常,旧个人版数据已保留")
            self.legacy.rename(self.parked["fork"])

    def activate(self, target):
        self.ready()
        if target not in EDITIONS:
            raise SessionError("未知版本")
        previous = self.owner()
        if previous == target:
            return False
        source = self.parked[previous]
        destination = self.parked[target]
        for path in (self.data, source, destination):
            self.valid_profile(path)
        if source.exists():
            raise SessionError("当前版本已有未恢复的暂存现场,拒绝覆盖")
        had_source, had_target = self.data.exists(), destination.exists()
        self.atomic_text(self.journal, json.dumps({"from": previous, "to": target,
                         "had_source": had_source, "had_target": had_target}) + "\n")
        moved_source = installed_target = False
        try:
            if had_source:
                self.data.rename(source)
                moved_source = True
            if had_target:
                destination.rename(self.data)
            else:
                self.data.parent.mkdir(parents=True, exist_ok=True)
                self.data.mkdir(mode=0o700)
            installed_target = True
            self.atomic_text(self.owner_file, target + "\n")
        except OSError:
            try:
                if installed_target:
                    if had_target:
                        self.data.rename(destination)
                    else:
                        self.data.rmdir()  # Never delete nonempty profile data.
                if moved_source:
                    source.rename(self.data)
                self.atomic_text(self.owner_file, previous + "\n")
                self.journal.unlink()
            except OSError:
                raise SessionError("版本交接失败,现场与 transfer.json 已保留,请先恢复") from None
            raise SessionError("版本交接失败,原活动目录已恢复") from None
        self.journal.unlink()
        return True


def desktop_pids():
    result = subprocess.run(["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,command="],
                            capture_output=True, text=True, check=True)
    pids = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and fields[1].startswith(APP_ROOT):
            pids.append(int(fields[0]))
    return pids


def quit_desktop():
    if not desktop_pids():
        return
    try:
        subprocess.run(["osascript", "-e", 'tell application "Claude" to quit'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except subprocess.TimeoutExpired:
        pass
    for _ in range(10):
        if not desktop_pids():
            return
        time.sleep(0.2)
    for pid in desktop_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not desktop_pids():
            return
        time.sleep(0.1)
    raise SessionError("Claude 未完全退出,未交换活动目录,请手动退出后重试")


def other_ui_running(edition):
    name = "ClaudeSwitcher" if edition == "fork" else "ClaudeSwitcher-fork"
    return subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def backend_command(backend, args, lock_fd=None, capture=False):
    env = dict(os.environ, CLAUDE_SWITCHER_SESSION_INTERNAL="1")
    return subprocess.run(["/bin/bash", str(backend), *args], env=env,
                          pass_fds=() if lock_fd is None else (lock_fd,),
                          capture_output=capture, text=True)


def run(store, edition, backend, args):
    command = args[0] if args else ""
    if command not in MUTATIONS:
        return backend_command(backend, args).returncode
    with store.lock() as lock_fd:
        store.ready()
        if command != "update" and other_ui_running(edition):
            raise SessionError("请先退出另一个版本的切换器,再操作共享 Claude 目录")
        if command == "release":
            if edition != "fork":
                raise SessionError("release 仅用于个人版交还默认活动目录")
            if store.owner() == "fork":
                quit_desktop()
                store.activate("upstream")
            print("OK:默认活动目录已交还原版,未启动 Claude")
            return 0
        if command == "switch":
            if len(args) != 2 or args[1] not in ("A", "B"):
                raise SessionError("账号只能为 A 或 B")
            # Check before closing applications or moving either profile.
            # Upstream check may log address-bearing failures; use the same
            # detailed audit as its GUI and report only failed item names here.
            audit = "checkdetail" if edition == "upstream" else "check"
            result = backend_command(backend, [audit], lock_fd, capture=True)
            if edition == "upstream":
                rows = re.findall(r"^([^#=]+)=(ok|warn|fail)(?=:|$)", result.stdout or "", re.M)
                failed = [name for name, state in rows if state == "fail"]
                if result.returncode or len(rows) != 14 or failed:
                    print("FAIL:原版启动安检未通过:" + ("、".join(failed) if failed else "结果不完整"))
                    return 1
            elif result.returncode:
                sys.stdout.write(result.stdout or "FAIL:启动安检未通过\n")
                return result.returncode
            if store.owner() != edition:
                quit_desktop()
                store.activate(edition)
        elif command not in ("update",) and store.owner() != edition and desktop_pids():
            raise SessionError("Claude 正由另一个版本使用,未修改共享系统设置")
        return backend_command(backend, args, lock_fd).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare")
    dispatch = sub.add_parser("run")
    dispatch.add_argument("edition", choices=EDITIONS)
    dispatch.add_argument("backend", type=Path)
    dispatch.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    store = SessionStore(Path.home())
    try:
        if args.action == "prepare":
            with store.lock():
                store.prepare(quit_desktop)
            print("OK:共享活动目录已初始化,两版账号库未迁移")
            return 0
        return run(store, args.edition, args.backend, args.args)
    except (SessionError, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, SessionError) else "文件或进程操作失败,未启动 Claude"
        print("FAIL:" + message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
