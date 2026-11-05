# IOKernel telemetry

## Running

```
sudo ./iokerneld ias numanode 0 telemetry 4 /data/run01 120
```

CPU 4 is an example. Choose an application-eligible CPU on socket 0 whose physical
core does not host the IOKernel dataplane or control thread. The scheduler reserves
both that CPU and its SMT sibling. The writer is a Linux pthread with affinity
set at creation. Exclude unrelated Linux tasks/IRQs separately in machine setup.

The path is an output **prefix**. This command creates four new files:

| File | Record size | Contents | Ring capacity |
| --- | ---: | --- | ---: |
| `/data/run01.bw.bin` | 16 bytes | TSC and channel-0 CAS counter | 65,536 records (1 MiB) |
| `/data/run01.llc.bin` | 32 bytes | TSC, assignment generation, LLC counter, CPU, PID | 1,048,576 records (32 MiB) |
| `/data/run01.pmc-failure.bin` | 24 bytes | Observation TSC, assignment generation, CPU, reason | 4,096 records (96 KiB) |
| `/data/run01.sched.bin` | 24 bytes | Decision TSC, new generation, PID, CPU, reason | 262,144 records (6 MiB) |

Every file is created exclusively; existing files are never overwritten. Startup
failure can leave incomplete files, so use a fresh prefix for the next attempt.

For the same application CPU allocation with collection disabled:

```
sudo ./iokerneld ias numanode 0 telemetry 4 - 120
```

`-` reserves the physical core without buffers, files, or a writer. Without the
option no extra core is reserved. Capture requires IAS and bandwidth monitoring.
The existing PCM initialization uses socket argument 0; use socket 0 initially.

Duration (1–86400 seconds) starts at the first measurement, normally immediately
after IOKernel initialization, not when an application connects. Include warmup
in the duration and select the analysis window offline. At expiry the scheduler
stops submitting; the writer drains every stream, synchronizes data, updates each
header with counts and a completion flag, synchronizes again, closes, and exits.
Caladan keeps running. Wait for `telemetry: all streams drained and closed` before
terminating it. SIGINT/SIGTERM or a crash do not drain gracefully. No signal
handlers are installed. Any writer error invalidates the run even if some streams
were already finalized.

## Rings and writer

Only the scheduler/dataplane thread may submit telemetry. Each stream has its own
single-producer/single-consumer ring, independent drop count, and file. Typed
submission functions construct only that stream's fields; shared helpers handle
publication, batching, file initialization, and completion. There are no per-record
type tags or footer records. Fixed-size slots match their stream's on-disk record.
The 32-bit CAS reading is widened to 64 bits for a naturally aligned 16-byte record.

Head and tail have separate cache lines and acquire/release publication. A full
ring drops new records and never blocks the scheduler. Other streams can still
accept data. Spare space in one ring cannot absorb another stream's overflow.

The dedicated writer polls with PAUSE and visits streams in round-robin passes.
Each pass writes at most one approximately 256 KiB batch per stream. It waits for
a full batch during capture and flushes partial batches only at capture end. A
ring smaller than a batch uses its capacity as the threshold. A wrapped batch
uses two memory regions in one writev call; partial writes can require retries.
Slots remain occupied until the entire batch is accepted. Partial writes and
EINTR are handled. A blocked disk write still stalls the shared writer for all
streams; batch fairness does not eliminate storage stalls.

No formatting, allocation, file I/O, or mutex occurs in submission. Buffers are
allocated and prefaulted after dataplane initialization; first-touch follows Linux
NUMA policy, rather than enforcing binding or locking memory. Buffered writes
also consume page cache. Compare repeated enabled/disabled runs on each server
with identical application CPU allocation to measure collection interference.

The measurement-only bandwidth policy is retained; other IAS controllers are
unaffected. Queue-delay and application processing-time capture remain future work.

## Inspecting a first capture

On node0, run `./scripts/cloudlab/smoke.sh summarize` for the latest capture, or
`python3 scripts/summarize_telemetry.py /path/to/prefix` for a specific capture.
This streams binary records without writing CSV or loading the dataset into RAM.
It reports interval cadence, the five largest gaps with capture-relative times,
estimated mean bandwidth, per-core/PID LLC deltas and
rates, skipped invalid intervals, and failure counts. Cross-stream experiment and
hardware metadata must match. Bandwidth CAS wraps are handled modulo 2^32; LLC
counter decreases are skipped because the file does not encode counter width.
Interval percentiles are approximate from a bounded systematic sample.

The LLC record window indicates sampling coverage, not confirmed request activity.
Client/server TSC clocks are not automatically aligned. Summary validation is not
predictor evaluation or proof of acceptable instrumentation overhead.

## Decoding and format

See [format version history](telemetry-format-versions.md) for layouts and changes
from versions 1 through 5. The current decoder supports versions 4 and 5.

```
python3 scripts/decode_telemetry.py /data/run01.bw.bin > /data/run01.bw.csv
python3 scripts/decode_telemetry.py /data/run01.llc.bin > /data/run01.llc.csv
python3 scripts/decode_telemetry.py /data/run01.pmc-failure.bin > /data/run01.pmc-failure.csv
python3 scripts/decode_telemetry.py /data/run01.sched.bin > /data/run01.sched.csv
```

The decoder streams CSV to stdout and metadata/counts to stderr. It validates
completion, layout, declared record count, truncation, and trailing data. Align
streams by TSC, not record position: each can have different rates and drops.

Version 5 is little-endian. Version 4 remains supported; versions 1–3 are rejected. The fixed 96-byte header:

- 8-byte magic `CALATLM\0`.
- u32 version, total header bytes, record bytes, stream type.
- f64 TSC cycles/us; u64 PMC selector (unused/zero for bandwidth).
- u32 writer CPU, scheduler CPU, application CPU count, flags.
- f64 bandwidth multiplier (used only for bandwidth).
- u64 successfully enqueued records, dropped records.
- 16-byte experiment UUID, stored in standard UUID byte order.

One random version-4 UUID is generated per capture and copied into all stream
headers. The decoder prints it as `experiment_id=...`. Match IDs before combining
streams, even if files have been renamed or moved. Separate captures receive
separate IDs; pairing baseline/enabled runs is external experiment metadata.

Exactly CPU-count u32 CPU IDs follow. Header size includes this list and is
independent of NCPU. Initially flags/counts are zero; after draining, flag bit 0
indicates completion and counts are finalized. A failed or interrupted file is
incomplete. Size assertions protect the versioned layouts; sizes alone do not
describe fields, so incompatible layout changes require a version bump.

Record layouts, identified once per file by stream type:

1. Bandwidth: u64 TSC, u64 CAS. CAS contains the raw 32-bit counter, widened.
   TSC is read just before CAS. Subtract CAS modulo 2^32, divide by elapsed TSC,
   and multiply by the header bandwidth multiplier for estimated decimal MB/s.
   Assumes fewer than 2^32 CAS events between selected endpoints.
2. LLC: u64 TSC, u64 assignment generation, u64 misses, u32 CPU, u32 PID.
   TSC is read immediately after the hardware counter on the target CPU. Compute
   intervals only between matching CPU and generation; account for hardware
   counter width on wrap. Versions 4/5 do not encode hardware counter width.
3. PMC failure: u64 observation TSC, u64 assignment generation, u32 CPU,
   u32 reason (1 = request still pending at gather). This is not a counter reading.

4. Scheduler event (version 5): u64 TSC, u64 new generation, u32 PID, u16 CPU,
   u16 reason: 1 = worker scheduled, 2 = idle placeholder, 3 = explicitly idled.
   For reason 3 the PID is the previous process (zero if none). These are scheduler
   decisions; actual execution on the target core may happen later. Raw timestamps
   and generations allow matching these events to LLC records. The summary counts
   each reason by core/PID. Old version-4 captures have no scheduler events.

Generation mismatches are skipped. Inspect timestamp gaps, failures, and per-stream
drops before analysis. Drop counts are aggregate per stream, not per CPU.

## Tests

```
cc -D_GNU_SOURCE -std=gnu11 -O2 -Wall -Iinc -pthread tests/telemetry/writer_test.c -o /tmp/telemetry-writer-test
# Supply a fresh prefix; the test creates four stream files.
/tmp/telemetry-writer-test /tmp/unique-test-prefix
cc -D_GNU_SOURCE -std=gnu11 -O2 -Wall -Iinc -pthread tests/telemetry/writev_test.c -o /tmp/telemetry-writev-test
/tmp/telemetry-writev-test
python3 tests/telemetry/decoder_test.py
```

The C test exercises the actual stream writer, independent overflows, concurrent
ordering across wraps, completion counts, partial-batch waiting/draining, and disk
failure propagation without NIC/ksched dependencies. Python covers all layouts,
variable CPU counts, incomplete/corrupt files. Hardware throughput, affinity, NUMA
placement, and latency overhead still require experiment-server validation.
