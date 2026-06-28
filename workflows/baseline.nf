#!/usr/bin/env nextflow

// baseline.nf - RQ1: Isolated Subsystem Profiling

nextflow.enable.dsl = 2

// CPU baseline process
process CPU_BASELINE {
    input:
    val threads
    val rep_id

    output:
    path "cpu_baseline_*.csv"

    script:
    """
    # TODO: Implement RAPL measurement + stress-ng CPU workload
    # 1. Read RAPL counter before
    # 2. Run: stress-ng --cpu ${threads} --timeout ${params.duration}s
    # 3. Read RAPL counter after
    # 4. Write CSV row: experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,run_id,timestamp

    echo "cpu_baseline,cpu,${threads},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > cpu_baseline_${rep_id}.csv
    """
}

// Memory baseline process
process MEM_BASELINE {
    input:
    val intensity
    val rep_id

    output:
    path "mem_baseline_*.csv"

    script:
    """
    # TODO: Implement RAPL measurement + stress-ng memory workload
    # 1. Read RAPL counter before
    # 2. Run: stress-ng --vm 1 --vm-bytes ${intensity}G --timeout ${params.duration}s
    # 3. Read RAPL counter after
    # 4. Write CSV row

    echo "mem_baseline,memory,${intensity},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > mem_baseline_${rep_id}.csv
    """
}

// I/O baseline process
process IO_BASELINE {
    input:
    val io_pattern
    val rep_id

    output:
    path "io_baseline_*.csv"

    script:
    """
    # TODO: Implement RAPL measurement + fio I/O workload
    # 1. Read RAPL counter before
    # 2. Run: fio with ${io_pattern} pattern for ${params.duration}s
    # 3. Read RAPL counter after
    # 4. Write CSV row

    echo "io_baseline,io,${io_pattern},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > io_baseline_${rep_id}.csv
    """
}

workflow baseline_workflow {
    // Define load levels for each subsystem
    cpu_threads = Channel.of(1, 2, 4, 8)
    mem_intensities = Channel.of(1, 2, 4, 8)
    io_patterns = Channel.of("sequential", "random")

    // Create repetition IDs (1 to params.repetitions)
    rep_ids = Channel.of(1..params.repetitions as Integer)

    // Execute baselines based on subsystem param
    if (params.subsystem == "cpu") {
        CPU_BASELINE(cpu_threads.combine(rep_ids), rep_ids)
    } else if (params.subsystem == "memory") {
        MEM_BASELINE(mem_intensities.combine(rep_ids), rep_ids)
    } else if (params.subsystem == "io") {
        IO_BASELINE(io_patterns.combine(rep_ids), rep_ids)
    }
}
