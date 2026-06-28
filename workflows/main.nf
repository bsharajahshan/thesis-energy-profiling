#!/usr/bin/env nextflow

// main.nf - Energy Profiling Benchmark Entry Point

nextflow.enable.dsl = 2

include { baseline_workflow } from './baseline.nf'
include { mixed_load_workflow } from './mixed_load.nf'
include { granularity_workflow } from './granularity.nf'

workflow {
    if (params.mode == "baseline") {
        baseline_workflow()
    } else if (params.mode == "mixed") {
        mixed_load_workflow()
    } else if (params.mode == "granularity") {
        granularity_workflow()
    } else {
        error("Unknown mode: ${params.mode}. Use: baseline, mixed, or granularity")
    }
}
