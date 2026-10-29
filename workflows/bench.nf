#!/usr/bin/env nextflow

/*
 * bench.nf - starts ONE measured run of the fixed-work benchmark as a Nextflow task
 *
 * The task calls scripts/bench/run_case.sh, which measures the run and decides
 * whether it is valid (exit 0) or has to be repeated (exit 3). A failed run is
 * retried until params.max_attempts attempts are used up, then recorded as
 * failed ('ignore'); Nextflow still exits 0 and the session goes on.
 *
 * Every attempt gets its own folder <runs_dir>/<run_id>/a<n>; n counts on
 * across Nextflow calls (the session driver makes a repair pass for runs that
 * are still missing at the end), so no attempt is ever overwritten.
 *
 * Why one run per Nextflow call: Nextflow does not keep the order of a list of
 * inputs. With 20 inputs, maxForks 1 and queueSize 1, Nextflow 26.04.4 started
 * them for example as 8 3 2 7 4 1 5 6 9 10 ... (the same with `fair true`;
 * tested 2026-10-29). The session must follow its seeded random order exactly,
 * so the session driver calls Nextflow once per run, in schedule order. The
 * start-up (about 3 s) lies before the measurement window, while the laptop
 * cools down anyway.
 *
 * The old fixed-time workflows (main.nf, baseline.nf, mixed_load.nf,
 * granularity.nf) are kept unchanged: they produced the October data.
 *
 * Usage (from the session driver; -C makes bench.config the ONLY config, so
 * no other Nextflow config on the machine can change these settings):
 *   nextflow -C workflows/bench.config run workflows/bench.nf \
 *       --spec <run spec json> --session_env <file> --runs_dir <dir> \
 *       [--max_attempts 3] [--bench_dir <dir>]
 */

nextflow.enable.dsl = 2

process RUN_CASE {
    tag "${run_id}"
    maxForks 1
    maxRetries 10
    errorStrategy { task.attempt < (params.max_attempts as int) ? 'retry' : 'ignore' }

    input:
    tuple val(run_id), val(spec), val(bench_dir)

    script:
    """
    # next free attempt folder (also counts attempts of earlier Nextflow calls)
    n=1
    while [[ -e "${params.runs_dir}/${run_id}/a\${n}" ]]; do n=\$((n + 1)); done
    # global number of the last attempt of this call (run_case keeps a run that
    # is only 'disturbed' when it is the last attempt)
    last=\$((n - ${task.attempt} + ${params.max_attempts}))
    bash "${bench_dir}/run_case.sh" "${params.session_env}" "${spec}" \\
        "${params.runs_dir}/${run_id}/a\${n}" "\${n}" "\${last}"
    """
}

workflow {
    if (!params.spec || !params.session_env || !params.runs_dir) {
        error("bench.nf needs --spec, --session_env and --runs_dir")
    }
    def bench_dir = params.bench_dir ?: "${projectDir}/../scripts/bench"
    def spec = params.spec.toString()
    def run_id = spec.tokenize('/')[-1].replaceAll(/\.json$/, '')
    RUN_CASE(channel.of(tuple(run_id, spec, bench_dir)))
}
