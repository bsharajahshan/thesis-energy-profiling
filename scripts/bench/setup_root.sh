#!/usr/bin/env bash

# setup_root.sh - ONE-TIME setup of the measurement laptop (the only step that needs sudo).
#
#   sudo bash scripts/bench/setup_root.sh
#
# Run it once in your own terminal: sudo asks for your password once. After
# that the benchmark (run.sh) never needs sudo, also not after a reboot.
# Running it again does no harm. After the thesis everything is undone with
#   sudo bash scripts/bench/undo_root.sh
#
# What it does (each step is reported as OK or FAIL at the end):
#   1. installs /usr/local/sbin/thesis-bench-perms and the boot service
#      thesis-bench-perms.service, and runs it now. At every boot it makes the
#      RAPL energy counters readable and the CPU governor and EPP writable for
#      YOUR group only (Linux keeps the energy counters root-only since 2020),
#      and lets users read the perf counters. The service runs this root-owned
#      copy, never a file from the repository.
#   2. /etc/sysctl.d/99-thesis-bench-perf.conf: kernel.perf_event_paranoid = -1
#      (perf may count the whole machine without root), also after reboots
#   3. loginctl enable-linger: the session keeps running after you log out
#   4. masks the sleep targets: the laptop cannot suspend during a session
#      (closing the lid does nothing while this setup is installed)
#   5. deletes /etc/sudoers.d/99-thesis-pilot from the first attempt (it never
#      worked); a copy stays in /etc/thesis-bench until the undo
#   6. writes /etc/thesis-bench/setup.txt, which the start check looks for
#   7. checks as your user that the counters are readable and the governor writable
#
# Test hooks (environment, for the tests only): BENCH_SETUP_TEST=1 (no root
# check), BENCH_SETUP_ROOT (prefix for /etc, /usr/local/sbin, /var/lib),
# BENCH_SETUP_SYS (prefix for /sys and /proc), BENCH_SETUP_USER,
# BENCH_SETUP_GROUP, BENCH_SYSTEMCTL, BENCH_SYSCTL, BENCH_LOGINCTL,
# BENCH_VISUDO, BENCH_RUNUSER.

set -u
export LC_ALL=C
exec </dev/null
umask 022

PREFIX="${BENCH_SETUP_ROOT:-}"
SYS="${BENCH_SETUP_SYS:-$PREFIX}"
SYSTEMCTL="${BENCH_SYSTEMCTL:-systemctl}"
SYSCTL="${BENCH_SYSCTL:-sysctl}"
LOGINCTL="${BENCH_LOGINCTL:-loginctl}"
VISUDO="${BENCH_VISUDO:-visudo}"
RUNUSER="${BENCH_RUNUSER:-runuser}"

if [[ $EUID -ne 0 && -z "${BENCH_SETUP_TEST:-}" ]]; then
    echo "This step needs root. Run it in your own terminal: sudo bash scripts/bench/setup_root.sh"
    exit 1
fi
USER_NAME="${BENCH_SETUP_USER:-${SUDO_USER:-}}"
if [[ -z "$USER_NAME" || "$USER_NAME" == root ]]; then
    echo "Run it with sudo from your own (normal) user account, not as root: sudo bash scripts/bench/setup_root.sh"
    exit 1
fi
GROUP_NAME="${BENCH_SETUP_GROUP:-$(id -gn "$USER_NAME" 2>/dev/null)}"
if [[ -z "$GROUP_NAME" ]]; then
    echo "Unknown user: $USER_NAME"
    exit 1
fi

STATE_DIR="$PREFIX/etc/thesis-bench"
HELPER="$PREFIX/usr/local/sbin/thesis-bench-perms"
UNIT="$PREFIX/etc/systemd/system/thesis-bench-perms.service"
SYSCTL_FILE="$PREFIX/etc/sysctl.d/99-thesis-bench-perf.conf"
LINGER_FILE="$PREFIX/var/lib/systemd/linger/$USER_NAME"
SUDOERS_OLD="$PREFIX/etc/sudoers.d/99-thesis-pilot"
TARGETS="sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target"
FAILS=0
REPORT=()

report() {   # OK|FAIL TEXT
    REPORT+=("$(printf '%-5s %s' "$1" "$2")")
    [[ "$1" == FAIL ]] && FAILS=$((FAILS + 1))
    return 0
}

mkdir -p "$STATE_DIR" "$(dirname "$HELPER")" "$(dirname "$UNIT")" "$(dirname "$SYSCTL_FILE")"
chmod 755 "$STATE_DIR"
STATE="$STATE_DIR/state.txt"
state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | head -n 1; }
state_set() {   # KEY VALUE - the first value is kept (the state before the FIRST setup)
    [[ -n "$(state_get "$1")" ]] || printf '%s=%s\n' "$1" "$2" >> "$STATE"
}
state_set user "$USER_NAME"
state_set group "$GROUP_NAME"
state_set perf_event_paranoid_before "$(cat "$SYS/proc/sys/kernel/perf_event_paranoid" 2>/dev/null || echo unknown)"
state_set linger_before "$([[ -e "$LINGER_FILE" ]] && echo yes || echo no)"

# ---- 1. boot helper and service ------------------------------------------------------------

cat > "$HELPER.tmp" <<EOF
#!/bin/bash
# thesis-bench-perms - installed by scripts/bench/setup_root.sh (thesis energy benchmark).
# Runs as root at every boot (thesis-bench-perms.service) and once at setup:
# RAPL energy counters readable and CPU governor / EPP writable for the group
# $GROUP_NAME, perf counters open for users. Removed by scripts/bench/undo_root.sh.
SYS='$SYS'
GROUP='$GROUP_NAME'
SYSCTL='$SYSCTL'
rc=0
modprobe -q intel_rapl_msr 2>/dev/null
for d in "\$SYS"/sys/class/powercap/*; do
    f="\$(readlink -f "\$d")/energy_uj"          # the class entries are symlinks
    [[ -f "\$f" ]] || continue
    chgrp "\$GROUP" "\$f" && chmod g+r "\$f" || rc=1
done
for f in "\$SYS"/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor \\
         "\$SYS"/sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference; do
    [[ -e "\$f" ]] || continue
    chgrp "\$GROUP" "\$f" && chmod g+rw "\$f" || rc=1
done
"\$SYSCTL" -q -w kernel.perf_event_paranoid=-1 || rc=1
exit \$rc
EOF
chmod 755 "$HELPER.tmp" && mv -f "$HELPER.tmp" "$HELPER"

cat > "$UNIT" <<EOF
[Unit]
Description=Thesis energy benchmark: RAPL counters readable, governor/EPP writable for group $GROUP_NAME (until undo_root.sh)
After=systemd-modules-load.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/thesis-bench-perms
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
"$SYSTEMCTL" daemon-reload > /dev/null 2>&1
if "$SYSTEMCTL" enable thesis-bench-perms.service > /dev/null 2>&1; then
    svc="boot service enabled"
else
    svc="boot service could NOT be enabled"
    report FAIL "systemctl enable thesis-bench-perms.service failed: the settings would be lost at the next reboot"
fi
if "$HELPER"; then
    report OK "counters readable and governor/EPP writable for group $GROUP_NAME ($svc)"
else
    report FAIL "$HELPER reported an error (see above)"
fi

# ---- 2. perf --------------------------------------------------------------------------------

printf '# written by scripts/bench/setup_root.sh (thesis energy benchmark); removed by undo_root.sh\nkernel.perf_event_paranoid = -1\n' > "$SYSCTL_FILE"
chmod 644 "$SYSCTL_FILE"
paranoid="$(cat "$SYS/proc/sys/kernel/perf_event_paranoid" 2>/dev/null)"
if [[ "$paranoid" == -1 ]]; then
    report OK "perf may count the whole machine without root (perf_event_paranoid = -1, kept after reboots)"
else
    report FAIL "perf_event_paranoid is '$paranoid', expected -1"
fi

# ---- 3. linger ------------------------------------------------------------------------------

if [[ ! -e "$LINGER_FILE" ]]; then
    "$LOGINCTL" enable-linger "$USER_NAME" > /dev/null 2>&1
fi
if [[ -e "$LINGER_FILE" ]]; then
    report OK "linger on for $USER_NAME: the session keeps running after logout"
else
    report FAIL "loginctl enable-linger $USER_NAME did not work"
fi

# ---- 4. no sleep ----------------------------------------------------------------------------

masked_now=()
for t in $TARGETS; do
    if [[ "$("$SYSTEMCTL" is-enabled "$t" 2>/dev/null)" == masked ]]; then
        continue                                  # masked before (by us earlier or by you): leave it
    fi
    if "$SYSTEMCTL" mask "$t" > /dev/null 2>&1; then
        masked_now+=("$t")
    else
        report FAIL "systemctl mask $t failed"
    fi
done
if (( ${#masked_now[@]} )); then
    printf 'masked_by_setup=%s\n' "${masked_now[*]}" >> "$STATE"
fi
report OK "sleep and suspend blocked (closing the lid does nothing until undo_root.sh)"

# ---- 5. old sudoers file -----------------------------------------------------------------------

if [[ -e "$SUDOERS_OLD" ]]; then
    mv -f "$SUDOERS_OLD" "$STATE_DIR/removed-sudoers-99-thesis-pilot" && chmod 600 "$STATE_DIR/removed-sudoers-99-thesis-pilot"
    if [[ -e "$SUDOERS_OLD" ]]; then
        report FAIL "could not remove $SUDOERS_OLD"
    elif "$VISUDO" -c > /dev/null 2>&1; then
        report OK "removed the old $SUDOERS_OLD (sudo configuration still valid)"
    else
        report FAIL "removed $SUDOERS_OLD, but 'visudo -c' reports a problem in the sudo configuration"
    fi
else
    report OK "old /etc/sudoers.d/99-thesis-pilot not present"
fi

# ---- 6. marker -----------------------------------------------------------------------------------

{
    echo "version=1"
    echo "user=$USER_NAME"
    echo "group=$GROUP_NAME"
    echo "installed=$(date -u +%FT%TZ)"
    echo "undo=sudo bash scripts/bench/undo_root.sh"
} > "$STATE_DIR/setup.txt"
chmod 644 "$STATE_DIR/setup.txt" "$STATE"
if grep -qx "user=$USER_NAME" "$STATE_DIR/setup.txt" 2>/dev/null; then
    report OK "marker $STATE_DIR/setup.txt written (the start check looks for it)"
else
    report FAIL "could not write $STATE_DIR/setup.txt"
fi

# ---- 7. check as the user --------------------------------------------------------------------------

as_user() { "$RUNUSER" -u "$USER_NAME" -- "$@" 2>/dev/null; }
pkg="$(readlink -f "$SYS/sys/class/powercap/intel-rapl:0" 2>/dev/null)"
dram=""
for d in "$pkg"/intel-rapl:0:*; do
    [[ "$(cat "$d/name" 2>/dev/null)" == dram ]] && dram="$d"
done
problems=()
[[ -n "$pkg" ]] && as_user test -r "$pkg/energy_uj" || problems+=("package energy counter")
[[ -n "$dram" ]] && as_user test -r "$dram/energy_uj" || problems+=("DRAM energy counter")
for f in "$SYS"/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
    [[ -e "$f" ]] || continue
    as_user test -w "$f" || { problems+=("governor ${f%/cpufreq/*}"); break; }
done
if (( ${#problems[@]} )); then
    report FAIL "as $USER_NAME not readable/writable: ${problems[*]}"
else
    report OK "checked as $USER_NAME: package and DRAM counters readable, governor writable"
fi

echo
printf '%s\n' "${REPORT[@]}"
echo
if (( FAILS == 0 )); then
    echo "SETUP DONE. The benchmark now runs without sudo. (Undo after the thesis: sudo bash scripts/bench/undo_root.sh)"
    exit 0
fi
echo "SETUP INCOMPLETE: $FAILS problem(s) above. Send this whole output back; do not start the benchmark yet."
exit 1
