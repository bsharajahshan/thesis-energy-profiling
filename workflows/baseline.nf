#!/usr/bin/env nextflow

// baseline.nf - RQ1: Isolated Subsystem Profiling

nextflow.enable.dsl = 2

// CPU baseline process
process CPU_BASELINE {
    publishDir "${params.results_dir}/rq1_baseline/cpu", mode: 'copy', overwrite: true

    input:
    tuple val(threads), val(rep_id)

    output:
    path "cpu_baseline_${threads}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq1_cpu_baseline"
    TASK_NAME="cpu_baseline"
    MODE="sequential"
    SUBSYSTEM="cpu"
    INTENSITY="${threads}"
    TASK_COUNT="1"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Run RAPL measurement wrapper
    RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "stress-ng --cpu ${threads} --timeout ${params.duration}s" "\${TASK_NAME}")

    # Parse output (key=value format) and extract values
    DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
    ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
    ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    DRAM_ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_UJ=" | cut -d= -f2)
    DRAM_ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_J=" | cut -d= -f2)

    # Write CSV row: experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,dram_energy_uj,dram_energy_j,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${SUBSYSTEM},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${DRAM_ENERGY_UJ},\${DRAM_ENERGY_J},\${RUN_ID},\${TIMESTAMP}" > cpu_baseline_${threads}_${rep_id}.csv
    """
}

// Memory baseline process
process MEM_BASELINE {
    maxForks 1
    publishDir "${params.results_dir}/rq1_baseline/memory", mode: 'copy', overwrite: true

    input:
    tuple val(intensity_gb), val(rep_id)

    output:
    path "mem_baseline_${intensity_gb}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq1_mem_baseline"
    TASK_NAME="mem_baseline"
    MODE="sequential"
    SUBSYSTEM="memory"
    INTENSITY="${intensity_gb}"
    TASK_COUNT="1"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Run RAPL measurement wrapper
    # Allocate memory: intensity in GB
    RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "stress-ng --vm 1 --vm-bytes ${intensity_gb}G --timeout ${params.duration}s" "\${TASK_NAME}")

    # Parse output
    DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
    ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
    ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    DRAM_ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_UJ=" | cut -d= -f2)
    DRAM_ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_J=" | cut -d= -f2)

    # Write CSV row: experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,dram_energy_uj,dram_energy_j,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${SUBSYSTEM},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${DRAM_ENERGY_UJ},\${DRAM_ENERGY_J},\${RUN_ID},\${TIMESTAMP}" > mem_baseline_${intensity_gb}_${rep_id}.csv
    """
}

// I/O baseline process
process IO_BASELINE {
    publishDir "${params.results_dir}/rq1_baseline/io", mode: 'copy', overwrite: true

    input:
    tuple val(io_pattern), val(rep_id)

    output:
    path "io_baseline_${io_pattern}_${rep_id}.csv"

    script:
    """
    EXPERIMENT_ID="rq1_io_baseline"
    TASK_NAME="io_baseline"
    MODE="sequential"
    SUBSYSTEM="io"
    INTENSITY="${io_pattern}"
    TASK_COUNT="1"
    RUN_ID="${rep_id}"
    TIMESTAMP=\$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Map pattern names to fio rw modes
    case "${io_pattern}" in
      sequential) FIO_RW="write" ;;
      random) FIO_RW="randwrite" ;;
      *) FIO_RW="write" ;;
    esac

    # Run RAPL measurement wrapper with fio
    RAPL_OUTPUT=\$(bash ${projectDir}/../scripts/measure_rapl.sh "fio --ioengine=libaio --iodepth=16 --blocksize=4k --name=io_job --rw=\${FIO_RW} --size=1G --runtime=${params.duration} --time_based" "\${TASK_NAME}")

    # Parse output
    DURATION_S=\$(echo "\${RAPL_OUTPUT}" | grep "^DURATION_S=" | cut -d= -f2)
    ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_UJ=" | cut -d= -f2)
    ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^ENERGY_J=" | cut -d= -f2)
    DRAM_ENERGY_UJ=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_UJ=" | cut -d= -f2)
    DRAM_ENERGY_J=\$(echo "\${RAPL_OUTPUT}" | grep "^DRAM_ENERGY_J=" | cut -d= -f2)

    # Write CSV row: experiment_id,task_name,mode,subsystem,intensity,task_count,duration_s,energy_uj,energy_j,dram_energy_uj,dram_energy_j,run_id,timestamp
    echo "\${EXPERIMENT_ID},\${TASK_NAME},\${MODE},\${SUBSYSTEM},\${INTENSITY},\${TASK_COUNT},\${DURATION_S},\${ENERGY_UJ},\${ENERGY_J},\${DRAM_ENERGY_UJ},\${DRAM_ENERGY_J},\${RUN_ID},\${TIMESTAMP}" > io_baseline_${io_pattern}_${rep_id}.csv

    # Delete fio's 1 GiB test file (fio keeps it by default). Runs after the
    # RAPL window has closed and the CSV is written, so it is not measured.
    # Without this, every I/O task leaves 1 GiB in work/ (40 GiB per full
    # session), which filled the test machine's disk on 2026-10-19.
    rm -f io_job.*
    """
}

workflow baseline_workflow {
    // Define load levels for each subsystem
    cpu_threads = Channel.of(1, 2, 4, 8)
    mem_intensities = Channel.of(1, 2, 4, 8)
    io_patterns = Channel.of("sequential", "random")

    // Create repetition IDs (1 to params.repetitions)
    rep_ids = Channel.of(1..params.repetitions.toInteger())

    // Execute baselines based on subsystem param
    if (params.subsystem == "cpu") {
        CPU_BASELINE(cpu_threads.combine(rep_ids))
    } else if (params.subsystem == "memory") {
        MEM_BASELINE(mem_intensities.combine(rep_ids))
    } else if (params.subsystem == "io") {
        IO_BASELINE(io_patterns.combine(rep_ids))
    }
}
