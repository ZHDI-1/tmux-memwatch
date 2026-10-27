# tmux-memwatch

Watch the tmux server's memory on macOS and Linux. Get an in-tmux warning and
inspect the panes holding the most scrollback. Cleanup stays manual.

Requires **tmux 3.2+ and Python 3.9+**. No pip packages, operating-system
services, persistent helper processes, or temporary state files are needed.

## Install

With [TPM](https://github.com/tmux-plugins/tpm), add this before TPM is loaded:

```tmux
set -g @plugin 'ZHDI-1/tmux-memwatch'
```

Press your prefix followed by `I`. Or clone the repository and load it directly:

```sh
git clone https://github.com/ZHDI-1/tmux-memwatch ~/.config/tmux/plugins/tmux-memwatch
```

```tmux
run-shell '~/.config/tmux/plugins/tmux-memwatch/memwatch.tmux'
```

Python must be on the tmux server's PATH when the plugin loads. The resolved
interpreter is used for subsequent checks.

## Behavior

By default the plugin checks server memory every 60 seconds, including while
detached. It warns at 4 GiB, reminds at most once every 15 minutes while still
above the threshold, and clears the warning below 75% of the threshold.
An attaching client receives an existing warning. Control-mode clients are skipped.

A warning appears in the status line and as a brief message. On tmux versions
with `display-message -C`, pane contents keep updating while the message is shown.
Older versions get a shorter, key-dismissible message; banners can be disabled.

**Prefix + Meta-m** opens the pane report in a popup. An existing binding is
preserved. The report identifies panes by stable ID, window location, command,
retained line count, and allocated grid bytes, sorted largest first.

The status badge is empty while healthy. By default the plugin appends
`#{@memwatch_status}` once to the existing global `status-right`. Session-specific
status overrides may need the same token added manually. To place it yourself,
set `@memwatch-status-append off` before loading and put `#{@memwatch_status}` in
your chosen format. To suppress it entirely, set `@memwatch-status off`.
Reloading never repeatedly appends the token.

## Options

Set options before loading the plugin. A reload starts one new schedule;
already queued callbacks from earlier loads become no-ops.

| Option | Default | Meaning |
| --- | --- | --- |
| `@memwatch-enabled` | `on` | Whether to schedule checks |
| `@memwatch-threshold` | `4GiB` | Warning level; B, KiB, MiB, GiB, TiB (K/M/G/T aliases use binary units) |
| `@memwatch-interval` | `60` | Seconds between completed checks |
| `@memwatch-reminder` | `900` | Minimum seconds between reminders |
| `@memwatch-reset-percent` | `75` | Percentage of threshold below which the warning resets |
| `@memwatch-top` | `5` | Number of largest histories cached with an alert |
| `@memwatch-metric` | `auto` | Native accounting, or `rss` for resident memory only |
| `@memwatch-status` | `on` | Produce a warning badge and append its token at load |
| `@memwatch-status-append` | `on` | Append the badge token; turn off for manual placement |
| `@memwatch-banner` | `on` | Send messages to attached normal clients |
| `@memwatch-key` | `M-m` | Prefix-table report key; empty string disables binding |

```tmux
set -g @memwatch-threshold '4GiB'
set -g @memwatch-interval 60
set -g @memwatch-reminder 900
set -g @plugin 'ZHDI-1/tmux-memwatch'
```

The checker re-reads options on each run. After disabling, reload or run `start`
to enable checks again. Measurement failure displays `MEM ?`, retains any prior
warning, and retries; it is never treated as zero usage.

## Memory accounting

* **macOS auto:** `proc_pid_rusage().ri_phys_footprint`, including memory charged
  for compressed/swapped allocations. This is not simply RSS.
* **Linux auto:** `VmRSS + VmSwap` from `/proc/<pid>/status`. This is an inexpensive
  estimate, includes private anonymous swap, and can count shared resident pages.
* **RSS mode/fallback:** `ps -o rss=`. A fallback is explicitly labeled in reports.

These operating-system counters are not identical. The report names its metric.
Only the tmux **server** is measured; shells, editors, and other pane applications
have their own memory. Multiple tmux servers are monitored independently.

Pane `history_bytes` includes visible screen rows and excludes malloc overhead,
copy-mode copies, paste buffers, and other server allocations. It identifies
cleanup candidates rather than attributing every byte of process memory.
Clearing history may not immediately lower process RSS because allocators reuse
freed pages. The plugin never clears history, kills panes, or changes history-limit.

## Scheduling and overhead

Each check schedules its successor with `run-shell -b -d`. tmux uses an event
timer during the delay and executes the checker in a child process. The command
queue and main input loop do not wait for the checker. Process spawning and
tmux commands still have a cost; this is not a zero-latency guarantee.

The normal check reads only the OS process counter. History grids are scanned
only for an alert or a requested report: `history_bytes` is computed synchronously
inside tmux and a huge scrollback can introduce latency during that scan.

State is stored in tmux user options. Every callback carries a generation token
and expected server PID, and explicitly uses its server socket. Reloads invalidate
older callbacks; a stopped server cannot be accidentally restarted by a check.
Notification failures do not stop the next check. The plugin reserves hook array
slot `client-attached[8341]` and preserves other entries.

## Commands

From a pane in the monitored server:

```sh
python3 ~/.config/tmux/plugins/tmux-memwatch/scripts/memwatch.py report
python3 ~/.config/tmux/plugins/tmux-memwatch/scripts/memwatch.py report --json
python3 ~/.config/tmux/plugins/tmux-memwatch/scripts/memwatch.py stop
python3 ~/.config/tmux/plugins/tmux-memwatch/scripts/memwatch.py start
```

After `stop`, set `@memwatch-enabled on` before `start`. Use `--socket /path/to/socket`
before the command to target another server. The report prints a manual cleanup
command template; inspect the selected pane before using it.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests cover both native memory adapters, warning hysteresis, reminders, disconnects,
reload invalidation, detached timers, unreadable samples, linked windows, and server
isolation. Integration tests use private temporary socket directories and stop their
servers in `finally` blocks. They never touch the default server or `/tmp/tmux-<uid>`.
GitHub Actions runs the suite on macOS and Linux. MIT licensed.

Accounting references: [Apple memory footprint](https://developer.apple.com/videos/play/wwdc2022/10106/),
[Linux process counters](https://www.kernel.org/doc/html/latest/filesystems/proc.html),
and [tmux delayed shell jobs](https://man.openbsd.org/tmux#run-shell).
