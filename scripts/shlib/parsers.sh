#!/bin/bash
# parsers.sh - parsers for the tool outputs used by run_case.sh (sourced, not run).
# Only awk is used (no bc, no jq). Every function prints one comma-separated line;
# an empty field means "not found". Tested against the real stress-ng / fio / perf
# logs of 2026-10-15 (scripts/tests/run_tests.sh).

# stress-ng --metrics-brief summary line:
#   stress-ng: metrc: [pid] cpu   359270   60.00   401.73   0.28   5987.57   893.69
# $1 = log file, $2 = stressor name (cpu | vm). Prints: bogo_ops,real_s,usr_s,sys_s
stress_metrics() {
    awk -v name="$2" '
        $0 ~ ("\\] +" name " +[0-9]") {
            sub(/^.*\] +/, "")
            printf "%s,%s,%s,%s\n", $2, $3, $4, $5
            found = 1; exit
        }
        END { if (!found) print ",,," }' "$1" 2>/dev/null || echo ",,,"
}

# fio --output-format=json, write section of the first job.
# $1 = json file. Prints: io_bytes,iops,runtime_ms
fio_metrics() {
    awk -F: '
        /"write"[ ]*:/ { w = 1 }
        w && /"io_bytes"/ && !b { gsub(/[^0-9.]/, "", $2); bytes = $2; b = 1 }
        w && /"iops"/ && !i { gsub(/[^0-9.]/, "", $2); iops = $2; i = 1 }
        w && /"runtime"/ && !r { gsub(/[^0-9.]/, "", $2); rt = $2; r = 1 }
        END { printf "%s,%s,%s\n", bytes, iops, rt }' "$1" 2>/dev/null || echo ",,"
}

# perf stat -x, -o file lines:  value,unit,event,run_time,percent,...
# $1 = csv file. Prints: instructions,cycles,dram_read_mib,dram_write_mib
# (uncore_imc values are reported in MiB by perf; KiB and GiB are converted.)
perf_metrics() {
    awk -F, '
        function mib(v, u) { if (u == "KiB") return v / 1024; if (u == "GiB") return v * 1024; return v }
        $1 ~ /^[0-9.]+$/ {
            ev = $3
            if (ev ~ /^instructions/) ins += $1
            else if (ev ~ /^cycles/) cyc += $1
            else if (ev ~ /data_reads/) rd += mib($1, $2)
            else if (ev ~ /data_writes/) wr += mib($1, $2)
        }
        END {
            printf "%s,%s,%s,%s\n", (ins > 0 ? sprintf("%.0f", ins) : ""), (cyc > 0 ? sprintf("%.0f", cyc) : ""),
                (rd > 0 ? sprintf("%.2f", rd) : ""), (wr > 0 ? sprintf("%.2f", wr) : "")
        }' "$1" 2>/dev/null || echo ",,,"
}

# Reads KEY=value lines (the output of measure_rapl.sh) and prints the value of one key.
# $1 = output text, $2 = key
kv() { printf '%s\n' "$1" | grep "^$2=" | head -1 | cut -d= -f2; }

# Value of key $2 in a component spec "type:k=v,k=v" ($1), or $3 if the key is missing.
comp_get() {
    local spec="$1" key="$2" def="${3:-}" rest pair
    rest="${spec#*:}"
    [[ "$spec" == *:* ]] || { echo "$def"; return; }
    local IFS=','
    for pair in $rest; do
        if [[ "${pair%%=*}" == "$key" ]]; then echo "${pair#*=}"; return; fi
    done
    echo "$def"
}

comp_type() { echo "${1%%:*}"; }
