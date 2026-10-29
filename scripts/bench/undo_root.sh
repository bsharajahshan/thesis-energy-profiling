#!/usr/bin/env bash

# undo_root.sh - removes everything setup_root.sh changed (after the thesis).
#
#   sudo bash scripts/bench/undo_root.sh
#
#   1. disables and deletes the boot service and /usr/local/sbin/thesis-bench-perms
#   2. deletes /etc/sysctl.d/99-thesis-bench-perf.conf and sets
#      perf_event_paranoid back to the value before the setup
#   3. turns linger off again (only if the setup turned it on)
#   4. unmasks the sleep targets the setup masked (targets masked before stay masked)
#   5. gives the energy counters and governor/EPP files back to root now
#      (a reboot would do the same)
#   6. deletes /etc/thesis-bench (the copy of the old 99-thesis-pilot sudoers file
#      is NOT restored: it never worked and it was a password-less sudo rule)
# The benchmark results in ~/thesis-results are not touched (bash scripts/bench/run.sh clean).
#
# Test hooks: the same as setup_root.sh.

set -u
export LC_ALL=C
exec </dev/null

PREFIX="${BENCH_SETUP_ROOT:-}"
SYS="${BENCH_SETUP_SYS:-$PREFIX}"
SYSTEMCTL="${BENCH_SYSTEMCTL:-systemctl}"
SYSCTL="${BENCH_SYSCTL:-sysctl}"
LOGINCTL="${BENCH_LOGINCTL:-loginctl}"
ROOT_GROUP="${BENCH_SETUP_ROOT_GROUP:-root}"

if [[ $EUID -ne 0 && -z "${BENCH_SETUP_TEST:-}" ]]; then
    echo "This step needs root. Run it in your own terminal: sudo bash scripts/bench/undo_root.sh"
    exit 1
fi

STATE_DIR="$PREFIX/etc/thesis-bench"
STATE="$STATE_DIR/state.txt"
HELPER="$PREFIX/usr/local/sbin/thesis-bench-perms"
UNIT="$PREFIX/etc/systemd/system/thesis-bench-perms.service"
SYSCTL_FILE="$PREFIX/etc/sysctl.d/99-thesis-bench-perf.conf"
if [[ ! -e "$STATE" && ! -e "$HELPER" && ! -e "$UNIT" ]]; then
    echo "UNDO DONE: the setup is not installed (nothing to undo)."
    exit 0
fi
state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | head -n 1; }
USER_NAME="$(state_get user)"
FAILS=0
REPORT=()
report() {
    REPORT+=("$(printf '%-5s %s' "$1" "$2")")
    [[ "$1" == FAIL ]] && FAILS=$((FAILS + 1))
    return 0
}

# 1. service and helper
"$SYSTEMCTL" disable --now thesis-bench-perms.service > /dev/null 2>&1
rm -f "$UNIT" "$HELPER"
"$SYSTEMCTL" daemon-reload > /dev/null 2>&1
if [[ -e "$UNIT" || -e "$HELPER" ]]; then
    report FAIL "could not delete $UNIT / $HELPER"
else
    report OK "boot service and helper removed"
fi

# 2. perf
rm -f "$SYSCTL_FILE"
before="$(state_get perf_event_paranoid_before)"
if [[ "$before" =~ ^-?[0-9]+$ ]]; then
    if "$SYSCTL" -q -w "kernel.perf_event_paranoid=$before" > /dev/null 2>&1; then
        report OK "perf_event_paranoid back to $before"
    else
        report FAIL "could not set perf_event_paranoid back to $before"
    fi
else
    report OK "perf setting file removed (old value unknown; the next reboot uses the system default)"
fi

# 3. linger
if [[ "$(state_get linger_before)" == no && -n "$USER_NAME" ]]; then
    if "$LOGINCTL" disable-linger "$USER_NAME" > /dev/null 2>&1; then
        report OK "linger off again for $USER_NAME"
    else
        report FAIL "loginctl disable-linger $USER_NAME failed"
    fi
else
    report OK "linger left as it was before the setup"
fi

# 4. sleep targets masked by the setup
masked="$(sed -n 's/^masked_by_setup=//p' "$STATE" 2>/dev/null | tr '\n' ' ')"
bad=""
for t in $masked; do
    "$SYSTEMCTL" unmask "$t" > /dev/null 2>&1 || bad+="$t "
done
if [[ -n "$bad" ]]; then
    report FAIL "could not unmask: $bad"
else
    report OK "sleep and suspend allowed again${masked:+ (unmasked: ${masked% })}"
fi

# 5. file permissions of this boot back to root
for d in "$SYS"/sys/class/powercap/*; do
    f="$(readlink -f "$d")/energy_uj"
    [[ -f "$f" ]] && chgrp "$ROOT_GROUP" "$f" 2>/dev/null && chmod g-r "$f" 2>/dev/null
done
for f in "$SYS"/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor \
         "$SYS"/sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference; do
    [[ -e "$f" ]] && chgrp "$ROOT_GROUP" "$f" 2>/dev/null && chmod g-w "$f" 2>/dev/null
done
report OK "energy counters and governor/EPP files back to root"

# 6. state
rm -rf "$STATE_DIR"
if [[ -e "$STATE_DIR" ]]; then
    report FAIL "could not delete $STATE_DIR"
else
    report OK "$STATE_DIR deleted"
fi

printf '%s\n' "${REPORT[@]}"
echo
if (( FAILS == 0 )); then
    echo "UNDO DONE. (The results in ~/thesis-results are removed with: bash scripts/bench/run.sh clean)"
    exit 0
fi
echo "UNDO INCOMPLETE: $FAILS problem(s) above."
exit 1
