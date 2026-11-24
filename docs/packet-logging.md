# Packet logging and per-request traces

Per-request latency labels for the dataset. The server runtime records a NIC
timestamp for every TCP segment it receives or sends. The load generator
records each request's byte range on its connection. Joining the two places
every request on the server TSC timeline, the same clock as the IOKernel
telemetry.

## What is recorded

| Where | File | Contents |
| --- | --- | --- |
| Server runtime (memcached) | `pktlog.pkt.kNNN.bin`, one per kthread | 32-byte records: RX/TX NIC completion timestamp, TCP sequence range, client port; CONN records with the connection's initial sequence numbers |
| Server IOKernel writer | `capture.nicclock.bin` | NIC clock read every 1 ms, bracketed by TSC reads (telemetry stream 9, format version 10) |
| Client loadgen | `ctrace.cNNN.sNNN.bin`, one per connection | 48-byte records: scheduled/actual send time, client latency, request and response end byte offsets, status |

Layouts: `inc/pktlog.h` (segment, packet records, dump files),
`write_request_trace` in `apps/loadgen/src/main.rs` (client traces),
`docs/telemetry-format-versions.md` (NIC clock stream).

How a request is matched: TCP sequence number minus the connection's initial
sequence number gives a byte offset. Loadgen counts the bytes it writes and
reads on each connection, so request *k* is the byte range ending at its
`req_end` and its response ends at `resp_end`. Memcached answers in order on a
TCP connection. Wraparound of the 32-bit sequence numbers is undone offline.

### Server-side capture path

- RX: `mbuf_fill_cqe` (both mlx5 RX paths) copies the CQE timestamp into
  `mbuf->timestamp`; `tcp_rx_conn` appends an RX record for segments with payload.
- TX: `mlx5_transmit_one` marks TCP payload mbufs (`mbuf->pktlog_tx`);
  `mlx5_gather_completions` appends a TX record with the completion's timestamp.
  Caladan gathers TX completions lazily, which is fine: the NIC wrote the
  timestamp when the send completed. The last few responses of a run may lack
  a TX record if their completions were never gathered; the join reports this.
- CONN: `tcp_conn_set_state` on reaching ESTABLISHED.

### The segment

A 64 GiB file on hugetlbfs (`/dev/hugepages/caladan-pktlog`), with its pages
bound to NUMA node 0, the socket that does not run Caladan. Each runtime
kthread appends to its own 64 MiB chunk with non-temporal stores (`movnti`),
so logging does not read remote lines or fill the Caladan socket's caches.
Nothing reads the segment during a run; `pktlogctl dump` writes it to files
after memcached exits. A file is used instead of SysV shared memory because
machine setup sets `kernel.shm_rmid_forced=1`, which would destroy a SysV
segment as soon as no process had it attached.

Capacity: 64 GiB holds about 2 billion records, about 125 s at 8M requests/s
(one RX and one TX record per request). A full segment drops records and the
run is marked failed; it never blocks the packet path.

## Operating procedure

The runner (`scripts/experiments/run.py`, needs Python 3.11+, e.g. `python3.12`)
does every step below automatically when `[pktlog] enabled = true`. The manual
commands are for checking and recovery.

### Once per reservation

1. Build on both nodes as usual (`make` also builds `scripts/pktlog/pktlogctl`;
   `scripts/cloudlab/setup.sh` installs `zstd`). Rebuild memcached against the
   new runtime and rebuild loadgen (`cargo build --release`).
2. On each node: `sudo scripts/cloudlab/mount-nvme.sh`. Results go to
   `/mnt/nvme/caladan-experiments` (the root partition is only 64 GiB). The
   script formats the NVMe only if it is completely empty and refuses otherwise.
3. On the desktop: create the join environment once:
   `python3.12 -m venv ~/.venvs/predictive-caladan && ~/.venvs/predictive-caladan/bin/pip install numpy`
   and make sure `pktlog.python` in the config points to it.
4. Fill in `nodes.*.host` in `scripts/experiments/pktlog_validation.toml` and
   `pktlog_suite.toml`.

The runner reserves the hugepages and creates the segment at the start of each
suite (`scripts/cloudlab/pktlog-setup.sh`). Reserve early after boot: a long
running machine may not have 32k free 2 MiB blocks, and the script fails rather
than reserving fewer.

### Validation run (do this first, every reservation)

```
python3.12 scripts/experiments/run.py --config scripts/experiments/pktlog_validation.toml --run
```

One 10 s logging-on run. Afterwards check, in the run's attempt directory:

- `run.json`: `state` is `complete`; `pktlog.records` > 0; `join` present.
- `joined.summary.json`: `ok` true. Look at `requests.rx_match_rate` and
  `tx_match_rate_of_completed` (should be ≥ 0.999), `server_longer_than_client`
  (should be ~0), `clock_fit.residual_ns_p99` (about 40 ns on the r650s; the
  join fails above 200 ns), `clock_fit.cycles_per_nic_tick` vs
  `expected_cycles_per_nic_tick`, and `nic_to_runtime.*_tsc_before_nic_time`
  (should be 0).
- `server/raw-verification.json` and `client/raw-verification.json` exist.
- Note the compressed sizes and `seconds` in `*/compression-manifest.json` and
  the transfer time; they decide how many runs fit in the night (see below).

If the join fails, stop and report; do not start the suite.

### Suite

```
python3.12 scripts/experiments/run.py --config scripts/experiments/pktlog_suite.toml --run
```

Per run, after the load finishes and telemetry is finalized, the runner:

1. stops all run processes;
2. dumps the segment to `pktlog.pkt.kNNN.bin` and only then resets it;
3. compresses every `*.bin` with zstd on each node and checks that each
   compressed file decompresses to the same SHA-256 before deleting the raw file;
4. transfers the run to the desktop and checks every file's SHA-256;
5. decompresses each file on the desktop and checks the raw SHA-256 again;
6. deletes the run's files from the nodes (`prune_remote`), only after step 5.

Resume an interrupted suite with `--resume results/<suite-dir>`; completed runs
are skipped.

## Rules for not losing data

- Never delete or reset the segment by hand unless `pktlogctl stat` shows
  `"dumped": true` or `"empty": true`. `reset` and `destroy` refuse otherwise;
  never pass `--discard-undumped` without the user's approval.
- Never delete anything in `results/` on the desktop.
- Never delete files on a node that are not listed in the run's
  `raw-verification.json` on the desktop. The runner only prunes verified files.
- If any step fails, the run is marked failed, its files stay wherever they
  were, and the suite stops (`continue_after_failure = false`). Do not change
  that setting.
- CloudLab erases the nodes when the reservation ends. Stop starting new runs
  when the remaining time is less than about twice one run's total time
  (load + dump + compression + transfer), so nothing is left on the nodes.

## Recovering from failures

| Symptom | Meaning | Action |
| --- | --- | --- |
| Preflight: "segment is not empty or is in use" | A previous run's records were not dumped | On the server: `sudo scripts/pktlog/pktlogctl stat --path /dev/hugepages/caladan-pktlog`. If `in_use`, a memcached is still running: find and stop it. Otherwise dump to the previous run's directory on the node: `sudo scripts/pktlog/pktlogctl dump --path ... --out <run-dir>/pktlog --experiment-id-from <run-dir>/capture.bw.bin`, then `reset`. Report which run it belonged to. |
| "server pktlog dump" error | Dump failed; segment still holds the records | Do not reset. Report the error and the run id. |
| "Packet log segment filled up" | Run exceeded 64 GiB of records | Data up to the limit is kept. Shorten the run or lower the rate, or raise `pktlog.gib` if node 0 has free hugepages. |
| "compression" or "Decompressed ... does not match" | A compressed copy is bad | The raw file was not deleted. Report; do not prune by hand. |
| Runtime fails to start: "pktlog: ..." in `memcached.log` | Segment missing, in use, or not empty | Run `pktlogctl stat`; see the first row. |
| "Load finished before scheduled antagonist events" | Schedule offsets exceed the run | Fix `[schedule]` offsets (seconds after load start). |

## Choosing the compression level and run count

`storage.zstd_level = 19` gave 9-60x on the existing telemetry streams
(measured on 2026-11-22 captures: queue 62x, llc 10x, task 9x, delay 11x) at
about 50 MB/s per 16 threads. If the validation run shows compression taking
longer than the run itself, lower it to 9 (about 20x faster, somewhat larger
files). Packet and client traces have not been measured yet; the validation
run's `compression-manifest.json` gives their ratio.

Desktop space: check `df -h` on the desktop before the suite. Runs per night ≈
min(free space / compressed size per run, remaining time / time per run).

## Offline join

```
~/.venvs/predictive-caladan/bin/python scripts/join_requests.py \
    --client 'DIR/client/ctrace.*.bin' --packets 'DIR/server/pktlog.pkt.k*.bin' \
    --nicclock DIR/server/capture.nicclock.bin --out DIR/joined --window-us 100 --slo-us 50
```

(Decompress the `.zst` files first: `zstd -d --long=27 FILE.zst`.) Outputs
`joined.requests.npy` (one row per request: port, status, client latency,
`server_rx_tsc`, `server_tx_tsc`, server latency, flags),
`joined.windows.npy` (per-window count, mean, p50, p99, p99.9, max, fraction
over the SLO, keyed by server receive time) and `joined.summary.json`.

## Tests

```
tests/pktlog/segment_test.sh            # pktlogctl with a real segment file
python3 tests/pktlog/join_test.py       # needs numpy
python3.12 tests/experiments/launcher_test.py
cc -D_GNU_SOURCE -std=gnu11 -O2 -Wall -Iinc -pthread tests/telemetry/writer_test.c -o /tmp/w && /tmp/w /tmp/w-prefix
```

Not testable off the nodes: the runtime and IOKernel changes (need rdma-core
and a ConnectX NIC) and the loadgen changes (Rust toolchain). The validation
run is their test.
