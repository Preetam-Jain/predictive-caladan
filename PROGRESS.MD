# Predictive Caladan: Progress

As of October 29, 2026. This report reflects the supplied seminar proposal and the repository contents reviewed on this date; it does not establish that experiments have run on external machines.

## Project objective

Determine whether recent interference signals can predict future application latency or throughput degradation. The proposal begins with coarse load measurements, then adds Caladan telemetry: memory bandwidth, LLC misses, and request processing time. Prediction is evaluated offline; workload-specific proactive scheduling policies are a later extension.

## Completed groundwork

- The Caladan source tree is available, including the IOKernel, application runtime, ksched kernel module, load generator, and interference workloads/configurations.
- A substantial source-reading guide exists under `docs/source-guide/`: `MASTER.md` and component guides covering runtime, IOKernel, and ksched behavior. This provides architectural groundwork for selecting instrumentation points.
- The proposal defines the research question, experiment/resource plan, completion levels, and milestones: October 23 for setup, November 13 for data collection/model training progress, and November 30 for evaluation and presentation.
- Source inspection has located existing measurement paths: queueing delay in `iokernel/sched.c`, memory-bandwidth estimation and conditional LLC-counter sampling in `iokernel/ias_bw.c`, and application performance output in `apps/loadgen/src/main.rs`.
- Server request handlers in `apps/loadgen/src/server.rs` provide concrete boundaries for future request processing-time instrumentation.

## Current implementation status

The inspected tree does not yet contain a project-specific telemetry-to-disk pipeline. Existing IOKernel statistics are coarse monitoring output, not the proposed fine-grained dataset. No experiment logs, assembled dataset, trained models, or evaluation results were established by this review. Successful builds, machine setup, and workload execution remain unverified here; the presence of build/configuration files is not evidence of successful execution.

Existing measurement semantics also need care:

- IAS subcontrollers target a 10-microsecond polling interval; actual sample times must be recorded rather than assumed to be exactly periodic.
- Memory bandwidth is estimated from one monitored memory channel and scaled by the active-channel count.
- Existing LLC sampling is triggered by bandwidth pressure and excludes latency-critical processes. It does not provide continuous coverage of every application.
- Scheduler delay measures queueing pressure, not request service time or client end-to-end latency.
- Disabling interference-control actions while retaining measurements requires separating measurement from policy; disabling the bandwidth controller also removes its measurements.

## Immediate next step

Implement local disk telemetry capture with timestamped records copied into a bounded memory buffer by the measurement thread and drained in batches by a separate writer. Preserve raw counter intervals, application/core attribution, validity information, and dropped-record counts. Start with bandwidth and scheduler-delay capture, then add continuous LLC sampling and explicit server processing-time records. Keep load-generator latency/throughput output as a separate performance stream that can be aligned with server measurements.

This step has been scoped but is not implemented. Only this progress document was added during the review; source code was not changed.

## Remaining project work

1. Verify the build, supported hardware counters, and repeatable workload execution on the experiment machines.
2. Implement and validate disk logging, including counter/sample validity, shutdown draining, and writer failures.
3. Measure logging overhead against a logging-disabled baseline and choose sustainable sampling rates.
4. Establish a measurement-only experiment configuration with controlled CPU allocations while keeping telemetry active.
5. Collect coarse baseline and fine-grained interference/performance data, then construct time-aligned datasets without future-data leakage.
6. Train and evaluate predictors across prediction horizons; consider hard-coded scheduling policies only after the prediction results justify them.

The proposal’s 75% scope (working workloads, telemetry collection, and a paired dataset) cannot yet be marked complete from the available repository evidence. RDMA offload remains optional follow-up work if local logging materially perturbs the workloads.
