#!/usr/bin/env python3

"""
preflight.py - checks the laptop before a session and writes the session settings

run.sh calls this before every start, also when a stopped session is
continued. It checks everything the session needs, fixes what it can (power
profile), and reports ALL problems at once, before the long run starts:

  FAIL  the session could not give valid data: it is not started
  WARN  the session starts; the summary repeats the warning
  OK    checked and fine
  INFO  recorded for the thesis

Writes into <session>:
  session.env      absolute tool paths, sysfs paths, perf events and the
                   session rules (read by session.sh, run_case.sh, benchlib.py)
  preflight.txt    the report
  preflight/       raw tool output (versions, perf test output: the parsers
                   are re-tested on these real files after the pilot)
  schedule.csv, calibration.env, git.txt   copies used by the session (kept
                   unchanged when a session is continued)
Exit 0 = ready, 1 = not ready.

Usage: preflight.py --step STEP --repo DIR --results DIR --session DIR --schedule CSV --calibration ENV

Test hooks (environment): BENCH_SYSROOT (prefix for /sys, /proc and /etc),
BENCH_EXTRA_PATH (searched first for tools), BENCH_IO_DIR, BENCH_DISK_STAT,
BENCH_SKIP_GIT. Standard library only (Python 3.10+).
"""

import argparse
import filecmp
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import benchlib  # noqa: E402
import cases  # noqa: E402

SYSROOT = os.environ.get("BENCH_SYSROOT", "")

REQUIRED_TOOLS = ["stress-ng", "fio", "perf", "nextflow", "java", "python3", "flock", "taskset", "setsid",
                  "timeout", "tar", "ps", "pgrep", "df", "sha256sum", "awk", "sleep"]
OPTIONAL_TOOLS = ["systemd-run", "systemd-inhibit", "systemctl", "powerprofilesctl", "git"]
PERF_REQUIRED = ["instructions", "cycles", "ref-cycles", "uncore_imc/data_reads/", "uncore_imc/data_writes/"]
PERF_OPTIONAL = ["msr/tsc/", "power/energy-pkg/", "power/energy-ram/", "context-switches", "cpu-migrations"]
SETUP_HINT = "run the one-time setup in your own terminal: sudo bash scripts/bench/setup_root.sh"
SESSION_DEFAULTS = {
    "MAX_ATTEMPTS": "3", "AC_WAIT_MAX_S": "600", "BATTERY_STOP_PCT": "30", "POWER_POLL_S": "30",
    "GRACE_MAX_S": "1200", "WAIT_FOR_GONE": "opencode hermes firefox chrome chromium code",
    "BG_CPU_MAX": "0.15", "BREAK_S": "600", "POWER_PROFILE": "balanced",
}


def P(path):
    """A system path, inside BENCH_SYSROOT when testing."""
    return SYSROOT + path


def read1(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def run(cmd, timeout=30, env=None, cwd=None):
    """-> (exit code, stdout + stderr). Never raises; never reads stdin."""
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=env, cwd=cwd, text=True, errors="replace")
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return 124, out + f"\n(timed out after {timeout} s)"
    except OSError as e:
        return 126, str(e)


class Report:
    def __init__(self):
        self.lines = []
        self.fails = 0
        self.warns = 0

    def add(self, level, topic, text):
        self.lines.append(f"{level:<5} {topic:<12} {text}")
        if level == "FAIL":
            self.fails += 1
        elif level == "WARN":
            self.warns += 1


def save(raw_dir, name, text):
    with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
        f.write(text)


# ---- tools --------------------------------------------------------------------------------

def login_shell_paths():
    """PATH of the user's login and interactive shells (the agent's shell may have another PATH)."""
    found = []
    for flags in ("-lc", "-ic"):
        rc, out = run(["bash", flags, 'printf "\\n__PATH__%s\\n" "$PATH"'], timeout=10)
        for line in out.splitlines():
            if line.startswith("__PATH__"):
                found += line[len("__PATH__"):].split(":")
    return found


def search_dirs():
    home = os.path.expanduser("~")
    dirs = [d for d in os.environ.get("BENCH_EXTRA_PATH", "").split(":") if d]
    dirs += os.environ.get("PATH", "").split(":")
    if not SYSROOT:
        dirs += login_shell_paths()
    dirs += [f"{home}/.local/bin", f"{home}/bin", home, "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
             "/usr/bin", "/sbin", "/bin", "/snap/bin", f"{home}/.sdkman/candidates/java/current/bin"]
    dirs += sorted(glob.glob("/usr/lib/jvm/*/bin"))
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def find_tool(name, dirs):
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)
    return None


def check_tools(rep, env, raw):
    dirs = search_dirs()
    tools = {}
    missing = []
    for name in REQUIRED_TOOLS + OPTIONAL_TOOLS:
        tools[name] = find_tool(name, dirs)
        if not tools[name] and name in REQUIRED_TOOLS:
            missing.append(name)
    if missing:
        rep.add("FAIL", "tools", "not found: " + ", ".join(missing) + " (searched: " + ":".join(dirs[:12]) + " ...)")
    versions = []
    for name, args in (("stress-ng", ["--version"]), ("fio", ["--version"]), ("perf", ["--version"]),
                       ("java", ["-version"])):
        if tools.get(name):
            rc, out = run([tools[name]] + args, timeout=20)
            save(raw, f"version_{name}.txt", out)
            versions.append(f"{name} {out.strip().splitlines()[0] if out.strip() else '?'}")
    if not missing:
        rep.add("OK", "tools", "all found; " + "; ".join(versions))
    for name in OPTIONAL_TOOLS:
        if not tools[name]:
            rep.add("INFO", "tools", f"{name} not found (optional)")
    keymap = {"stress-ng": "STRESS_NG", "fio": "FIO", "perf": "PERF", "nextflow": "NEXTFLOW", "python3": "PYTHON",
              "flock": "FLOCK", "taskset": "TASKSET", "sleep": "SLEEP_BIN", "timeout": "TIMEOUT_BIN",
              "systemd-inhibit": "SYSTEMD_INHIBIT", "tar": "TAR", "sha256sum": "SHA256SUM"}
    for name, key in keymap.items():
        env[key] = tools.get(name) or ""
    save(raw, "tool_paths.txt", "".join(f"{k} {v or '-'}\n" for k, v in tools.items()))
    return tools


def check_nextflow(rep, env, tools, raw):
    java, nxf = tools.get("java"), tools.get("nextflow")
    if not java or not nxf:
        return
    java_home = os.environ.get("JAVA_HOME") or ""
    if not os.path.isfile(os.path.join(java_home, "bin", "java")):
        java_home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
    nxf_home = os.environ.get("NXF_HOME") or os.path.join(os.path.expanduser("~"), ".nextflow")
    base = {"HOME": os.path.expanduser("~"), "PATH": f"{java_home}/bin:/usr/bin:/bin", "JAVA_HOME": java_home,
            "NXF_HOME": nxf_home, "NXF_DISABLE_CHECK_LATEST": "true", "LC_ALL": "C"}
    if os.environ.get("NXF_VER"):
        base["NXF_VER"] = os.environ["NXF_VER"]
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = run([nxf, "-version"], timeout=180, env=dict(base, NXF_OFFLINE="true"), cwd=tmp)
        if rc != 0:      # its files are not downloaded yet: allow one download, then check offline again
            run([nxf, "-version"], timeout=600, env=base, cwd=tmp)
            rc, out = run([nxf, "-version"], timeout=180, env=dict(base, NXF_OFFLINE="true"), cwd=tmp)
    save(raw, "version_nextflow.txt", out)
    m = re.search(r"version\s+(\d+\.\d+\.\d+)", out)
    if rc != 0 or not m:
        rep.add("FAIL", "nextflow", f"`nextflow -version` failed without internet (exit {rc}); see preflight/version_nextflow.txt")
        return
    env.update(JAVA_HOME=java_home, NXF_HOME=nxf_home, NXF_VER=m.group(1))
    rep.add("OK", "nextflow", f"{m.group(1)} works offline (java {java_home})")


# ---- measurement hardware -----------------------------------------------------------------------

def readable_counter(path):
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def check_rapl(rep, env):
    base = P("/sys/class/powercap/intel-rapl:0")
    if not os.path.exists(base):
        rep.add("FAIL", "rapl", f"{base} does not exist (no Intel RAPL?)")
        return
    pkg = os.path.realpath(base)          # a symlink on this laptop; `find` does not follow it
    dram = ""
    for d in sorted(glob.glob(os.path.join(pkg, "intel-rapl:0:*"))):
        if read1(os.path.join(d, "name")) == "dram":
            dram = d
    mmio = P("/sys/class/powercap/intel-rapl-mmio:0")
    mmio = os.path.realpath(mmio) if os.path.exists(mmio) else ""
    env.update(RAPL_PKG_DIR=pkg, RAPL_DRAM_DIR=dram, RAPL_MMIO_DIR=mmio)
    problems = []
    if readable_counter(os.path.join(pkg, "energy_uj")) is None:
        problems.append("package energy_uj not readable")
    if not dram:
        problems.append("no DRAM domain")
    elif readable_counter(os.path.join(dram, "energy_uj")) is None:
        problems.append("DRAM energy_uj not readable")
    if problems:
        rep.add("FAIL", "rapl", "; ".join(problems) + f" -> {SETUP_HINT}")
    else:
        rep.add("OK", "rapl", f"package and DRAM readable ({pkg}; dram {os.path.basename(dram)})")
    rep.add("INFO", "rapl", "limits: " + limits_text(pkg) + ("; mmio: " + limits_text(mmio) if mmio else "; no intel-rapl-mmio"))


def limits_text(d):
    parts = []
    for k in range(4):
        uw = read1(os.path.join(d, f"constraint_{k}_power_limit_uw"))
        if uw is not None:
            parts.append(f"{read1(os.path.join(d, f'constraint_{k}_name'))} {int(uw) / 1e6:g} W")
    return ", ".join(parts) or "none readable"


def check_perf(rep, env, tools, raw):
    perf = tools.get("perf")
    sleep = tools.get("sleep") or "sleep"
    paranoid = read1(P("/proc/sys/kernel/perf_event_paranoid"))
    rep.add("INFO", "perf", f"perf_event_paranoid = {paranoid}")
    if not perf:
        return
    supported, unsupported = [], []
    with tempfile.TemporaryDirectory() as tmp:
        out_file = os.path.join(tmp, "p.csv")
        for ev in PERF_REQUIRED + PERF_OPTIONAL:
            rc, msg = run([perf, "stat", "-a", "-x,", "-o", out_file, "-e", ev, "--", sleep, "0.2"], timeout=30)
            text = benchlib.read_text(out_file)
            rec = benchlib.parse_perf_csv(text, [ev])["totals"].get(ev) if rc == 0 else None
            (supported if rec and rec["value"] is not None else unsupported).append(ev)
            save(raw, "perf_event_" + re.sub(r"[^A-Za-z0-9]+", "_", ev).strip("_") + ".txt",
                 f"exit {rc}\n--- stdout/stderr\n{msg}\n--- {out_file}\n{text}")
            if os.path.exists(out_file):
                os.remove(out_file)
        missing_req = [e for e in PERF_REQUIRED if e not in supported]
        if missing_req:
            hint = SETUP_HINT if paranoid not in ("-1", "0") else "see preflight/perf_event_*.txt"
            rep.add("FAIL", "perf", "cannot count " + ", ".join(missing_req) + f" -> {hint}")
            return
        events = [e for e in PERF_REQUIRED + PERF_OPTIONAL if e in supported]
        env["PERF_EVENTS"] = ",".join(events)
        # the exact command run_case.sh uses: interval mode with a summary
        for summary in (True, False):
            args = [perf, "stat", "-a", "-x,", "-I", "200"] + (["--summary"] if summary else []) + \
                   ["-o", out_file, "-e", env["PERF_EVENTS"], "--", sleep, "1"]
            rc, msg = run(args, timeout=30)
            text = benchlib.read_text(out_file)
            save(raw, f"perf_interval{'_summary' if summary else ''}.csv", text)
            parsed = benchlib.parse_perf_csv(text, events)
            if rc == 0 and parsed["series"]:
                env["PERF_SUMMARY"] = "1" if summary and parsed["source"] == "summary" else "0"
                low = [e for e, r in parsed["totals"].items() if r["pct"] is not None and r["pct"] < 99]
                text_events = f"{len(events)} events ({', '.join(unsupported) + ' not available' if unsupported else 'all'})"
                if low:
                    rep.add("WARN", "perf", f"{text_events}; shared counters (multiplexing): {', '.join(low)}")
                else:
                    rep.add("OK", "perf", f"{text_events}; interval mode{' with summary' if env['PERF_SUMMARY'] == '1' else ' without --summary'}")
                return
        rep.add("FAIL", "perf", "interval mode (-I) gave no output; see preflight/perf_interval*.csv")


def check_temperature(rep, env):
    temp = ""
    for d in sorted(glob.glob(P("/sys/class/hwmon/hwmon*"))):
        if read1(os.path.join(d, "name")) != "coretemp":
            continue
        for label in sorted(glob.glob(os.path.join(d, "temp*_label"))):
            if read1(label) == "Package id 0":
                temp = label[:-len("_label")] + "_input"
    if not temp:
        for z in sorted(glob.glob(P("/sys/class/thermal/thermal_zone*"))):
            if read1(os.path.join(z, "type")) == "x86_pkg_temp":
                temp = os.path.join(z, "temp")
    v = readable_counter(temp) if temp else None
    if v is None or not 5000 <= v <= 110000:
        rep.add("FAIL", "temperature", "no readable package temperature (hwmon coretemp 'Package id 0' or x86_pkg_temp)")
        return
    env["TEMP_FILE"] = temp
    rep.add("OK", "temperature", f"package {v / 1000:g} C ({temp})")
    thr = P("/sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count")
    rep.add("OK" if os.path.exists(thr) else "WARN", "throttle",
            "throttle counters present" if os.path.exists(thr) else "no thermal_throttle counters")


def check_power(rep, env, tools, cal):
    ac = bat = ""
    for d in sorted(glob.glob(P("/sys/class/power_supply/*"))):
        kind = read1(os.path.join(d, "type"))
        if kind == "Mains" and not ac:
            ac = os.path.join(d, "online")
        elif kind == "Battery" and not bat:
            bat = os.path.join(d, "capacity")
    env.update(AC_FILE=ac, BATTERY_FILE=bat)
    cap = read1(bat) if bat else None
    if not ac:
        rep.add("WARN", "power", "no mains power supply found; the cable cannot be checked")
    elif read1(ac) != "1":
        rep.add("FAIL", "power", "power cable not plugged in -> plug it in and leave it in")
    else:
        rep.add("OK", "power", f"power cable in; battery {cap if cap is not None else '?'} %")
    want = cal.get("POWER_PROFILE") or SESSION_DEFAULTS["POWER_PROFILE"]
    ppc = tools.get("powerprofilesctl")
    if not ppc:
        rep.add("INFO", "profile", "powerprofilesctl not found; EPP is set directly for every run")
        return
    rc, now = run([ppc, "get"], timeout=10)
    now = now.strip()
    if now != want:
        run([ppc, "set", want], timeout=10)
        rc, after = run([ppc, "get"], timeout=10)
        if after.strip() == want:
            rep.add("OK", "profile", f"power profile was '{now}', set to '{want}'")
        else:
            rep.add("WARN", "profile", f"power profile is '{after.strip()}', could not set '{want}'; EPP is set directly for every run")
    else:
        rep.add("OK", "profile", f"power profile '{want}'")


def parse_cpu_list(text):
    return cases.parse_cpu_list(text or "")


def check_cpu(rep, env, cal, rows):
    base = P("/sys/devices/system/cpu")
    online = parse_cpu_list(read1(os.path.join(base, "online")) or "")
    if not online:
        online = sorted(int(m.group(1)) for d in glob.glob(os.path.join(base, "cpu[0-9]*"))
                        if (m := re.search(r"cpu(\d+)$", d)))
    env.update(CPU_SYSFS=base, NCPU=str(len(online)))
    groups = set()
    for c in online:
        sib = read1(os.path.join(base, f"cpu{c}", "topology", "thread_siblings_list"))
        groups.add(tuple(parse_cpu_list(sib)) if sib else (c,))
    siblings = ";".join(",".join(map(str, g)) for g in sorted(groups))
    env["CPU_SIBLINGS"] = siblings
    needs_smt = any(r["case_id"] == "mech_pin_shared_core" for r in rows)
    try:
        pins = cases.pin_sets(siblings)
        if needs_smt and "@all_but_mem_cpu" not in pins:
            rep.add("FAIL", "cpu", f"no second hardware thread per core ({siblings}); the shared-core case cannot run")
        else:
            rep.add("OK", "cpu", f"{len(online)} logical CPUs, cores {siblings}; memory worker pinned to CPU {pins['@mem_cpu']}")
    except ValueError as e:
        rep.add("FAIL", "cpu", str(e))
    rep.add("INFO", "cpu", f"intel_pstate {read1(os.path.join(base, 'intel_pstate', 'status'))}, "
                           f"no_turbo {read1(os.path.join(base, 'intel_pstate', 'no_turbo'))}")
    if read1(os.path.join(base, "intel_pstate", "no_turbo")) == "1":
        rep.add("WARN", "cpu", "turbo is switched off (intel_pstate/no_turbo = 1)")

    govs = sorted(glob.glob(os.path.join(base, "cpu[0-9]*", "cpufreq", "scaling_governor")))
    epps = sorted(glob.glob(os.path.join(base, "cpu[0-9]*", "cpufreq", "energy_performance_preference")))
    gov_now = sorted({read1(g) or "?" for g in govs})     # '?' = file not readable
    epp_now = sorted({read1(e) or "?" for e in epps})
    rep.add("INFO", "governor", f"now: governor {','.join(map(str, gov_now))}, EPP {','.join(map(str, epp_now))}")
    need_gov = any(cases.CASE_BY_ID[r["case_id"]]["governor"] == "control" for r in rows)
    problems = []
    if not govs:
        problems.append("no cpufreq governor files")
    writable = lambda f: os.path.isfile(f) and os.access(f, os.W_OK)  # noqa: E731
    if need_gov and not all(writable(g) for g in govs):
        problems.append("governor not writable (needed for the governor control runs)")
    if epps and not all(writable(e) for e in epps) and epp_now != [cal["EPP_MAIN"]]:
        problems.append(f"EPP not writable and not {cal['EPP_MAIN']}")
    avail = read1(os.path.join(base, "cpu0", "cpufreq", "scaling_available_governors")) or ""
    for g in {cal["GOVERNOR_MAIN"]} | ({cal["GOVERNOR_CONTROL"]} if need_gov else set()):
        if avail and g not in avail.split():
            problems.append(f"governor {g} not available ({avail})")
    avail_epp = read1(os.path.join(base, "cpu0", "cpufreq", "energy_performance_available_preferences")) or ""
    if avail_epp and cal["EPP_MAIN"] not in avail_epp.split():
        problems.append(f"EPP {cal['EPP_MAIN']} not available ({avail_epp})")
    if problems:
        rep.add("FAIL", "governor", "; ".join(problems) + f" -> {SETUP_HINT}")
    else:
        rep.add("OK", "governor", f"main runs {cal['GOVERNOR_MAIN']} / {cal['EPP_MAIN']}"
                + (f", control runs {cal['GOVERNOR_CONTROL']} (writable)" if need_gov else ""))


def meminfo():
    d = {}
    try:
        with open(P("/proc/meminfo"), encoding="ascii") as f:
            for line in f:
                k, _, rest = line.partition(":")
                d[k] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return d


def check_memory(rep, env, cal):
    m = meminfo()
    need_kb = 8 * 1024 ** 2 + int(benchlib.SESSION_DEFAULTS["MEM_MARGIN_KB"])
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    if total < need_kb + 2 * 1024 ** 2:
        rep.add("FAIL", "memory", f"only {total / 1024 ** 2:.1f} GiB RAM; the 8 GB memory job needs about {need_kb / 1024 ** 2 + 2:.0f} GiB")
    elif avail < need_kb:
        rep.add("WARN", "memory", f"only {avail / 1024 ** 2:.1f} GiB free now; close programs (runs wait for free memory)")
    else:
        rep.add("OK", "memory", f"{avail / 1024 ** 2:.1f} of {total / 1024 ** 2:.1f} GiB free")


def fs_type(path):
    """File system type of the mount that holds path (from mountinfo), or ''."""
    best, kind = "", ""
    real = os.path.realpath(path)
    try:
        with open(P("/proc/self/mountinfo"), encoding="utf-8") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                mnt = left.split()[4] if len(left.split()) > 4 else ""
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, kind = mnt, right.split()[0]
    except OSError:
        pass
    return kind


def disk_stat_for(path):
    """/sys/block/<disk>/stat of the disk that holds path, and whether it spins."""
    st = os.stat(path)
    dev = P(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
    if not os.path.exists(dev):
        return "", None
    d = os.path.realpath(dev)
    if os.path.exists(os.path.join(d, "partition")):
        d = os.path.dirname(d)
    return os.path.join(d, "stat"), read1(os.path.join(d, "queue", "rotational"))


def check_disk(rep, env, cal, results):
    home = os.path.expanduser("~")
    io_dir = os.environ.get("BENCH_IO_DIR", "")
    why = "test setting"
    if not io_dir:
        same_fs = os.stat("/tmp").st_dev == os.stat(home).st_dev
        kind = fs_type("/tmp")
        if same_fs and kind not in ("tmpfs", "ramfs"):
            io_dir, why = "/tmp/thesis-bench-io", "on the same disk as before, emptied at every reboot"
        else:
            io_dir, why = os.path.join(results, "io"), f"/tmp is {kind or 'on another disk'}"
    os.makedirs(io_dir, exist_ok=True)
    disk_stat = os.environ.get("BENCH_DISK_STAT", "")
    rotational = None
    if not disk_stat:
        disk_stat, rotational = disk_stat_for(io_dir)
    env.update(IO_DIR=io_dir, DISK_STAT=disk_stat)
    io_bytes = int(cal["IO_BYTES"])
    keep_kb = int(cal.get("DISK_KEEP_FREE_KB") or benchlib.SESSION_DEFAULTS["DISK_KEEP_FREE_KB"])
    need = io_bytes + keep_kb * 1024 + 1024 ** 3          # fio file + kept free + about 1 GiB of results
    free = shutil.disk_usage(io_dir).free
    gib = 1024 ** 3
    if free < need:
        rep.add("FAIL", "disk", f"{free / gib:.1f} GiB free at {io_dir}; the session needs {need / gib:.1f} GiB "
                                f"(fio file {io_bytes / gib:.1f} + {keep_kb / 1024 ** 2:.0f} kept free + 1 for results)")
    else:
        rep.add("OK", "disk", f"{free / gib:.1f} GiB free, needs {need / gib:.1f}; fio file in {io_dir} ({why})")
    if rotational == "1":
        rep.add("WARN", "disk", f"{io_dir} is on a spinning disk, not the SSD")
    if os.path.realpath(results) != os.path.realpath(io_dir) and shutil.disk_usage(results).free < 1024 ** 3:
        rep.add("FAIL", "disk", f"less than 1 GiB free for the results at {results}")


def check_cgroup(rep, env):
    root = P("/sys/fs/cgroup")
    if os.path.exists(os.path.join(root, "cgroup.controllers")):
        env["CGROUP_ROOT"] = root
        rep.add("OK", "cgroup", "cgroup v2: CPU time of other programs is measured in every run")
    else:
        env["CGROUP_ROOT"] = ""
        rep.add("WARN", "cgroup", "no cgroup v2; CPU time of other programs cannot be measured")


def proc_cpu_times():
    """{pid: (name, utime+stime ticks)} from /proc."""
    out = {}
    for d in glob.glob(P("/proc/[0-9]*")):
        try:
            with open(os.path.join(d, "stat"), encoding="utf-8", errors="replace") as f:
                s = f.read()
            name = s[s.index("(") + 1:s.rindex(")")]
            fields = s[s.rindex(")") + 2:].split()
            out[int(os.path.basename(d))] = (name, int(fields[11]) + int(fields[12]))
        except (OSError, ValueError, IndexError):
            continue
    return out


def check_programs(rep, env, cal):
    a = proc_cpu_times()
    time.sleep(2)
    b = proc_cpu_times()
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    busy = []
    for pid, (name, t) in b.items():
        if pid in a and pid != os.getpid():
            pct = (t - a[pid][1]) / tick / 2 * 100
            if pct >= 2:
                busy.append((pct, name))
    busy.sort(reverse=True)
    names = {v[0] for v in b.values()}
    watch = (cal.get("WAIT_FOR_GONE") or SESSION_DEFAULTS["WAIT_FOR_GONE"]).split()
    running = sorted(n for n in watch if n in names)
    if busy:
        rep.add("INFO", "programs", "busy now: " + ", ".join(f"{n} {p:.0f}%" for p, n in busy[:8]))
    if running:
        rep.add("WARN", "programs", "running: " + ", ".join(running) + " -> close them after STARTED "
                f"(the session waits up to {int(float(cal.get('GRACE_MAX_S') or SESSION_DEFAULTS['GRACE_MAX_S'])) // 60} min for them)")
    else:
        rep.add("OK", "programs", "no agent, browser or editor running")


def check_git(rep, env, repo, step, tools, session):
    if os.environ.get("BENCH_SKIP_GIT"):
        rep.add("INFO", "git", "not checked (test setting)")
        return
    git = tools.get("git")
    if not git:
        rep.add("WARN", "git", "git not found; the code version is not recorded")
        return
    rc, head = run([git, "-C", repo, "rev-parse", "HEAD"], timeout=20)
    rc2, changed = run([git, "-C", repo, "status", "--porcelain", "--untracked-files=no", "--",
                        "scripts/bench", "workflows"], timeout=20)
    rc3, branch = run([git, "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
    save(session, "git.txt", f"commit {head.strip()}\nbranch {branch.strip()}\nchanged tracked files:\n{changed}")
    if rc != 0 or rc2 != 0:
        rep.add("WARN", "git", "could not read the git state: " + (head + changed).strip().splitlines()[-1])
        return
    env["GIT_COMMIT"] = head.strip()
    if changed.strip():
        level = "FAIL" if step == "main" else "WARN"
        rep.add(level, "git", "benchmark files differ from the commit: " + " ".join(changed.split()[1::2][:6])
                + " -> git checkout -- scripts/bench workflows")
    else:
        rep.add("OK", "git", f"commit {head.strip()[:10]} ({branch.strip()}), benchmark files unchanged")


# ---- plan files -----------------------------------------------------------------------------

def check_plan(rep, a):
    """Copies schedule and calibration into the session (first start only) and checks them."""
    sched_copy = os.path.join(a.session, "schedule.csv")
    cal_copy = os.path.join(a.session, "calibration.env")
    for src, dst, what in ((a.schedule, sched_copy, "schedule"), (a.calibration, cal_copy, "calibration")):
        if not os.path.exists(src):
            rep.add("FAIL", what, f"{src} does not exist")
            return None, None
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
        elif not filecmp.cmp(src, dst, shallow=False):
            rep.add("WARN", what, f"{os.path.basename(src)} changed since this session started; the session keeps its own copy")
    try:
        cal = cases.load_calibration(cal_copy)
        rows = cases.read_schedule(sched_copy)
    except (OSError, ValueError, KeyError) as e:
        rep.add("FAIL", "plan", str(e))
        return None, None
    unknown = sorted({r["case_id"] for r in rows} - set(cases.CASE_BY_ID))
    if unknown:
        rep.add("FAIL", "plan", "unknown cases in the schedule: " + ", ".join(unknown))
        return None, None
    done = sum(os.path.exists(os.path.join(a.session, "runs", r["run_id"], "VALID")) for r in rows)
    rep.add("OK", "plan", f"{len(rows)} runs planned, {done} already done; job sizes from {os.path.basename(a.calibration)}")
    return cal, rows


def write_session_env(path, env):
    lines = ["# session.env - written by preflight.py " + datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "# read by session.sh, run_case.sh and benchlib.py; do not edit by hand"]
    lines += [f"{k}={shlex.quote(str(v))}" for k, v in env.items()]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="check the laptop before a session")
    ap.add_argument("--step", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--calibration", required=True)
    a = ap.parse_args(argv)
    raw = os.path.join(a.session, "preflight")
    os.makedirs(raw, exist_ok=True)
    rep = Report()
    env = {"STEP": a.step, "REPO": os.path.abspath(a.repo), "RESULTS_DIR": os.path.abspath(a.results),
           "SESSION_DIR": os.path.abspath(a.session), "LOCK_FILE": os.path.join(os.path.abspath(a.results), ".window.lock"),
           "PROC_STAT": P("/proc/stat"), "PROC_MEMINFO": P("/proc/meminfo")}
    rep.add("INFO", "start", f"{a.step} preflight {datetime.now(timezone.utc).isoformat(timespec='seconds')}; "
                             f"one-time setup {'installed' if os.path.exists(P('/etc/thesis-bench/setup.txt')) else 'NOT found'}")
    cal, rows = check_plan(rep, a)
    tools = {}
    steps = [
        ("tools", lambda: tools.update(check_tools(rep, env, raw))),
        ("nextflow", lambda: check_nextflow(rep, env, tools, raw)),
        ("rapl", lambda: check_rapl(rep, env)),
        ("perf", lambda: check_perf(rep, env, tools, raw)),
        ("temperature", lambda: check_temperature(rep, env)),
        ("power", lambda: cal is not None and check_power(rep, env, tools, cal)),
        ("cpu", lambda: cal is not None and check_cpu(rep, env, cal, rows)),
        ("memory", lambda: cal is not None and check_memory(rep, env, cal)),
        ("disk", lambda: cal is not None and check_disk(rep, env, cal, a.results)),
        ("cgroup", lambda: check_cgroup(rep, env)),
        ("programs", lambda: cal is not None and check_programs(rep, env, cal)),
        ("git", lambda: check_git(rep, env, a.repo, a.step, tools, a.session)),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # a check must never stop the others
            rep.add("FAIL", name, f"check crashed: {type(e).__name__}: {e}")
    if cal is not None:
        for key in ("SETTLE_TEMP_C", "SETTLE_MIN_S", "SETTLE_MAX_S", "OTHER_CPU_LIMIT", "DISK_KEEP_FREE_KB",
                    "OPS_SEMANTICS", "BREAK_S", "SETTLE_FALLBACK_S", "WATCHDOG_FACTOR", "WATCHDOG_MIN_EXTRA_S"):
            if cal.get(key):
                env[key] = cal[key]
        for key, default in SESSION_DEFAULTS.items():
            env[key] = cal.get(key) or default
    write_session_env(os.path.join(a.session, "session.env"), env)
    verdict = "READY" if rep.fails == 0 else f"NOT READY: {rep.fails} problem(s), see the FAIL lines"
    text = "\n".join(rep.lines) + f"\nRESULT: {verdict}" + (f" ({rep.warns} warning(s))" if rep.warns else "") + "\n"
    with open(os.path.join(a.session, "preflight.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text, end="")
    return 0 if rep.fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
