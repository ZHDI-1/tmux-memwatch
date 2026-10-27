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


@contextlib.contextmanager
def terminal(tmux, height=40, width=160):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    env = dict(os.environ, TERM="xterm-256color")
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    process = subprocess.Popen([TMUX, "-N", "-S", tmux.socket, "attach-session", "-t", "test"],
                               stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)

    class View:
        popup = None
        output = b""

        def pump(self, timeout=0.05):
            if select.select([master], [], [], timeout)[0]:
                self.output += os.read(master, 65536)

        def wait(self, predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while not predicate():
                if time.monotonic() > deadline:
                    raise AssertionError("terminal condition timed out: " + repr(self.output[-600:]))
                self.pump()

        def read_until(self, text):
            self.wait(lambda: text in self.output)
            return self.output

        def send(self, data):
            os.write(master, data)

        def open(self):
            self.output = b""
            self.popup = subprocess.Popen([sys.executable, mw.SCRIPT, "--tmux", TMUX,
                                           "--socket", tmux.socket, "show", "--client", self.client],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.read_until(b"Clear row number + Enter")

        def close_popup(self, key=b"q"):
            self.send(key)
            _, error = self.popup.communicate(timeout=5)
            if self.popup.returncode:
                raise AssertionError(error.decode())
            self.popup = None

    view = View()
    try:
        view.wait(lambda: bool(mw.clients(tmux)))
        view.client = mw.clients(tmux)[0]
        yield view
    finally:
        if view.popup and view.popup.poll() is None:
            view.popup.kill()
            view.popup.communicate()
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        os.close(master)


def history_writer(tmux, name, lines=3000, columns=120):
    script = f"import sys,time;sys.stdout.write(('X'*{columns}+'\\n')*{lines}+'MEMWATCH_READY\\n');sys.stdout.flush();time.sleep(120)"
    pane = tmux.run("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "test", "-n", name,
                    sys.executable, "-c", script)
    deadline = time.monotonic() + 5
    while "MEMWATCH_READY" not in tmux.run("capture-pane", "-p", "-t", pane):
        if time.monotonic() > deadline:
            raise AssertionError("history writer did not finish")
        time.sleep(0.03)
    return pane


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

    def test_hard_escalation_bypasses_soft_reminder_and_has_its_own_hysteresis(self):
        cfg = mw.config({"@memwatch-soft-limit": "4KiB", "@memwatch-hard-limit": "8KiB"})
        state, due = mw.transition({}, 4096, cfg, 100)
        self.assertEqual(state["level"], "soft")
        self.assertTrue(due)
        state, due = mw.transition(state, 8192, cfg, 101)
        self.assertEqual(state["level"], "hard")
        self.assertTrue(due)
        state, due = mw.transition(state, 8192, cfg, 102)
        self.assertFalse(due)
        state, due = mw.transition(state, 7000, cfg, 1100)
        self.assertEqual(state["level"], "hard")
        self.assertFalse(due)  # No new popup while improving below the hard limit.
        state, _ = mw.transition(state, 5000, cfg, 1101)
        self.assertEqual(state["level"], "soft")
        state, _ = mw.transition(state, 3000, cfg, 1102)
        self.assertEqual(state["level"], "normal")

    def test_limits_validation_and_old_threshold_alias(self):
        cfg = mw.config({"@memwatch-threshold": "12GiB"})
        self.assertEqual(cfg["soft-limit"], 12 * 1024**3)
        self.assertEqual(cfg["hard-limit"], 24 * 1024**3)
        cfg = mw.config({"@memwatch-threshold": "1B", "@memwatch-soft-limit": "4GiB"})
        self.assertEqual(cfg["soft-limit"], 4 * 1024**3)
        with self.assertRaises(ValueError):
            mw.config({"@memwatch-soft-limit": "8GiB", "@memwatch-hard-limit": "4GiB"})

    def test_hard_popup_is_not_disabled_by_soft_banner_option(self):
        class Connection:
            calls = []

            def invocation(self, *args):
                return " ".join(map(str, args))

            def guard(self, token, commands):
                self.calls.extend(commands)
        connection = Connection()
        mw.notify(connection, "abc", {"level": "hard", "server_pid": 123},
                  mw.config({"@memwatch-banner": "off"}), ["client1"])
        self.assertEqual(connection.calls[0][:2], ["run-shell", "-b"])
        self.assertIn("--warning", connection.calls[0][2])

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
            t.run("set-option", "-g", "@memwatch-soft-limit", "1TiB")
            t.run("set-option", "-g", "@memwatch-hard-limit", "2TiB")
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

    def test_popup_ignores_other_keys_and_closes_only_with_q_or_escape(self):
        with server() as t, terminal(t) as view:
            mw.start(t)
            for closing_key in (b"q", b"\x1b"):
                view.open()
                view.send(b"a\r\x03\x04\x1a\x1b[B\x1b[A")  # Enter, signals, arrows cannot close it.
                time.sleep(0.25)
                self.assertIsNone(view.popup.poll())
                self.assertIn(b"Grid bytes", view.output)
                view.close_popup(closing_key)

    def test_popup_clears_only_selected_history_and_keeps_screen_and_process(self):
        with server() as t:
            big = history_writer(t, "big", columns=120)
            small = history_writer(t, "small", lines=500, columns=20)
            other_lines = t.run("display-message", "-p", "-t", small, "#{history_size}")
            original_pid = t.run("display-message", "-p", "-t", big, "#{pane_pid}")
            screen = t.run("capture-pane", "-p", "-t", big)
            with terminal(t) as view:
                mw.start(t)
                view.open()
                view.send(b"1\r")
                view.wait(lambda: t.run("display-message", "-p", "-t", big, "#{history_size}") == "0")
                self.assertEqual(t.run("display-message", "-p", "-t", small, "#{history_size}"), other_lines)
                self.assertEqual(t.run("display-message", "-p", "-t", big, "#{pane_pid}"), original_pid)
                self.assertEqual(t.run("capture-pane", "-p", "-t", big), screen)
                view.send(b"1\r")  # Rows must not silently re-sort after clearing.
                time.sleep(0.25)
                self.assertEqual(t.run("display-message", "-p", "-t", small, "#{history_size}"), other_lines)
                self.assertIsNone(view.popup.poll())
                view.close_popup()

    def test_multi_digit_selection_and_small_terminal(self):
        with server() as t:
            for number in range(11):
                history_writer(t, "pane" + str(number), lines=150, columns=10 + number * 10)
            rows = mw.panes(t)
            target, untouched = rows[9]["pane"], rows[0]["pane"]
            original = t.run("display-message", "-p", "-t", untouched, "#{history_size}")
            with terminal(t, height=20, width=90) as view:
                mw.start(t)
                view.open()
                view.send(b"10\r")
                view.wait(lambda: t.run("display-message", "-p", "-t", target, "#{history_size}") == "0")
                self.assertEqual(t.run("display-message", "-p", "-t", untouched, "#{history_size}"), original)
                view.close_popup()

    def test_hard_limit_opens_popup_immediately_after_soft_banner(self):
        with server() as t, terminal(t) as view:
            pane = history_writer(t, "keep-during-warning")
            lines = t.run("display-message", "-p", "-t", pane, "#{history_size}")
            mw.start(t)
            token = t.options()["@memwatch-generation"]
            with patch.object(mw, "memory", return_value=(5 * 1024**3, "test")):
                mw.tick(t, token, t.identity())
            self.assertEqual(mw.state_from(t.options())["level"], "soft")
            with patch.object(mw, "memory", return_value=(9 * 1024**3, "test")):
                mw.tick(t, token, t.identity())
            output = view.read_until(b"Only q or Esc acknowledges")
            self.assertIn(b"HARD MEMORY LIMIT WARNING", output)
            self.assertEqual(mw.state_from(t.options())["level"], "hard")
            view.send(b"1\rxyz\x03")
            time.sleep(0.25)
            self.assertEqual(t.run("display-message", "-p", "-t", pane, "#{history_size}"), lines)
            view.send(b"q")

    def test_wrong_server_cannot_clear_reused_pane_id(self):
        with server() as original, server() as replacement:
            pane = history_writer(replacement, "keep")
            before = replacement.run("display-message", "-p", "-t", pane, "#{history_size}")
            with self.assertRaises(mw.Unavailable):
                mw.clear_pane(replacement, original.identity(), pane)
            self.assertEqual(replacement.run("display-message", "-p", "-t", pane, "#{history_size}"), before)

    def test_missing_server_cannot_be_started_by_old_callback(self):
        with tempfile.TemporaryDirectory(prefix="memwatch-gone-", dir="/tmp") as directory:
            t = mw.Tmux(TMUX, str(Path(directory) / "socket"))
            with self.assertRaises(mw.Unavailable):
                mw.tick(t, "old", 1)
            self.assertFalse(Path(t.socket).exists())


if __name__ == "__main__":
    unittest.main()
