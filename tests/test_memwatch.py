import contextlib
import importlib.util
import json
import os
from pathlib import Path
import fcntl
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memwatch", ROOT / "scripts/memwatch.py")
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)
TMUX = shutil.which("tmux")


@contextlib.contextmanager
def server():
    # Keep every test socket inside a private directory, never tmux-<uid>.
    with tempfile.TemporaryDirectory(prefix="memwatch-test-", dir="/tmp") as directory:
        socket = str(Path(directory) / "socket")
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        subprocess.run([TMUX, "-S", socket, "-f", "/dev/null", "new-session",
                        "-d", "-s", "test", "-x", "160", "-y", "40", "/bin/sleep", "120"],
                       env=env, check=True, capture_output=True, timeout=10)
        try:
            yield mw.Tmux(TMUX, socket)
        finally:
            subprocess.run([TMUX, "-N", "-S", socket, "kill-server"],
                           capture_output=True, timeout=10)


class PolicyTests(unittest.TestCase):
    def test_hysteresis_and_reminders(self):
        cfg = mw.config({"@memwatch-threshold": "4KiB"})
        state, due = mw.transition({}, 4096, cfg, 100)
        self.assertTrue(due)
        state, due = mw.transition(state, 3500, cfg, 1100)
        self.assertTrue(state["warning"])
        self.assertFalse(due)  # Improved below threshold: retain badge, no reminder.
        state, due = mw.transition(state, 4096, cfg, 1101)
        self.assertTrue(due)
        state, due = mw.transition(state, 3000, cfg, 1102)
        self.assertFalse(state["warning"])
        state, due = mw.transition(state, 4096, cfg, 1103)
        self.assertTrue(due)  # A new incident alerts immediately.

    def test_linux_includes_swap_and_rejects_missing_counters(self):
        self.assertEqual(mw.linux_memory("VmRSS: 1024 kB\nVmSwap: 2048 kB\n"),
                         (3 * 1024**2, "rss+swap"))
        with self.assertRaises(mw.Unavailable):
            mw.linux_memory("VmRSS: 1024 kB\n")

    def test_native_failure_is_explicit_rss_fallback(self):
        fake = subprocess.CompletedProcess([], 0, "123\n", "")
        with patch.object(mw, "mac_memory", side_effect=OSError), \
                patch.object(mw.sys, "platform", "darwin"), \
                patch.object(mw.subprocess, "run", return_value=fake):
            self.assertEqual(mw.memory(123, "auto"), (123 * 1024, "rss (fallback)"))

    def test_config_rejects_unbounded_or_invalid_inputs(self):
        for value in ("0", "-1GiB", "garbage", "999999999999999999999TiB"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mw.config({"@memwatch-threshold": value})
        with self.assertRaises(ValueError):
            mw.config({"@memwatch-interval": "nan"})

    def test_disconnected_client_does_not_prevent_other_notifications(self):
        class Connection:
            calls = []

            def guard(self, token, commands):
                self.calls.append(commands)
                if len(self.calls) == 1:
                    raise mw.Unavailable("client left")
        connection = Connection()
        mw.notify(connection, "abc", {"bytes": 100, "metric": "rss"},
                  mw.config({}), ["client1", "client2"])
        self.assertEqual(len(connection.calls), 2)


@unittest.skipUnless(TMUX, "tmux is required for integration tests")
class IntegrationTests(unittest.TestCase):
    def test_tpm_entry_point_uses_the_calling_server(self):
        with server() as t:
            env = dict(os.environ, TMUX=f"{t.socket},{t.identity()},0", TMUX_PANE="%0")
            subprocess.run([str(ROOT / "memwatch.tmux")], env=env, cwd="/tmp",
                           check=True, capture_output=True, timeout=10)
            state = mw.state_from(t.options())
            self.assertGreater(state["bytes"], 0)
            self.assertEqual(state["metric"], "footprint" if sys.platform == "darwin" else "rss+swap")

    def test_start_is_scoped_and_does_not_change_history_settings(self):
        with server() as t, server() as other:
            history = t.run("show-options", "-gv", "history-limit")
            mw.start(t)
            options = t.options()
            self.assertIn("@memwatch-generation", options)
            self.assertGreater(mw.state_from(options)["bytes"], 0)
            self.assertEqual(t.run("show-options", "-gv", "history-limit"), history)
            self.assertNotIn("@memwatch-generation", other.options())
            self.assertIn("client-attached", t.run("show-hooks", "-g", "client-attached"))

    def test_reload_invalidates_old_callbacks_and_does_not_duplicate_badge(self):
        with server() as t:
            mw.start(t)
            old = t.options()["@memwatch-generation"]
            mw.start(t)
            options = t.options()
            checks = mw.state_from(options)["checks"]
            mw.tick(t, old, t.identity())
            self.assertEqual(mw.state_from(t.options())["checks"], checks)
            self.assertEqual(options["status-right"].count("#{@memwatch_status}"), 1)
            self.assertNotEqual(options["@memwatch-generation"], old)

    def test_timer_runs_without_status_redraws_and_stop_invalidates_it(self):
        with server() as t:
            t.run("set-option", "-g", "status", "off")
            t.run("set-option", "-g", "@memwatch-interval", "0.2")
            mw.start(t)
            deadline = time.monotonic() + 5
            while mw.state_from(t.options())["checks"] < 3:
                if time.monotonic() > deadline:
                    self.fail("scheduled checks did not run")
                time.sleep(0.05)
            t.run("set-option", "-g", "@memwatch-enabled", "off")
            count = mw.state_from(t.options())["checks"]
            time.sleep(0.5)
            self.assertEqual(mw.state_from(t.options())["checks"], count)

    def test_failed_sample_keeps_warning_and_schedules_recovery(self):
        with server() as t:
            t.run("set-option", "-g", "@memwatch-threshold", "1B")
            mw.start(t)
            token = t.options()["@memwatch-generation"]
            with patch.object(mw, "memory", side_effect=mw.Unavailable("unreadable")):
                mw.tick(t, token, t.identity())
            state = mw.state_from(t.options())
            self.assertTrue(state["warning"])
            self.assertEqual(state["error"], "unreadable")
            self.assertIn("MEM ?", t.options()[mw.STATUS])
            t.run("set-option", "-g", "@memwatch-threshold", "1TiB")
            mw.tick(t, token, t.identity())
            self.assertFalse(mw.state_from(t.options())["warning"])
            self.assertEqual(t.options()[mw.STATUS], "")

    def test_report_ranks_real_history_and_deduplicates_linked_windows(self):
        with server() as t:
            script = "import sys,time;sys.stdout.write(('X'*120+'\\n')*3000);sys.stdout.flush();time.sleep(120)"
            t.run("new-window", "-d", "-t", "test", "-n", "big", sys.executable, "-c", script)
            deadline = time.monotonic() + 5
            while int(t.run("display-message", "-p", "-t", "test:big", "#{history_size}")) < 1000:
                if time.monotonic() > deadline:
                    self.fail("history writer did not finish")
                time.sleep(0.05)
            t.run("new-session", "-d", "-s", "linked", "-t", "test")
            rows = mw.panes(t)
            self.assertEqual(len(rows), 2)
            big_id = t.run("display-message", "-p", "-t", "test:big", "#{pane_id}")
            self.assertEqual(rows[0]["pane"], big_id)
            self.assertGreater(rows[0]["lines"], 1000)
            self.assertGreater(rows[0]["bytes"], rows[1]["bytes"])
            output = subprocess.check_output([sys.executable, mw.SCRIPT, "--tmux", TMUX,
                                               "--socket", t.socket, "report", "--json"], text=True)
            self.assertEqual(len(json.loads(output)["panes"]), 2)

    def test_manual_status_placement_preserves_format(self):
        with server() as t:
            t.run("set-option", "-g", "status-right", "my theme")
            t.run("set-option", "-g", "@memwatch-status-append", "off")
            mw.start(t)
            self.assertEqual(t.options()["status-right"], "my theme")

    def test_report_popup_opens_and_closes_on_an_attached_terminal(self):
        with server() as t:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
            env = dict(os.environ, TERM="xterm-256color")
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            attached = subprocess.Popen([TMUX, "-N", "-S", t.socket, "attach-session", "-t", "test"],
                                        stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            popup = None
            try:
                deadline = time.monotonic() + 5
                while not mw.clients(t):
                    if time.monotonic() > deadline:
                        self.fail("terminal did not attach")
                    if select.select([master], [], [], 0.05)[0]:
                        os.read(master, 65536)
                mw.start(t)
                client = mw.clients(t)[0]
                popup = subprocess.Popen([sys.executable, mw.SCRIPT, "--tmux", TMUX,
                                          "--socket", t.socket, "show", "--client", client],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                output = b""
                deadline = time.monotonic() + 8
                while b"Press Enter to close" not in output:
                    if time.monotonic() > deadline:
                        self.fail("popup report did not render: " + repr(output[-500:]))
                    if select.select([master], [], [], 0.1)[0]:
                        output += os.read(master, 65536)
                os.write(master, b"\r")
                _, error = popup.communicate(timeout=5)
                self.assertEqual(popup.returncode, 0, error.decode())
                self.assertIn(b"Grid bytes", output)
            finally:
                if popup and popup.poll() is None:
                    popup.kill()
                    popup.communicate()
                attached.terminate()
                try:
                    attached.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    attached.kill()
                    attached.wait()
                os.close(master)

    def test_missing_server_cannot_be_started_by_old_callback(self):
        with tempfile.TemporaryDirectory(prefix="memwatch-gone-", dir="/tmp") as directory:
            t = mw.Tmux(TMUX, str(Path(directory) / "socket"))
            with self.assertRaises(mw.Unavailable):
                mw.tick(t, "old", 1)
            self.assertFalse(Path(t.socket).exists())


if __name__ == "__main__":
    unittest.main()
