#!/usr/bin/env nextflow

// granularity.nf - RQ3: Task Granularity (1 large vs N small tasks)

nextflow.enable.dsl = 2

// Fixed total work, variable granularity
process GRANULARITY_SWEEP {
    publishDir "${params.results_dir}/rq3_granularity", mode: 'copy', overwrite: true

    input:
    val task_count
    val rep_id

    output:
    path "granularity_*.csv"

    script:
    """
    # TODO: Implement task granularity sweep
    # Hold total work constant (e.g., 120s of CPU work total)
    # Vary task splitting: 1x120s, 2x60s, 4x30s, 8x15s
    #
    # 1. Verify work equivalence (CPU instruction count) across all N values
    # 2. Execute N tasks in sequence (no parallelism within repetition)
    # 3. Measure RAPL for entire duration
    # 4. Write CSV row

    task_duration=\$((${params.duration} / ${task_count}))
    echo "granularity_sweep,sequential,${task_count},\${task_duration},${params.duration},0,0,${rep_id},\$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > granularity_${rep_id}.csv
    """
}

workflow granularity_workflow {
    // Task count sweep: 1, 2, 4, 8
    task_counts = Channel.of(1, 2, 4, 8)
    rep_ids = Channel.of(1..params.repetitions.toInteger())

    GRANULARITY_SWEEP(task_counts.combine(rep_ids))
}
