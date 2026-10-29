#!/usr/bin/env bash

# sampler.sh - one line per second during a run:
#   <time (bash EPOCHREALTIME)> <package energy uJ> <DRAM energy uJ> <package temperature milli-degC>
# ('-' where a file cannot be read). Only sysfs reads with bash builtins and
# one `sleep` per second, so it costs almost nothing; the measurement-cost
# control runs compare runs with and without it. Stops by itself when its
# parent (run_case.sh) is gone.
#
# Usage: sampler.sh <session.env> <out_file> <parent_pid>

set -u
export LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
exec </dev/null

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$HERE/common.sh"
# shellcheck source=/dev/null
source "$1"
OUT_FILE="$2"
PARENT="$3"
INTERVAL="${SAMPLER_INTERVAL_S:-1}"

p="" d="" c=""
while kill -0 "$PARENT" 2>/dev/null; do
    rd p "$RAPL_PKG_DIR/energy_uj"
    d=""
    [[ -n "${RAPL_DRAM_DIR:-}" ]] && rd d "$RAPL_DRAM_DIR/energy_uj"
    rd c "$TEMP_FILE"
    printf '%s %s %s %s\n' "$EPOCHREALTIME" "${p:--}" "${d:--}" "${c:--}"
    sleep "$INTERVAL"
done >> "$OUT_FILE"
