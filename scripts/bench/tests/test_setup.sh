#!/usr/bin/env bash

# test_setup.sh - setup_root.sh and undo_root.sh on a fake root
#
# Stand-ins for systemctl, sysctl, loginctl, visudo and runuser record what
# they are asked to do; /etc, /usr/local/sbin, /var/lib and /sys live in a
# temporary folder. Runs as a normal user (test hook BENCH_SETUP_TEST=1). Real
# systemd, the real boot service and real sysfs permissions are NOT tested
# here; the friend's setup run prints OK/FAIL for each of them.
#
# Usage: bash scripts/bench/tests/test_setup.sh   (any Linux; used: WSL1 Ubuntu 22.04)

set -u
export LC_ALL=C
BENCH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d /tmp/setup_test.XXXXXX)"
trap 'rm -rf "$T"' EXIT
PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }

ME="$(id -un)"
GRP="$(id -gn)"
CTL="$T/ctl"
BIN="$T/bin"

fresh() {   # a new fake machine
    rm -rf "$T/p" "$T/s" "$CTL" "$BIN"
    P="$T/p"
    S="$T/s"
    mkdir -p "$CTL" "$BIN" "$P/etc/sudoers.d" "$P/var/lib/systemd/linger" "$S/proc/sys/kernel" "$S/sys/class/powercap"
    local rapl="$S/sys/devices/virtual/powercap/intel-rapl" mmio="$S/sys/devices/virtual/powercap/intel-rapl-mmio" c
    mkdir -p "$rapl/intel-rapl:0/intel-rapl:0:2" "$mmio/intel-rapl-mmio:0"
    echo package-0 > "$rapl/intel-rapl:0/name"
    echo dram > "$rapl/intel-rapl:0/intel-rapl:0:2/name"
    for f in "$rapl/intel-rapl:0/energy_uj" "$rapl/intel-rapl:0/intel-rapl:0:2/energy_uj" "$mmio/intel-rapl-mmio:0/energy_uj"; do
        echo 12345 > "$f"
        chmod 400 "$f"                       # root-only, as on Linux since 2020
    done
    ln -s ../../devices/virtual/powercap/intel-rapl "$S/sys/class/powercap/intel-rapl"
    ln -s ../../devices/virtual/powercap/intel-rapl/intel-rapl:0 "$S/sys/class/powercap/intel-rapl:0"
    ln -s ../../devices/virtual/powercap/intel-rapl/intel-rapl:0/intel-rapl:0:2 "$S/sys/class/powercap/intel-rapl:0:2"
    ln -s ../../devices/virtual/powercap/intel-rapl-mmio/intel-rapl-mmio:0 "$S/sys/class/powercap/intel-rapl-mmio:0"
    for c in 0 1 2 3; do
        mkdir -p "$S/sys/devices/system/cpu/cpu$c/cpufreq"
        echo powersave > "$S/sys/devices/system/cpu/cpu$c/cpufreq/scaling_governor"
        echo balance_performance > "$S/sys/devices/system/cpu/cpu$c/cpufreq/energy_performance_preference"
        chmod 644 "$S/sys/devices/system/cpu/cpu$c/cpufreq/"*
    done
    echo 4 > "$S/proc/sys/kernel/perf_event_paranoid"
    echo "$ME ALL=(ALL) NOPASSWD: /usr/bin/true" > "$P/etc/sudoers.d/99-thesis-pilot"
    touch "$CTL/masked" "$CTL/systemctl_calls"

    cat > "$BIN/systemctl" <<EOF
#!/bin/bash
CTL=$CTL
EOF
    cat >> "$BIN/systemctl" <<'EOF'
echo "$*" >> "$CTL/systemctl_calls"
case "$1" in
    is-enabled) grep -qx "$2" "$CTL/masked" && echo masked || echo static ;;
    mask) echo "$2" >> "$CTL/masked" ;;
    unmask) grep -vx "$2" "$CTL/masked" > "$CTL/masked.tmp"; mv "$CTL/masked.tmp" "$CTL/masked" ;;
    enable) [[ -e "$CTL/fail_enable" ]] && exit 1 ;;
esac
exit 0
EOF
    cat > "$BIN/sysctl" <<EOF
#!/bin/bash
for a in "\$@"; do
    [[ "\$a" == kernel.perf_event_paranoid=* ]] && echo "\${a#*=}" > "$S/proc/sys/kernel/perf_event_paranoid"
done
exit 0
EOF
    cat > "$BIN/loginctl" <<EOF
#!/bin/bash
case "\$1" in
    enable-linger) touch "$P/var/lib/systemd/linger/\$2" ;;
    disable-linger) rm -f "$P/var/lib/systemd/linger/\$2"; echo disable-linger >> "$CTL/loginctl_calls" ;;
esac
EOF
    cat > "$BIN/visudo" <<EOF
#!/bin/bash
[[ -e "$CTL/visudo_bad" ]] && exit 1
exit 0
EOF
    cat > "$BIN/runuser" <<EOF
#!/bin/bash
# runuser -u USER -- CMD...  (here: same user; can be told to deny)
[[ -e "$CTL/runuser_deny" ]] && exit 1
shift 3
exec "\$@"
EOF
    chmod +x "$BIN"/*
}

# setup / undo with the test hooks; sets OUT, RC
hooks() {
    env BENCH_SETUP_TEST=1 BENCH_SETUP_ROOT="$P" BENCH_SETUP_SYS="$S" BENCH_SETUP_USER="${USER_OVERRIDE-$ME}" \
        BENCH_SETUP_ROOT_GROUP="$GRP" BENCH_SYSTEMCTL="$BIN/systemctl" BENCH_SYSCTL="$BIN/sysctl" \
        BENCH_LOGINCTL="$BIN/loginctl" BENCH_VISUDO="$BIN/visudo" BENCH_RUNUSER="$BIN/runuser" "$@"
}
setup() { OUT="$(hooks bash "$BENCH/setup_root.sh" 2>&1 < /dev/null)"; RC=$?; }
undo()  { OUT="$(hooks bash "$BENCH/undo_root.sh" 2>&1 < /dev/null)"; RC=$?; }
out_has() { grep -q -e "$1" <<< "$OUT"; }
mode_is() { [[ "$(stat -c '%a %G' "$1")" == "$2" ]]; }
state_has() { grep -qx -e "$1" "$P/etc/thesis-bench/state.txt"; }
PKG_E="$T/s/sys/devices/virtual/powercap/intel-rapl/intel-rapl:0/energy_uj"
DRAM_E="$T/s/sys/devices/virtual/powercap/intel-rapl/intel-rapl:0/intel-rapl:0:2/energy_uj"
MMIO_E="$T/s/sys/devices/virtual/powercap/intel-rapl-mmio/intel-rapl-mmio:0/energy_uj"
GOV0="$T/s/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
EPP3="$T/s/sys/devices/system/cpu/cpu3/cpufreq/energy_performance_preference"
TARGETS="sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target"

echo "1. first setup"
fresh
setup
check "exit 0, SETUP DONE" bash -c "(( $RC == 0 )) && grep -q '^SETUP DONE' <<< \"\$1\"" _ "$OUT"
check "seven OK lines, no FAIL" bash -c "[[ \$(grep -c '^OK ' <<< \"\$1\") == 7 ]] && ! grep -q '^FAIL' <<< \"\$1\"" _ "$OUT"
check "helper installed and executable" test -x "$P/usr/local/sbin/thesis-bench-perms"
check "helper names the group and no repository file" bash -c "grep -q \"^GROUP='$GRP'\" '$P/usr/local/sbin/thesis-bench-perms' && ! grep -q '$BENCH' '$P/usr/local/sbin/thesis-bench-perms'"
check "boot service runs the installed helper" grep -qx "ExecStart=/usr/local/sbin/thesis-bench-perms" "$P/etc/systemd/system/thesis-bench-perms.service"
check "service enabled" grep -qx "enable thesis-bench-perms.service" "$CTL/systemctl_calls"
check "package counter readable for the group only (440)" mode_is "$PKG_E" "440 $GRP"
check "DRAM counter readable for the group (440)" mode_is "$DRAM_E" "440 $GRP"
check "mmio counter readable for the group (440)" mode_is "$MMIO_E" "440 $GRP"
check "governor writable for the group (664)" mode_is "$GOV0" "664 $GRP"
check "EPP writable for the group (664)" mode_is "$EPP3" "664 $GRP"
check "perf_event_paranoid = -1 now" grep -qx -- "-1" "$S/proc/sys/kernel/perf_event_paranoid"
check "... and after reboots (sysctl.d)" grep -qx "kernel.perf_event_paranoid = -1" "$P/etc/sysctl.d/99-thesis-bench-perf.conf"
check "linger on" test -e "$P/var/lib/systemd/linger/$ME"
check "all five sleep targets masked" bash -c "for t in $TARGETS; do grep -qx \$t '$CTL/masked' || exit 1; done"
check "old sudoers file removed" test ! -e "$P/etc/sudoers.d/99-thesis-pilot"
check "copy of it kept, readable by root only (600)" mode_is "$P/etc/thesis-bench/removed-sudoers-99-thesis-pilot" "600 $GRP"
check "marker for the start check" grep -qx "user=$ME" "$P/etc/thesis-bench/setup.txt"
check "old perf value remembered" state_has "perf_event_paranoid_before=4"
check "linger state before remembered" state_has "linger_before=no"
check "masked targets remembered" state_has "masked_by_setup=$TARGETS"

echo "2. second setup (nothing breaks, the 'before' values stay)"
setup
check "exit 0, SETUP DONE" bash -c "(( $RC == 0 )) && grep -q '^SETUP DONE' <<< \"\$1\"" _ "$OUT"
check "old perf value still 4 (not -1)" bash -c "[[ \$(grep -c '^perf_event_paranoid_before=' '$P/etc/thesis-bench/state.txt') == 1 ]] && grep -qx 'perf_event_paranoid_before=4' '$P/etc/thesis-bench/state.txt'"
check "no second masked_by_setup line" test "$(grep -c '^masked_by_setup=' "$P/etc/thesis-bench/state.txt")" -eq 1
check "sudoers: not present any more" out_has "99-thesis-pilot not present"

echo "3. undo"
undo
check "exit 0, UNDO DONE" bash -c "(( $RC == 0 )) && grep -q '^UNDO DONE' <<< \"\$1\"" _ "$OUT"
check "service disabled" grep -qx "disable --now thesis-bench-perms.service" "$CTL/systemctl_calls"
check "service, helper, sysctl file, state folder deleted" bash -c "[[ ! -e '$P/etc/systemd/system/thesis-bench-perms.service' && ! -e '$P/usr/local/sbin/thesis-bench-perms' && ! -e '$P/etc/sysctl.d/99-thesis-bench-perf.conf' && ! -e '$P/etc/thesis-bench' ]]"
check "perf_event_paranoid back to 4" grep -qx "4" "$S/proc/sys/kernel/perf_event_paranoid"
check "linger off again" test ! -e "$P/var/lib/systemd/linger/$ME"
check "sleep targets unmasked" test ! -s "$CTL/masked"
check "counters root-only again (400)" mode_is "$PKG_E" "400 $GRP"
check "governor not group-writable again (644)" mode_is "$GOV0" "644 $GRP"
check "old sudoers rule not restored" test ! -e "$P/etc/sudoers.d/99-thesis-pilot"
undo
check "undo again: nothing to undo" out_has "^UNDO DONE: the setup is not installed"

echo "4. things that were like this before the setup stay like this after the undo"
fresh
echo sleep.target > "$CTL/masked"
touch "$P/var/lib/systemd/linger/$ME"
setup
check "setup: SETUP DONE" bash -c "(( $RC == 0 ))"
check "setup masked only the four other targets" state_has "masked_by_setup=suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target"
undo
check "sleep.target still masked after the undo" grep -qx sleep.target "$CTL/masked"
check "linger still on after the undo" test -e "$P/var/lib/systemd/linger/$ME"
check "disable-linger not called" test ! -e "$CTL/loginctl_calls"

echo "5. problems are reported, not hidden"
fresh
USER_OVERRIDE="" setup
check "no user (not started with sudo): clear message, exit 1" bash -c "(( $RC == 1 )) && grep -q 'from your own (normal) user account' <<< \"\$1\"" _ "$OUT"
OUT="$(env -u BENCH_SETUP_TEST bash "$BENCH/setup_root.sh" 2>&1 < /dev/null)"; RC=$?
check "not root: 'needs root', exit 1" bash -c "(( $RC == 1 )) && grep -q 'needs root' <<< \"\$1\"" _ "$OUT"
touch "$CTL/runuser_deny" "$CTL/fail_enable"
setup
check "exit 1, SETUP INCOMPLETE" bash -c "(( $RC == 1 )) && grep -q '^SETUP INCOMPLETE' <<< \"\$1\"" _ "$OUT"
check "boot service problem reported" out_has "^FAIL  systemctl enable"
check "user check problem reported" out_has "^FAIL  as $ME not readable/writable"
rm -f "$CTL/runuser_deny" "$CTL/fail_enable"

echo
echo "passed: $PASS   failed: $FAIL"
(( FAIL == 0 ))
