# Experiment findings so far (as of 2026-11-24)

What the CloudLab experiments have shown, with the numbers. The raw results
live in `results/` on the desktop, which is not committed; directory names are
given so they can be found there. "p99.9" is the loadgen client-measured
99.9th-percentile request latency. "Sent" is the rate loadgen actually sent.

## Setup

- Nodes: two CloudLab Clemson r650 (2 x 36-core Xeon 8360Y, 128 GiB per
  socket, ConnectX-6 100 Gb). Caladan runs on NUMA node 1; node 0 hosts the
  telemetry writer and the packet log buffer.
- Workload: memcached (20 B keys, 2 B values, 0.2% SET, 32M keys), loadgen with
  64 TCP connections on 34 client cores, directpath on, no core reallocation
  or other Caladan interventions (measurement only).
- Nov 22 runs used nodes clnode254/279; Nov 24 runs used clnode282/284.

## 1. Capacity depends on the nodes, and overload collapses instead of saturating

| Date | memcached cores | Highest rate that held | What happened above it |
| --- | ---: | --- | --- |
| Nov 22 | 1 | 0.74M (p99.9 <= 100 us) | collapse |
| Nov 22 | 4 | 2.32M (p99.9 <= 100 us) | collapse |
| Nov 22 | 16 | 6.08M | client TX backpressure |
| Nov 22 | 34 | 7.68M | client TX backpressure |
| Nov 24 | 16 | 12.2M (p99.9 88 us; 12.0M: 80 us) | 12.4M: sent dropped to 7.9M, stayed collapsed for all later steps |
| Nov 24 | 25 | 13.0M (p99.9 40 us); 16M sent 15.6M (p99.9 59 us) | 20M: sent 13.2M |

Sources: `20261122-peak-load-client34/summary.csv`,
`20261124T052910Z-929bc466`, `20261124T051825Z-3304d013`,
`20261124T050633Z-3618d588`.

- The Nov 24 nodes carried about twice the Nov 22 rate at 16 cores. Capacity
  must be re-measured on each reservation; do not reuse numbers across nodes.
- Past capacity, throughput does not plateau. Sent rate falls far below the
  offered rate, tens of millions of requests go unanswered, median latency
  becomes hundreds of ms, and later lower-rate steps in the same run do not
  recover. Seen on Nov 22 (1 core at 0.96M: sent 0.58M, p50 399 ms), Nov 24
  validation (4 cores at 3.5M: sent 0.14M) and Nov 24 16 cores at 12.4M.
  Runs must stay below the cliff; there is no graceful overload region.

## 2. Tail latency is higher at low load

25 memcached cores, no logging (`20261124T044850Z-653df204`), p99.9 by offered rate:

| Rate | 0.5M | 1M | 1.5M | 2M | 3M | 4M | 5M | 7M | 9M | 12M |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| p99.9 (us) | 85 | 77 | 66 | 56 | 40 | 33 | 29 | 28 | 28 | 35 |

Repeated in the same run and in separate runs. With fewer requests per core,
p99.9 is about 3x higher than near capacity. The cause has not been
investigated (likely per-request wakeup/polling cost when cores are mostly
idle). This matters for labels: "degradation" at low load is not caused by
interference.

## 3. The antagonists tried so far barely move memcached latency

- **STREAM DRAM, Nov 22** (`contention-sweep-20261122-223132`): 25 memcached
  cores at 5.08M, 1-8 STREAM cores, 256 MiB-2 GiB per worker. Socket-1 read
  bandwidth rose from 1.9 GB/s to 101 GB/s; p99.9 went from 28 to at most
  30 us.
- **Load sweeps with antagonists, Nov 24, logging off**
  (`20261124T045128Z-1e3b5794` swaptions-GC 9 threads,
  `20261124T045408Z-a7c0eae8` STREAM 9 cores x 1 GiB): p99.9 at every rate
  from 0.5M to 5M was within 1-2 us of memcached alone.
- **With telemetry and packet logging on, Nov 24**, 25 cores at 1M, the same
  STREAM antagonist did raise p99.9: alone 80-89 us; STREAM 1 core 79,
  4 cores 93, 9 cores 101-115 us (`20261124T0244..`-`0332..`). The logging-off
  sweep above shows no such effect at 1M (77 us both). This difference is not
  explained; it may involve the logging (node-0 writes, telemetry) rather than
  the antagonist alone. Needs a controlled logging on/off x antagonist on/off
  comparison.
- swaptions-GC throughput with memcached: 2.5k-18.6k ops/s (1-8 cores);
  STREAM: 46-285 ops/s (`logging-off-recovery-20261122-211836`).

Implication for the project: memcached with 2-byte values is not sensitive to
DRAM bandwidth contention in this configuration. Contention that hurts it will
likely need LLC/L2 pressure (an STREAM-L2 config, `scripts/experiments/stream_l2.toml`,
has been written but not run), hyperthread sharing, larger values, or fewer
memcached cores running closer to capacity.

## 4. Logging overhead

- IOKernel telemetry writer (Nov 22 logging matrix, 25 cores at 5.08M):
  p99.9 28 us off, 28 us writer on same socket, 31 us writer on other socket.
  Telemetry volume is 150-335 MB/s (queue stream largest); one capture filled
  the 64 GiB root disk (ENOSPC), hence results now go to `/mnt/nvme`.
- Some runs with STREAM and the other-socket writer dropped telemetry records
  (ring full); see `logging-matrix-20261122-192821/summary.json`.
- Packet logging (Nov 24, 4 cores at 1.16M, same binaries):

  | Packet logging | Responses/s | p99.9 |
  | --- | ---: | ---: |
  | off | 1,147,411 | 53 us |
  | on | 1,147,051 | 58 us |

  Throughput cost 0.03%, p99.9 +5 us (one run each; not yet repeated).

## 5. Per-request labels work

Packet log + client traces + join (`docs/packet-logging.md`). On the 1.16M
validation run (`20261124T012118Z-873fed19`, 8.1M requests):

- 100% of requests matched to their RX packet; 99.9996% of completed requests
  matched to their TX completion; 0 requests with server latency above client
  latency.
- Server (wire-to-wire) latency p50 8.2 us, p99 34.7 us; client latency p50
  16.5 us, p99 43.1 us. Client minus server is about 8 us (network + client).
- Packets wait in the NIC RX queue before the runtime processes them:
  p50 3.9 us, p99 23 us. A software timestamp at processing time would miss
  this, which is why NIC timestamps are used.
- NIC clock to TSC: the two oscillators drift by up to +-10 us over a few
  minutes relative to a single straight-line fit. The join now fits each
  1 s window and interpolates: residual p99 38 ns, max 69 ns. Earlier
  `joined.summary.json` files computed with the single-line fit (residual
  2-11 us) should be re-joined.

## 6. Open problems

1. **16-core runs stopped working on Nov 24 after the 12.4M collapse**
   (`20261124T053429Z-340cc887`, `20261124T053911Z-14a2f6d0`). Two of three
   attempts: loadgen could not connect (`Connection timed out` at startup),
   and the client IOKernel logged `warning: flow tagging not functional`
   (`iokernel/rx.c:210`: IP packets arriving at the IOKernel instead of the
   NIC steering them to loadgen's queues). The third attempt connected but
   only 0.64-0.73M of 10-12M requests/s were answered. The same warning
   appeared in an earlier run whose throughput fell to 0 mid-run
   (`20261124T042720Z-f48ab5a3`). Hypothesis, not verified: NIC flow
   steering state is left broken after an overload collapse. Next step:
   reboot both nodes (or reload mlx5), confirm a 7M run works, then repeat the
   10-12M sweep with a ramp.
2. Schedule adherence on the client was 98.9% at 1.16M on 4 cores, below the
   99% validation cutoff; client headroom at higher rates is unverified.
3. The logging-on vs logging-off difference in antagonist sensitivity (3).
4. No run yet has collected the full dataset: per-request labels with
   scheduled load and antagonist changes (`pktlog_suite.toml`) on a
   configuration that actually shows contention.

## Tooling state

- Telemetry format version 10 (adds the NIC clock stream).
- Packet logging, client traces, join, compression/verification/pruning in
  the runner: `docs/packet-logging.md`.
- Runner configs: `pktlog_validation.toml`, `pktlog_suite.toml` (capacity
  values in them are from the 4-core Nov 22 search and must be re-measured on
  new nodes), `stream_l2.toml`.
- x264 can be built as an additional best-effort workload
  (`build-workloads.sh --x264`); not yet used in a run.
