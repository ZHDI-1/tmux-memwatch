#!/bin/sh
# TPM entry point. The checker needs only Python's standard library.
set -eu
plugin_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
python_bin=$(command -v python3) || {
    printf '%s\n' 'tmux-memwatch requires Python 3.9 or later.' >&2
    exit 1
}
exec "$python_bin" "$plugin_dir/scripts/memwatch.py" start
