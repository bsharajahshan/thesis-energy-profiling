#!/usr/bin/env python3

"""
measure_rapl.py - RAPL Energy Measurement Reader (Python version)

Usage:
    result = measure_rapl(command, task_name, params)
    energy_uj = result['energy_uj']
    duration_s = result['duration_s']
"""

import subprocess
import time
import sys
from pathlib import Path
from typing import Dict, Any

RAPL_PATH = Path("/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj")
RAPL_MAX = 2**32 - 1  # 32-bit counter wraps


def read_rapl() -> int:
    """Read current RAPL counter in microjoules."""
    if not RAPL_PATH.exists():
        raise FileNotFoundError(f"RAPL not available at {RAPL_PATH}")
    return int(RAPL_PATH.read_text().strip())


def measure_rapl(command: str, task_name: str = "task", params: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Measure energy consumption of a command using RAPL.

    Args:
        command: Shell command to execute
        task_name: Human-readable task name
        params: Optional dict of experiment parameters

    Returns:
        Dict with keys: duration_ns, duration_s, energy_uj, energy_j, power_w, exit_code
    """
    if params is None:
        params = {}

    # 1. Read RAPL before
    energy_before = read_rapl()
    start_ns = time.time_ns()

    # 2. Run command
    result = subprocess.run(command, shell=True, capture_output=True, text=True)

    # 3. Read RAPL after
    end_ns = time.time_ns()
    energy_after = read_rapl()

    # 4. Handle wraparound
    if energy_after < energy_before:
        energy_uj = (RAPL_MAX - energy_before) + energy_after
    else:
        energy_uj = energy_after - energy_before

    duration_ns = end_ns - start_ns
    duration_s = duration_ns / 1e9
    energy_j = energy_uj / 1e6
    power_w = energy_j / duration_s if duration_s > 0 else 0

    return {
        "task_name": task_name,
        "duration_ns": duration_ns,
        "duration_s": duration_s,
        "energy_uj": energy_uj,
        "energy_j": energy_j,
        "power_w": power_w,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: measure_rapl.py <command> [task_name]")
        sys.exit(1)

    cmd = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "task"

    result = measure_rapl(cmd, name)
    print(f"Task: {result['task_name']}")
    print(f"Duration: {result['duration_s']:.3f} s")
    print(f"Energy: {result['energy_j']:.6f} J ({result['energy_uj']} µJ)")
    print(f"Power: {result['power_w']:.3f} W")
    print(f"Exit code: {result['exit_code']}")
