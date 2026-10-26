#!/bin/bash
# sensors.sh - small sysfs readers for run_case.sh (sourced, not run).
# Every reader prints NA when the file does not exist, so a missing sensor never stops a run.
# SYSFS_ROOT can point to a fake tree for tests.

SYSFS_ROOT="${SYSFS_ROOT:-}"

# Package temperature in degrees C (thermal zone of type x86_pkg_temp).
read_pkg_temp_c() {
    local z
    for z in "${SYSFS_ROOT}"/sys/class/thermal/thermal_zone*; do
        [[ -r "$z/type" ]] || continue
        if [[ "$(cat "$z/type" 2>/dev/null)" == "x86_pkg_temp" ]]; then
            awk -v t="$(cat "$z/temp" 2>/dev/null)" 'BEGIN { if (t == "") print "NA"; else printf "%.1f", t / 1000 }'
            return
        fi
    done
    echo NA
}

# Thermal throttle event counters. Prints: package_count,core_count_sum
read_throttle_counts() {
    local base="${SYSFS_ROOT}/sys/devices/system/cpu" pkg="NA" core=0 f seen=0 v
    if [[ -r "$base/cpu0/thermal_throttle/package_throttle_count" ]]; then
        pkg="$(cat "$base/cpu0/thermal_throttle/package_throttle_count" 2>/dev/null)"
    fi
    for f in "$base"/cpu[0-9]*/thermal_throttle/core_throttle_count; do
        [[ -r "$f" ]] || continue
        v="$(cat "$f" 2>/dev/null)"; core=$((core + v)); seen=1
    done
    [[ $seen -eq 1 ]] || core="NA"
    echo "${pkg},${core}"
}

# RAPL package power limits (constraint 0 = long term, constraint 1 = short term) in W.
# Prints: pl_long_w,pl_short_w
read_rapl_limits() {
    local root="${RAPL_ROOT:-/sys/class/powercap/intel-rapl}/intel-rapl:0" out="" c f
    for c in 0 1; do
        f="$root/constraint_${c}_power_limit_uw"
        if [[ -r "$f" ]]; then
            out="${out}$(awk -v u="$(cat "$f" 2>/dev/null)" 'BEGIN { printf "%.2f", u / 1000000 }'),"
        else
            out="${out}NA,"
        fi
    done
    echo "${out%,}"
}
