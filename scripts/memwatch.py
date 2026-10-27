#!/usr/bin/env python3
"""Notification-only tmux server memory monitor. No third-party dependencies."""

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
    "enabled": "on", "threshold": "4GiB", "interval": "60",
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
    result["threshold"] = parse_size(result["threshold"])
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
            if key.startswith(PREFIX) or key in (STATUS, "status-right"):
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
    warning = value >= cfg["threshold"] or (
        previous.get("warning", False)
        and value >= cfg["threshold"] * cfg["reset-percent"] / 100)
    due = warning and value >= cfg["threshold"] and (
        not previous.get("warning", False)
        or now - previous.get("last_alert", 0) >= cfg["reminder"])
    state = dict(previous, warning=warning, bytes=value, sampled_at=time.time(),
                 error="", checks=previous.get("checks", 0) + 1)
    if due:
        state["last_alert"] = now
    if not warning:
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
        return "#[fg=red,bold]MEM " + size_label(state["bytes"]) + "#[default]"
    return ""


def notify(tmux, generation, state, cfg, targets=None):
    if cfg["banner"] == "off":
        return
    try:
        targets = clients(tmux) if targets is None else targets
    except Unavailable:
        return
    message = f"tmux memory: {size_label(state.get('bytes', 0))} ({state.get('metric', '?')}); threshold {size_label(cfg['threshold'])}."
    top = state.get("top", [])
    if top:
        message += f" Largest history: {top[0]['pane']} {size_label(top[0]['bytes'])}."
    message += " Inspect pane histories before manual cleanup."
    flags = ["-C"] if state.get("live_message") else []
    # Older tmux versions lack -C; use a shorter, key-dismissible message.
    delay = "10000" if flags else "2000"
    for client in targets:
        try:
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
        if due:
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
            values[PREFIX + name] = value
    if cfg["status"] == "on" and cfg["status-append"] == "on" and "#{" + STATUS + "}" not in options.get("status-right", ""):
        values["status-right"] = options.get("status-right", "") + " #{" + STATUS + "}"
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


def report(tmux, as_json=False, pause=False):
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
        print(f"Threshold: {size_label(cfg['threshold'])}; socket: {clean(tmux.socket)}\n")
        print(f"{'PANE':<8} {'GRID':>12} {'LINES':>10}  LOCATION (COMMAND)")
        for row in rows:
            print(f"{row['pane']:<8} {size_label(row['bytes']):>12} {row['lines']:>10,}  {row['label']}")
        print("\nGrid bytes include visible rows and exclude allocator overhead.")
        print("Pane applications use separate memory. Clearing history may not immediately reduce RSS.")
        print("\nInspect a pane before cleanup. This plugin never clears history or changes history-limit.")
        print("Manual cleanup: tmux -S <socket-above> clear-history -t <pane-id>")
    if pause:
        try:
            input("\nPress Enter to close. ")
        except EOFError:
            pass


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
            tmux.identity()
            target = args.client or next(iter(clients(tmux)), None)
            if not target:
                raise Unavailable("no attached client for the popup; use report")
            tmux.run("display-popup", "-E", "-c", target, "-w", "90%", "-h", "80%",
                     "-T", "tmux memory", tmux.invocation("report", "--pause"), timeout=None)
        else:
            report(tmux, args.json, args.pause)
    except (Unavailable, ValueError, OSError) as error:
        # Scheduled checks must not open tmux view mode by printing errors.
        if args.command in ("tick", "attached"):
            return 0
        print("tmux-memwatch: " + clean(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
