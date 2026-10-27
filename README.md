# tmux-memwatch

Watch the tmux server's memory on macOS and Linux. Soft limits show a banner and
status badge; hard limits open a warning popup. Inspect the biggest pane histories
and explicitly clear one from the interactive report.

Requires **tmux 3.2+ and Python 3.8+ with its standard curses module**. No pip packages, operating-system
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
detached. At the **4 GiB soft limit**, an amber badge and a short status-line banner
appear. At the **8 GiB hard limit**, the badge turns red and a warning popup opens
on each attached normal client. A hard escalation bypasses the soft reminder timer.
Soft and hard reminders each have a 15-minute interval. Each severity resets below
75% of its own limit, so small fluctuations do not repeatedly trigger alerts.
An attaching client receives an existing warning; control-mode clients are skipped.

A warning appears in the status line and as a brief message. On tmux versions
with `display-message -C`, pane contents keep updating while the message is shown.
Older versions get a shorter, key-dismissible message; banners can be disabled.

**Prefix + Meta-m** opens the interactive report. An existing binding is preserved.
Both popups close only with **q or Escape**. Enter without a selection, ordinary
letters, and Ctrl-C/D/Z do not dismiss them.

The manual report lists numbered rows with pane ID, window location, command,
retained lines, and grid bytes, largest first. **Type a row number and press Enter**
to clear that pane's entire scrollback. Multi-digit rows work (for example `12`
then Enter). The visible screen and running command stay intact, and no other pane
is cleared. Row numbers stay fixed after cleanup; `r` refreshes and re-sorts.
Arrows, Page Up/Down, Home/End, and `j`/`k` scroll through longer lists.

Automatic hard warnings are read-only so ongoing typing cannot clear history.
Close the warning, then use the manual report for cleanup. Memory counters update
every two seconds while a popup is open, but histories are scanned only on open,
refresh, or cleanup. A warning stays open even if memory falls below the limit.

The status badge is empty while healthy. By default the plugin appends
`#{@memwatch_status}` once to the existing global `status-right` and reserves at
least 80 columns for that area so the badge is not clipped. Session-specific
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
| `@memwatch-soft-limit` | `4GiB` | Banner and status warning level |
| `@memwatch-hard-limit` | `8GiB` | Automatic warning popup level; must exceed soft limit |
| `@memwatch-interval` | `60` | Seconds between completed checks |
| `@memwatch-reminder` | `900` | Minimum seconds between reminders of each severity |
| `@memwatch-reset-percent` | `75` | Percentage of each limit below which that severity resets |
| `@memwatch-top` | `5` | Number of largest histories cached with an alert |
| `@memwatch-metric` | `auto` | Native accounting, or `rss` for resident memory only |
| `@memwatch-status` | `on` | Produce a warning badge and append its token at load |
| `@memwatch-status-append` | `on` | Append the badge token; turn off for manual placement |
| `@memwatch-banner` | `on` | Send soft-limit banners; hard popups remain enabled |
| `@memwatch-key` | `M-m` | Prefix-table report key; empty string disables binding |

```tmux
set -g @memwatch-soft-limit '4GiB'
set -g @memwatch-hard-limit '8GiB'
set -g @memwatch-interval 60
set -g @memwatch-reminder 900
set -g @plugin 'ZHDI-1/tmux-memwatch'
```

Sizes accept B, KiB, MiB, GiB, TiB; K/M/G/T aliases also use binary units. When
the hard limit is initially unset, it defaults to twice the soft limit. The old
`@memwatch-threshold` is accepted as a soft-limit alias only while the new option
is unset. After migration, change the new options.

The checker re-reads options on each run. After disabling, set `@memwatch-enabled`
back to `on` and run `start`. Measurement failure displays `MEM ?`, retains any prior
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
freed pages. Cleanup happens only after explicit selection in the manual popup;
the plugin never automatically clears histories, kills panes, or changes history-limit.

tmux's `clear-history` has no option to remove only the oldest N lines. The plugin
therefore clears all scrollback for the selected pane. Lowering a global history
limit would affect other panes, so the plugin does not use that as a trimming trick.
It also does not paste captured text back into a live pane to fake partial cleanup.

## Scheduling and overhead

Each check schedules its successor with `run-shell -b -d`. tmux uses an event
timer during the delay and executes the checker in a child process. The command
queue and main input loop do not wait for the checker. Process spawning and
tmux commands still have a cost; this is not a zero-latency guarantee.

The normal check reads only the OS process counter. History grids are scanned
for soft alerts and opened reports: `history_bytes` is computed synchronously
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
GitHub Actions runs the suite on Python 3.8 on macOS and Linux, plus Python 3.12
on both platforms. MIT licensed.

Accounting references: [Apple memory footprint](https://developer.apple.com/videos/play/wwdc2022/10106/),
[Linux process counters](https://www.kernel.org/doc/html/latest/filesystems/proc.html),
and [tmux delayed shell jobs](https://man.openbsd.org/tmux#run-shell).
