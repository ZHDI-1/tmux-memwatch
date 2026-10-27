#!/usr/bin/env python3
"""tmux memory alerts and interactive, explicitly selected history cleanup."""

import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid


PREFIX = "@memwatch-"
STATUS = "@memwatch_status"
SCRIPT = str(Path(__file__).resolve())
DEFAULTS = {
    "enabled": "on", "soft-limit": "4GiB", "hard-limit": "8GiB", "interval": "60",
    "reminder": "900", "reset-percent": "75", "top": "5",
    "metric": "auto", "status": "on", "status-append": "on", "banner": "on", "key": "M-m",
}


class Unavailable(RuntimeError):
    pass


def clean(text):
    return "".join(c if not unicodedata.category(c).startswith("C") else " "
                   for c in str(text))


def size_label(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def parse_size(text):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", text, re.I)
    if not match:
        raise ValueError("threshold must be a size such as 4GiB")
    number, unit = match.groups()
    units = {"": 1, "b": 1}
    for power, prefix in enumerate("kmgt", 1):
        for suffix in ("", "b", "ib"):
            units[prefix + suffix] = 1024 ** power
    if unit.lower() not in units:
        raise ValueError("unknown threshold unit")
    value = int(float(number) * units[unit.lower()])
    if not 0 < value < 2 ** 63:
        raise ValueError("threshold must be positive and below 8EiB")
    return value


def config(options):
    result = {key: options.get(PREFIX + key, default)
              for key, default in DEFAULTS.items()}
    # Keep the original threshold option as an alias until explicitly migrated.
    if PREFIX + "soft-limit" not in options and PREFIX + "threshold" in options:
        result["soft-limit"] = options[PREFIX + "threshold"]
    result["soft-limit"] = parse_size(result["soft-limit"])
    result["hard-limit"] = (parse_size(result["hard-limit"])
                            if PREFIX + "hard-limit" in options
                            else result["soft-limit"] * 2)
    if result["hard-limit"] <= result["soft-limit"]:
        raise ValueError("hard-limit must be greater than soft-limit")
    result["threshold"] = result["soft-limit"]  # Legacy Python-facing alias.
    for key in ("interval", "reminder"):
        result[key] = float(result[key])
        if not 0.1 <= result[key] <= 86400:
            raise ValueError(key + " must be between 0.1 and 86400 seconds")
    for key, low, high in (("top", 1, 50), ("reset-percent", 1, 99)):
        result[key] = int(result[key])
        if not low <= result[key] <= high:
            raise ValueError(key + " is out of range")
    for key in ("enabled", "status", "status-append", "banner"):
        if result[key] not in ("on", "off"):
            raise ValueError(key + " must be on or off")
    if result["metric"] not in ("auto", "rss"):
        raise ValueError("metric must be auto or rss")
    return result


def linux_memory(text):
    fields = {}
    for line in text.splitlines():
        match = re.match(r"^(VmRSS|VmSwap):\s+(\d+)\s+kB$", line)
        if match:
            fields[match[1]] = int(match[2]) * 1024
    if "VmRSS" not in fields or "VmSwap" not in fields:
        raise Unavailable("Linux memory counters unavailable")
    return fields["VmRSS"] + fields["VmSwap"], "rss+swap"


def mac_memory(pid):
    # Layout from macOS <sys/resource.h>, RUSAGE_INFO_V0.
    class Usage(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
            (name, ctypes.c_uint64) for name in (
                "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups",
                "pageins", "wired_size", "resident_size", "phys_footprint",
                "proc_start_abstime", "proc_exit_abstime")]
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_pid_rusage.restype = ctypes.c_int
    usage = Usage()
    if lib.proc_pid_rusage(pid, 0, ctypes.byref(usage)):
        raise OSError(ctypes.get_errno(), "proc_pid_rusage failed")
    return usage.phys_footprint, "footprint"


def memory(pid, metric):
    if metric == "auto":
        try:
            if sys.platform == "darwin":
                return mac_memory(pid)
            if sys.platform.startswith("linux"):
                return linux_memory(Path(f"/proc/{pid}/status").read_text())
        except (OSError, AttributeError, Unavailable):
            pass
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "rss="],
                                capture_output=True, text=True, timeout=5, check=True)
        return int(result.stdout.strip()) * 1024, "rss" if metric == "rss" else "rss (fallback)"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Unavailable("could not read server memory") from error


class Tmux:
    def __init__(self, binary, socket=None):
        self.binary = binary
        self.socket = socket

    def run(self, *args, timeout=15):
        argv = [self.binary, "-N"]
        if self.socket:
            argv += ["-S", self.socket]
        try:
            result = subprocess.run(argv + list(args), capture_output=True,
                                    text=True, timeout=timeout, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise Unavailable("tmux server or command unavailable") from error
        return result.stdout.strip()

    def identity(self):
        value = self.run("display-message", "-p", "#{pid}\t#{socket_path}")
        pid, self.socket = value.split("\t", 1)
        return int(pid)

    def options(self):
        result = {}
        for line in self.run("show-options", "-g").splitlines():
            key, _, value = line.partition(" ")
            if key.startswith(PREFIX) or key in (STATUS, "status-right", "status-right-length"):
                words = shlex.split(value)
                result[key] = words[0] if words else ""
        return result

    def set_commands(self, values):
        return [["set-option", "-g", key, str(value)] for key, value in values.items()]

    def commands(self, commands):
        argv = []
        for command in commands:
            if argv:
                argv.append(";")
            argv.extend(command)
        if argv:
            self.run(*argv)

    def guard(self, generation, commands):
        # The condition and following commands are evaluated inside the server.
        # A callback from a previous config load cannot replace state or rearm.
        expression = "#{==:#{@memwatch-generation}," + generation + "}"
        body = " ; ".join(shlex.join(command) for command in commands)
        self.run("if-shell", "-F", expression, body)

    def invocation(self, command, *args):
        return shlex.join([sys.executable, SCRIPT, "--tmux", self.binary,
                           "--socket", self.socket, command, *map(str, args)])


def state_from(options):
    try:
        state = json.loads(options.get(PREFIX + "state", "{}"))
        return state if isinstance(state, dict) else {}
    except ValueError:
        return {}


def transition(previous, value, cfg, now):
    before = previous.get("level", "soft" if previous.get("warning") else "normal")
    reset = cfg["reset-percent"] / 100
    if value >= cfg["hard-limit"] or (before == "hard" and value >= cfg["hard-limit"] * reset):
        level = "hard"
    elif value >= cfg["soft-limit"] or (before != "normal" and value >= cfg["soft-limit"] * reset):
        level = "soft"
    else:
        level = "normal"
    trigger = cfg["hard-limit"] if level == "hard" else cfg["soft-limit"]
    last = previous.get("last_" + level + "_alert",
                        previous.get("last_alert", 0) if level == "soft" else 0)
    entered = (level == "hard" and before != "hard") or (level == "soft" and before == "normal")
    due = level != "normal" and value >= trigger and (entered or now - last >= cfg["reminder"])
    state = dict(previous, warning=level != "normal", level=level, bytes=value, sampled_at=time.time(),
                 error="", checks=previous.get("checks", 0) + 1)
    if due:
        state["last_alert"] = now
        state["last_" + level + "_alert"] = now
    if level == "normal":
        state["top"] = []
    return state, due


def panes(tmux):
    fmt = "#{pane_id}\t#{history_bytes}\t#{history_size}\t#{session_name}:#{window_index}.#{pane_index} (#{pane_current_command})"
    unique = {}
    for line in tmux.run("list-panes", "-a", "-F", fmt).splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4 or not re.fullmatch(r"%\d+", parts[0]):
            continue
        pane, size, lines, label = parts
        unique[pane] = dict(pane=pane, bytes=int(size), lines=int(lines), label=clean(label))
    return sorted(unique.values(), key=lambda p: p["bytes"], reverse=True)


def clients(tmux):
    result = []
    for line in tmux.run("list-clients", "-F", "#{client_name}\t#{client_control_mode}").splitlines():
        name, _, control = line.partition("\t")
        if name and control == "0":
            result.append(name)
    return result


def badge(state, cfg):
    if cfg["status"] == "off":
        return ""
    if state.get("error"):
        return "#[fg=yellow]MEM ?#[default]"
    if state.get("warning"):
        hard = state.get("level") == "hard"
        style = "red,bold" if hard else "yellow,bold"
        label = "MEM HARD " if hard else "MEM "
        return "#[fg=" + style + "]" + label + size_label(state["bytes"]) + "#[default]"
    return ""


def notify(tmux, generation, state, cfg, targets=None):
    hard = state.get("level") == "hard"
    if not hard and cfg["banner"] == "off":
        return
    try:
        targets = clients(tmux) if targets is None else targets
    except Unavailable:
        return
    message = f"tmux memory: {size_label(state.get('bytes', 0))} ({state.get('metric', '?')}); soft limit {size_label(cfg['soft-limit'])}."
    top = state.get("top", [])
    if top:
        message += f" Largest history: {top[0]['pane']} {size_label(top[0]['bytes'])}."
    message += " Inspect pane histories before manual cleanup."
    flags = ["-C"] if state.get("live_message") else []
    # Older tmux versions lack -C; use a shorter, key-dismissible message.
    delay = "10000" if flags else "2000"
    for client in targets:
        try:
            if hard:
                command = tmux.invocation("show", "--client", client, "--warning",
                                          "--server-pid", state["server_pid"])
                tmux.guard(generation, [["run-shell", "-b", command]])
            else:
                tmux.guard(generation, [["display-message", *flags, "-d", delay,
                                         "-c", client, "-l", message]])
        except Unavailable:
            pass  # A disconnect must not stop the scheduled checks.


def tick(tmux, generation, expected_pid):
    if tmux.identity() != expected_pid:
        return
    options = tmux.options()
    if options.get(PREFIX + "generation") != generation:
        return
    previous = state_from(options)
    cfg = dict(DEFAULTS)
    due = False
    try:
        cfg = config(options)
        if cfg["enabled"] == "off":
            tmux.guard(generation, tmux.set_commands({STATUS: ""}))
            return
        value, metric = memory(expected_pid, cfg["metric"])
        state, due = transition(previous, value, cfg, time.monotonic())
        state["metric"] = metric
        state["server_pid"] = expected_pid
        if due and state["level"] == "soft":
            try:
                state["top"] = panes(tmux)[:cfg["top"]]
            except Unavailable:
                state["top"] = []
    except (ValueError, Unavailable, OverflowError) as error:
        # Keep an existing warning on an unreadable sample. Never turn it into 0.
        cfg = config({})
        state = dict(previous, error=clean(error), sampled_at=time.time())
    values = {PREFIX + "state": json.dumps(state, separators=(",", ":")),
              STATUS: badge(state, cfg)}
    schedule = ["run-shell", "-b", "-d", str(cfg["interval"]),
                tmux.invocation("tick", "--generation", generation,
                                "--server-pid", expected_pid)]
    # Rearm before delivering banners: notification failure cannot kill monitoring.
    tmux.guard(generation, tmux.set_commands(values) + [schedule])
    if due:
        notify(tmux, generation, state, cfg)


def start(tmux):
    pid = tmux.identity()
    options = tmux.options()
    cfg = config(options)
    generation = uuid.uuid4().hex
    state = state_from(options)
    usage = next((line for line in tmux.run("list-commands").splitlines()
                  if line.startswith("display-message ")), "")
    match = re.search(r"\[(-[a-zA-Z]+)\]", usage)
    state["live_message"] = bool(match and "C" in match.group(1))
    values = {PREFIX + "generation": generation,
              PREFIX + "state": json.dumps(state, separators=(",", ":"))}
    for name, value in DEFAULTS.items():
        if PREFIX + name not in options:
            values[PREFIX + name] = str(cfg[name]) if name in ("soft-limit", "hard-limit") else value
    if cfg["status"] == "on" and cfg["status-append"] == "on" and "#{" + STATUS + "}" not in options.get("status-right", ""):
        values["status-right"] = options.get("status-right", "") + " #{" + STATUS + "}"
    if cfg["status"] == "on" and cfg["status-append"] == "on":
        values["status-right-length"] = max(80, int(options.get("status-right-length", "40")))
    commands = tmux.set_commands(values)
    attached = tmux.invocation("attached", "--client", "#{client_name}")
    hook = shlex.join(["run-shell", "-b", attached])
    commands.append(["set-hook", "-g", "client-attached[8341]", hook])
    if cfg["key"]:
        binding = tmux.invocation("show", "--client", "#{client_name}")
        try:
            existing = tmux.run("list-keys", "-T", "prefix", cfg["key"])
        except Unavailable:
            existing = ""
        if not existing or SCRIPT in existing:
            commands.append(["bind-key", "-T", "prefix", cfg["key"], "run-shell", "-b", binding])
    tmux.commands(commands)
    tick(tmux, generation, pid)


def attached(tmux, client):
    tmux.identity()
    options = tmux.options()
    cfg = config(options)
    state = state_from(options)
    if cfg["enabled"] == "on" and state.get("warning"):
        notify(tmux, options.get(PREFIX + "generation", ""), state, cfg, [client])


def report(tmux, as_json=False):
    pid = tmux.identity()
    options = tmux.options()
    cfg = config(options)
    value, metric = memory(pid, cfg["metric"])
    rows = panes(tmux)
    if as_json:
        print(json.dumps(dict(pid=pid, socket=tmux.socket, bytes=value,
                             metric=metric, panes=rows), indent=2))
    else:
        print(f"tmux server {pid}: {size_label(value)} [{metric}]")
        print(f"Soft: {size_label(cfg['soft-limit'])}; hard: {size_label(cfg['hard-limit'])}; socket: {clean(tmux.socket)}\n")
        print(f"{'PANE':<8} {'GRID':>12} {'LINES':>10}  LOCATION (COMMAND)")
        for row in rows:
            print(f"{row['pane']:<8} {size_label(row['bytes']):>12} {row['lines']:>10,}  {row['label']}")
        print("\nGrid bytes include visible rows and exclude allocator overhead.")
        print("Pane applications use separate memory. Clearing history may not immediately reduce RSS.")
        print("\nInteractive cleanup: open the popup, type a row number, then Enter.")
        print("Only explicit selection clears history; history-limit and pane applications are preserved.")
        print("Manual cleanup: tmux -S <socket-above> clear-history -t <pane-id>")


def clear_pane(tmux, expected_pid, pane):
    if not re.fullmatch(r"%\d+", pane):
        raise ValueError("invalid pane ID")
    # Check inside the server, not just before the RPC: a replacement server
    # could reuse pane IDs while a user is looking at an older report.
    condition = "#{==:#{pid}," + str(expected_pid) + "}"
    marker = "memwatch-server-changed"
    result = tmux.run("if-shell", "-F", condition,
                      shlex.join(["clear-history", "-t", pane]),
                      shlex.join(["display-message", "-p", marker]))
    if marker in result:
        raise Unavailable("server changed; no history was cleared")


def interactive(tmux, expected_pid=None, warning=False):
    import curses

    if not sys.stdin.isatty():
        raise Unavailable("interactive report needs a terminal; use report --json")
    pid = tmux.identity()
    if expected_pid is not None and pid != expected_pid:
        raise Unavailable("server changed; open a new report")
    cfg = config(tmux.options())
    allow_cleanup = not warning  # An automatic popup must not turn ongoing typing into a clear.
    rows, value, metric = [], 0, "loading"
    os.environ.setdefault("ESCDELAY", "25")  # Also works on Python 3.8.

    def run(screen):
        nonlocal rows, value, metric, cfg, warning
        curses.raw()  # Ctrl-C is input to ignore, not a signal that closes the UI.
        screen.keypad(True)
        screen.timeout(250)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        entry, offset, message = "", 0, ""

        def refresh(reorder):
            nonlocal rows, value, metric, cfg
            if tmux.identity() != pid:
                raise Unavailable("server changed; close and reopen this report")
            cfg = config(tmux.options())
            fresh = panes(tmux)
            value, metric = memory(pid, cfg["metric"])
            if reorder:
                rows = fresh
            else:
                by_id = {row["pane"]: row for row in fresh}
                # A clear does not renumber the rows under the user's fingers.
                rows = [by_id.get(row["pane"], dict(row, closed=True)) for row in rows]

        height, width = screen.getmaxyx()
        try:
            screen.addnstr(0, 0, "HARD MEMORY LIMIT WARNING" if warning else "tmux memory - pane histories",
                           max(0, width - 1), curses.A_BOLD)
            screen.addnstr(1, 0, "Loading pane histories...", max(0, width - 1))
        except curses.error:
            pass
        screen.refresh()
        try:
            refresh(True)
        except (Unavailable, ValueError) as error:
            message = clean(error)
        last_sample = time.monotonic()
        while True:
            if time.monotonic() - last_sample >= 2:
                try:
                    value, metric = memory(pid, cfg["metric"])
                    warning = warning or value >= cfg["hard-limit"]
                except Unavailable as error:
                    message = clean(error)
                last_sample = time.monotonic()
            height, width = screen.getmaxyx()
            page = max(1, height - 10)
            offset = min(offset, max(0, len(rows) - page))
            screen.erase()

            def put(y, text, attr=0):
                if 0 <= y < height:
                    try:
                        screen.addnstr(y, 0, clean(text), max(0, width - 1), attr)
                    except curses.error:
                        pass

            title = "HARD MEMORY LIMIT WARNING" if warning else "tmux memory - pane histories"
            put(0, title, curses.A_BOLD | (curses.A_REVERSE if warning else 0))
            put(1, f"Server {pid}: {size_label(value)} [{metric}]")
            put(2, f"Soft {size_label(cfg['soft-limit'])} | Hard {size_label(cfg['hard-limit'])}")
            put(3, "Numbers clear the ENTIRE selected pane scrollback; visible screen and command stay."
                if allow_cleanup else "Read-only hard-limit warning. Open prefix + Alt-m separately for cleanup.")
            put(4, f"{'ROW':>4}  {'PANE':<7} {'GRID':>11} {'LINES':>9}  LOCATION (COMMAND)", curses.A_BOLD)
            for position, row in enumerate(rows[offset:offset + page], offset):
                label = "[pane closed]" if row.get("closed") else row["label"]
                put(5 + position - offset,
                    f"{position + 1:>4}  {row['pane']:<7} {size_label(row['bytes']):>11} {row['lines']:>9,}  {label}")
            put(height - 4, "Grid bytes include the visible screen; freed memory may stay in the allocator.")
            put(height - 3, message)
            put(height - 2, "q / Esc: close | arrows / PgUp / PgDn: scroll | r: refresh and re-sort")
            put(height - 1, "Clear row number + Enter: " + entry if allow_cleanup else
                "Only q or Esc acknowledges this warning. Pane history is unchanged.", curses.A_BOLD)
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ("q", "\x1b"):
                return
            if allow_cleanup and isinstance(key, str) and key.isascii() and key.isdigit():
                entry = (entry + key)[:8]
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                entry = entry[:-1]
            elif allow_cleanup and key in ("\n", "\r", curses.KEY_ENTER) and entry:
                number, entry = int(entry), ""
                if not 1 <= number <= len(rows):
                    message = "No such row; nothing cleared."
                    continue
                row = rows[number - 1]
                try:
                    if row.get("closed"):
                        raise Unavailable("pane has closed; nothing cleared")
                    clear_pane(tmux, pid, row["pane"])
                    message = f"Cleared {row['pane']} scrollback. Row numbers are unchanged."
                    refresh(False)
                except (Unavailable, ValueError) as error:
                    message = clean(error)
            elif key == "r":
                entry = ""
                try:
                    refresh(True)
                    message = "Refreshed and re-sorted rows."
                except (Unavailable, ValueError) as error:
                    message = clean(error)
            elif key in (curses.KEY_DOWN, "j"):
                offset += 1
            elif key in (curses.KEY_UP, "k"):
                offset = max(0, offset - 1)
            elif key == curses.KEY_NPAGE:
                offset += page
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - page)
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = len(rows)

    curses.wrapper(run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmux", default=shutil.which("tmux") or "tmux")
    parser.add_argument("--socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("stop")
    callback = sub.add_parser("tick")
    callback.add_argument("--generation", required=True)
    callback.add_argument("--server-pid", type=int, required=True)
    hook = sub.add_parser("attached")
    hook.add_argument("--client", required=True)
    popup = sub.add_parser("show")
    popup.add_argument("--client")
    popup.add_argument("--warning", action="store_true")
    popup.add_argument("--server-pid", type=int)
    ui = sub.add_parser("ui")
    ui.add_argument("--warning", action="store_true")
    ui.add_argument("--server-pid", type=int)
    output = sub.add_parser("report")
    output.add_argument("--json", action="store_true")
    output.add_argument("--pause", action="store_true")
    args = parser.parse_args()
    tmux = Tmux(args.tmux, args.socket)
    try:
        if args.command == "start":
            start(tmux)
        elif args.command == "tick":
            tick(tmux, args.generation, args.server_pid)
        elif args.command == "attached":
            attached(tmux, args.client)
        elif args.command == "stop":
            tmux.identity()
            tmux.commands(tmux.set_commands({PREFIX + "enabled": "off",
                                            PREFIX + "generation": uuid.uuid4().hex,
                                            STATUS: ""}))
        elif args.command == "show":
            pid = tmux.identity()
            if args.server_pid is not None and pid != args.server_pid:
                raise Unavailable("server changed; warning popup cancelled")
            target = args.client or next(iter(clients(tmux)), None)
            if not target:
                raise Unavailable("no attached client for the popup; use report")
            child = ["--server-pid", pid]
            if args.warning:
                child.append("--warning")
            tmux.run("display-popup", "-E", "-c", target, "-w", "90%", "-h", "80%",
                     tmux.invocation("ui", *child), timeout=None)
        elif args.command == "ui":
            interactive(tmux, args.server_pid, args.warning)
        else:
            if args.pause:
                interactive(tmux)
            else:
                report(tmux, args.json)
    except (Unavailable, ValueError, OSError) as error:
        # Scheduled checks must not open tmux view mode by printing errors.
        if args.command in ("tick", "attached"):
            return 0
        print("tmux-memwatch: " + clean(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
