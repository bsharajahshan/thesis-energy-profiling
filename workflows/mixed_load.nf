#!/usr/bin/env nextflow

// mixed_load.nf - RQ2: Mixed-Load Behaviour (CPU + I/O Interaction)

nextflow.enable.dsl = 2

// Sequential execution: CPU task then I/O task
process SEQUENTIAL_MIXED {
    input:
    tuple val(cpu_intensity), val(rep_id)

    output:
    path "sequential_mixed_*.csv"

    script:
    """
    # TODO: Implement sequential execution
    # 1. Run CPU task (measure RAPL)
    # 2. Run I/O task (measure RAPL)
    # 3. Write CSV row with total energy and runtime

    echo "sequential_mixed,sequential,${cpu_intensity},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > sequential_mixed_${rep_id}.csv
    """
}

// Parallel execution: CPU + I/O tasks overlapped
process PARALLEL_COMPLEMENTARY {
    input:
    tuple val(cpu_intensity), val(rep_id)

    output:
    path "parallel_complementary_*.csv"

    script:
    """
    # TODO: Implement parallel complementary execution
    # 1. Start CPU task in background
    # 2. Start I/O task in background
    # 3. Wait for both to complete (measure RAPL for total duration)
    # 4. Write CSV row

    echo "parallel_complementary,parallel,${cpu_intensity},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > parallel_complementary_${rep_id}.csv
    """
}

// Parallel competing execution: CPU + CPU tasks (control condition)
process PARALLEL_COMPETING {
    input:
    tuple val(cpu_intensity), val(rep_id)

    output:
    path "parallel_competing_*.csv"

    script:
    """
    # TODO: Implement parallel competing execution (CPU + CPU)
    # 1. Start two CPU tasks in parallel
    # 2. Measure RAPL for total duration
    # 3. Write CSV row

    echo "parallel_competing,parallel,${cpu_intensity},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > parallel_competing_${rep_id}.csv
    """
}

workflow mixed_load_workflow {
    // Intensity ratios: 25/75, 50/50, 75/25 CPU-to-I/O
    cpu_ratios = Channel.of(25, 50, 75)
    rep_ids = Channel.of(1..params.repetitions.toInteger())

    // Run all three configurations
    SEQUENTIAL_MIXED(cpu_ratios.combine(rep_ids))
    PARALLEL_COMPLEMENTARY(cpu_ratios.combine(rep_ids))
    PARALLEL_COMPETING(cpu_ratios.combine(rep_ids))
}
