#!/usr/bin/env nextflow

// mixed_load.nf - RQ2: Mixed-Load Behaviour (complementary subsystem combinations)
//
// Design (see CLAUDE.md Phase 2 / DEVLOG 2026-08-07 for full rationale):
// - Combinations: cpu_ram, cpu_io, ram_io, cpu_ram_io (all cross-subsystem,
//   i.e. complementary by construction — no same-resource "competing" condition).
// - Fixed intensity, MAX level, matched exactly to RQ1 (CPU=8 threads, RAM=8GB,
//   I/O=sequential) so results are directly comparable to RQ1 baseline data points.
// - Sequential mode: constituent workloads run one after another, each in its
//   own RAPL window (separate measure_rapl.sh calls), summed.
// - Parallel mode: constituent workloads launched together (backgrounded + wait)
//   as a single command string, so RAPL wraps one window over the combined run.
//   This is Nextflow-process-level sequential dispatch of a bash-level parallel
//   command — Nextflow-level process parallelism would not guarantee both
//   workloads fall inside the same RAPL measurement window.

nextflow.enable.dsl = 2

// Sequential execution: constituent workloads run one after another
process SEQUENTIAL_MIXED {
    publishDir "${params.results_dir}", mode: 'copy', overwrite: true

    input:
    tuple val(combo), val(rep_id)

    output:
    path "rq2_sequential_${combo}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq2_mixed"
    TASK_NAME="rq2_mixed"
    MODE="sequential"
    COMBINATION="${combo}"
    INTENSITY="max"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Fixed MAX-level constituent commands, matched exactly to RQ1 baselines
    CPU_CMD="stress-ng --cpu 8 --timeout ${params.duration}s"
    RAM_CMD="stress-ng --vm 1 --vm-bytes 8G --timeout ${params.duration}s"
    IO_CMD="fio --ioengine=libaio --iodepth=16 --blocksize=4k --name=io_job --rw=write --size=1G --runtime=${params.duration} --time_based"

    case "${combo}" in
      cpu_ram)     TASKS="cpu:\${CPU_CMD}|ram:\${RAM_CMD}";           TASK_COUNT=2 ;;
      cpu_io)      TASKS="cpu:\${CPU_CMD}|io:\${IO_CMD}";             TASK_COUNT=2 ;;
      ram_io)      TASKS="ram:\${RAM_CMD}|io:\${IO_CMD}";             TASK_COUNT=2 ;;
      cpu_ram_io)  TASKS="cpu:\${CPU_CMD}|ram:\${RAM_CMD}|io:\${IO_CMD}"; TASK_COUNT=3 ;;
      *) echo "Unknown combination: ${combo}" >&2; exit 1 ;;
    esac

    TOTAL_DURATION="0"
    TOTAL_ENERGY_UJ="0"
    WEIGHTED_CPU="0"
    WEIGHTED_MEM="0"

    IFS='|' read -ra TASK_ARR <<< "\$TASKS"
    for T in "\${TASK_ARR[@]}"; do
        NAME="\${T%%:*}"
        CMD="\${T#*:}"

        RAPL_OUTPUT=\$(bash \${projectDir}/../scripts/measure_rapl.sh "\${CMD}" "rq2_sequential_\${NAME}")

        D=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
        E=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
        CU=\$(echo "\${RAPL_OUTPUT}" | grep "^CPU_UTIL_PCT=" | cut -d= -f2)
        MU=\$(echo "\${RAPL_OUTPUT}" | grep "^MEM_UTIL_PCT=" | cut -d= -f2)

        TOTAL_DURATION=\$(echo "\${TOTAL_DURATION} + \${D}" | bc)
        TOTAL_ENERGY_UJ=\$(echo "\${TOTAL_ENERGY_UJ} + \${E}" | bc)
        WEIGHTED_CPU=\$(echo "\${WEIGHTED_CPU} + (\${CU} * \${D})" | bc)
        WEIGHTED_MEM=\$(echo "\${WEIGHTED_MEM} + (\${MU} * \${D})" | bc)
    done

    DURATION_S="\${TOTAL_DURATION}"
    ENERGY_UJ="\${TOTAL_ENERGY_UJ}"
    ENERGY_J=\$(echo "scale=6; \${TOTAL_ENERGY_UJ} / 1000000" | bc)
    # Duration-weighted average utilisation across the constituent runs
    if (( \$(echo "\${TOTAL_DURATION} > 0" | bc -l) )); then
        CPU_UTIL_PCT=\$(echo "scale=3; \${WEIGHTED_CPU} / \${TOTAL_DURATION}" | bc)
        MEM_UTIL_PCT=\$(echo "scale=3; \${WEIGHTED_MEM} / \${TOTAL_DURATION}" | bc)
    else
        CPU_UTIL_PCT="0"
        MEM_UTIL_PCT="0"
    fi

    # Write CSV row: experiment_id,task_name,mode,combination,intensity,task_count,duration_s,energy_uj,energy_j,cpu_util_pct,mem_util_pct,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${COMBINATION},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${CPU_UTIL_PCT},\${MEM_UTIL_PCT},\${RUN_ID},\${TIMESTAMP}" > rq2_sequential_${combo}_${rep_id}.csv
    """
}

// Parallel execution: constituent workloads launched together, single RAPL window
process PARALLEL_MIXED {
    publishDir "${params.results_dir}", mode: 'copy', overwrite: true

    input:
    tuple val(combo), val(rep_id)

    output:
    path "rq2_parallel_${combo}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq2_mixed"
    TASK_NAME="rq2_mixed"
    MODE="parallel"
    COMBINATION="${combo}"
    INTENSITY="max"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Fixed MAX-level constituent commands, matched exactly to RQ1 baselines
    CPU_CMD="stress-ng --cpu 8 --timeout ${params.duration}s"
    RAM_CMD="stress-ng --vm 1 --vm-bytes 8G --timeout ${params.duration}s"
    IO_CMD="fio --ioengine=libaio --iodepth=16 --blocksize=4k --name=io_job --rw=write --size=1G --runtime=${params.duration} --time_based"

    case "${combo}" in
      cpu_ram)     PARALLEL_CMD="(\${CPU_CMD}) & (\${RAM_CMD}) & wait";              TASK_COUNT=2 ;;
      cpu_io)      PARALLEL_CMD="(\${CPU_CMD}) & (\${IO_CMD}) & wait";               TASK_COUNT=2 ;;
      ram_io)      PARALLEL_CMD="(\${RAM_CMD}) & (\${IO_CMD}) & wait";               TASK_COUNT=2 ;;
      cpu_ram_io)  PARALLEL_CMD="(\${CPU_CMD}) & (\${RAM_CMD}) & (\${IO_CMD}) & wait"; TASK_COUNT=3 ;;
      *) echo "Unknown combination: ${combo}" >&2; exit 1 ;;
    esac

    RAPL_OUTPUT=\$(bash \${projectDir}/../scripts/measure_rapl.sh "\${PARALLEL_CMD}" "rq2_parallel_${combo}")

    DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
    ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
    ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    CPU_UTIL_PCT=\$(echo "\${RAPL_OUTPUT}" | grep "^CPU_UTIL_PCT=" | cut -d= -f2)
    MEM_UTIL_PCT=\$(echo "\${RAPL_OUTPUT}" | grep "^MEM_UTIL_PCT=" | cut -d= -f2)

    # Write CSV row: experiment_id,task_name,mode,combination,intensity,task_count,duration_s,energy_uj,energy_j,cpu_util_pct,mem_util_pct,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${COMBINATION},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${CPU_UTIL_PCT},\${MEM_UTIL_PCT},\${RUN_ID},\${TIMESTAMP}" > rq2_parallel_${combo}_${rep_id}.csv
    """
}

workflow mixed_load_workflow {
    // Complementary (cross-subsystem) combinations only — no same-resource
    // "competing" condition, per the actual RQ2 proposal text.
    combos = Channel.of("cpu_ram", "cpu_io", "ram_io", "cpu_ram_io")
    rep_ids = Channel.of(1..params.repetitions.toInteger())

    combo_reps = combos.combine(rep_ids)

    SEQUENTIAL_MIXED(combo_reps)
    PARALLEL_MIXED(combo_reps)
}
