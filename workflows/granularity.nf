#!/usr/bin/env nextflow

// granularity.nf - RQ3: Task Granularity (1 large vs N small tasks)
//
// Design (see CLAUDE.md Phase 3 / DEVLOG 2026-08-21..25 for full rationale):
// - Subsystems: CPU and RAM only, no I/O (parallel I/O splitting would
//   conflate granularity with the sequential-vs-random variable RQ1 already
//   tests).
// - Main experiment is PARALLEL N-way splitting (Betreuer-confirmed);
//   sequential is a secondary reference kept to isolate pure per-task
//   startup overhead from genuine concurrency/contention effects
//   (E_parallel - E_sequential).
// - Resource division per sub-task, NOT duration: each of the N sub-tasks
//   runs for the FULL baseline duration; it's the per-task resource share
//   that divides by N (cores for CPU, GB for RAM) — confirmed with Philipp
//   to avoid massive oversubscription (N=8 full-core sub-tasks would be 64
//   threads on a 4c/8t machine) while keeping total concurrent parallelism
//   matched to the N=1 baseline.
// - RAM sub-tasks are NOT bandwidth-capped below full capacity (Philipp's
//   explicit choice) — any resulting contention effect is a reportable
//   finding, not something to engineer around. This may make RAM results
//   non-comparable to CPU results; that must be discussed in the thesis.
// - CSV schema: general schema as-is, no additional columns (unlike RQ2).
//   intensity is fixed at "max", matching RQ1/RQ2's MAX-intensity
//   convention.
// - No per-process maxForks guard needed here: nextflow.config caps the
//   local executor's whole resource pool at one task's worth, so Nextflow
//   can never run this (or any other) process's task instances
//   concurrently — required because RAPL is one shared system-wide
//   counter and an overlapping measure_rapl.sh window silently corrupts
//   both readings.

nextflow.enable.dsl = 2

process CPU_GRANULARITY {
    publishDir "${params.results_dir}/rq3_granularity/cpu", mode: 'copy', overwrite: true

    input:
    tuple val(task_count), val(exec_mode), val(rep_id)

    output:
    path "cpu_granularity_${exec_mode}_${task_count}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq3_cpu_granularity"
    TASK_NAME="cpu_granularity"
    MODE="${exec_mode}"
    SUBSYSTEM="cpu"
    INTENSITY="max"
    TASK_COUNT="${task_count}"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    THREADS_PER_TASK=\$((8 / ${task_count}))
    SUBTASK_CMD="stress-ng --cpu \${THREADS_PER_TASK} --timeout ${params.duration}s"

    if [[ "${exec_mode}" == "sequential" ]]; then
        # N sub-tasks back-to-back, own RAPL windows, summed.
        TOTAL_DURATION="0"
        TOTAL_ENERGY_UJ="0"
        for i in \$(seq 1 ${task_count}); do
            RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "\${SUBTASK_CMD}" "cpu_granularity_seq_\${i}")
            D=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
            E=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
            TOTAL_DURATION=\$(echo "\${TOTAL_DURATION} + \${D}" | bc)
            TOTAL_ENERGY_UJ=\$(echo "\${TOTAL_ENERGY_UJ} + \${E}" | bc)
        done
        DURATION_S="\${TOTAL_DURATION}"
        ENERGY_UJ="\${TOTAL_ENERGY_UJ}"
        ENERGY_J=\$(echo "scale=6; \${TOTAL_ENERGY_UJ} / 1000000" | bc)
    else
        # Parallel: N sub-tasks launched together inside a subshell so the
        # bare `wait` only blocks on them, not on measure_rapl.sh's own
        # backgrounded memory sampler (same pattern as mixed_load.nf).
        PARALLEL_CMD="("
        for i in \$(seq 1 ${task_count}); do
            PARALLEL_CMD="\${PARALLEL_CMD} (\${SUBTASK_CMD}) &"
        done
        PARALLEL_CMD="\${PARALLEL_CMD} wait )"

        RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "\${PARALLEL_CMD}" "cpu_granularity_par")
        DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
        ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
        ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    fi

    # CSV row: experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${SUBSYSTEM},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${RUN_ID},\${TIMESTAMP}" > cpu_granularity_${exec_mode}_${task_count}_${rep_id}.csv
    """
}

process RAM_GRANULARITY {
    publishDir "${params.results_dir}/rq3_granularity/ram", mode: 'copy', overwrite: true

    input:
    tuple val(task_count), val(exec_mode), val(rep_id)

    output:
    path "ram_granularity_${exec_mode}_${task_count}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq3_ram_granularity"
    TASK_NAME="ram_granularity"
    MODE="${exec_mode}"
    SUBSYSTEM="ram"
    INTENSITY="max"
    TASK_COUNT="${task_count}"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    GB_PER_TASK=\$((8 / ${task_count}))
    # Deliberately NOT bandwidth-capped — Philipp's confirmed choice.
    # Parallel workers compete for full memory bandwidth; any resulting
    # contention is a reportable finding (see CLAUDE.md RAM non-comparability
    # note), not something this script should hide.
    SUBTASK_CMD="stress-ng --vm 1 --vm-bytes \${GB_PER_TASK}G --timeout ${params.duration}s"

    if [[ "${exec_mode}" == "sequential" ]]; then
        TOTAL_DURATION="0"
        TOTAL_ENERGY_UJ="0"
        for i in \$(seq 1 ${task_count}); do
            RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "\${SUBTASK_CMD}" "ram_granularity_seq_\${i}")
            D=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
            E=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
            TOTAL_DURATION=\$(echo "\${TOTAL_DURATION} + \${D}" | bc)
            TOTAL_ENERGY_UJ=\$(echo "\${TOTAL_ENERGY_UJ} + \${E}" | bc)
        done
        DURATION_S="\${TOTAL_DURATION}"
        ENERGY_UJ="\${TOTAL_ENERGY_UJ}"
        ENERGY_J=\$(echo "scale=6; \${TOTAL_ENERGY_UJ} / 1000000" | bc)
    else
        PARALLEL_CMD="("
        for i in \$(seq 1 ${task_count}); do
            PARALLEL_CMD="\${PARALLEL_CMD} (\${SUBTASK_CMD}) &"
        done
        PARALLEL_CMD="\${PARALLEL_CMD} wait )"

        RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "\${PARALLEL_CMD}" "ram_granularity_par")
        DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
        ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
        ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    fi

    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${SUBSYSTEM},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${RUN_ID},\${TIMESTAMP}" > ram_granularity_${exec_mode}_${task_count}_${rep_id}.csv
    """
}

workflow granularity_workflow {
    // Task count sweep: 1, 2, 4, 8 sub-tasks, each mode, each repetition.
    task_counts = Channel.of(1, 2, 4, 8)
    exec_modes  = Channel.of("parallel", "sequential")
    rep_ids     = Channel.of(1..params.repetitions.toInteger())

    combos = task_counts.combine(exec_modes).combine(rep_ids)

    CPU_GRANULARITY(combos)
    RAM_GRANULARITY(combos)
}
