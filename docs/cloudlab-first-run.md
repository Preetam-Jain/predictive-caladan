# Set up fresh CloudLab nodes

This is the working setup for two xl170 nodes: **node0 runs the server; node1
runs the load generator**. Both use Caladan's IOKernel with DPDK forwarding.
Native Directpath is disabled. The client/server smoke test has run successfully;
telemetry collection and its overhead still need hardware validation.

## 1. Deploy from your development machine

From the local repository, replace the destinations with your new reservation:

```bash
./scripts/cloudlab/deploy.sh \
  Preetam@NEW_NODE0_HOST \
  Preetam@NEW_NODE1_HOST
```

This transfers the committed baseline, working diff, and new source files without
making a commit or changing your local diff. Both nodes receive ~/predictive-caladan.
Existing destinations are refused; use --repo NEW_DIRECTORY for another checkout.
SSH config is respected; --identity KEY selects a key explicitly.

## 2. Inspect each node

On **both nodes**:

```bash
cd ~/predictive-caladan
mkdir -p ~/cloudlab-logs
./scripts/cloudlab/specs.sh 2>&1 | tee ~/cloudlab-logs/specs.log
./scripts/cloudlab/nic.sh 2>&1 | tee ~/cloudlab-logs/nic.log
```

Missing /dev/ksched is expected before machine setup. The working defaults are:

| Setting | Value |
| --- | --- |
| Experiment interface / PCI address | ens1f1np1 / 0000:03:00.1 |
| Managed socket | 0 |
| IOKernel control / dataplane CPUs | 0 / 10 |
| Reserved writer CPU / sibling on node0 | 4 / 14 |
| Linux node0 / node1 IPs | 192.168.1.1 / 192.168.1.2 |
| Runtime server / client IPs | 192.168.1.101 / 192.168.1.102 |

Check these against the new nodes. Adjust the smoke script's defaults if needed.
Keep Linux's IPs and the management NIC intact. See the previous reservation's
[node inventory](cloudlab-node-inventory.md) for reference.

## 3. Build and configure both nodes

Run each step in order, continuing only after it succeeds. Run as your normal
login user; scripts request sudo for privileged operations themselves.

```bash
./scripts/cloudlab/setup.sh
./scripts/cloudlab/build.sh
./scripts/cloudlab/fix-mlx5.sh
./scripts/cloudlab/machine-setup.sh
./scripts/cloudlab/build-loadgen.sh
```

| Script | What it does |
| --- | --- |
| setup.sh | Installs packages; builds patched rdma-core, DPDK, SPDK, and PCM. |
| build.sh | Builds Caladan/runtime and ksched for the running kernel. |
| fix-mlx5.sh | Applies the mlx5 Verbs queue-size fix, rebuilds DPDK, and relinks the IOKernel. Required on our tested firmware. |
| machine-setup.sh | Loads ksched with UINTR disabled, reserves hugepages, sets sysctls, and verifies setup. |
| build-loadgen.sh | Installs Rust if needed and builds the client/server. |

The tested ConnectX-4 Lx firmware (14.18.2030) triggered a DPDK descriptor-limit
bug; fix-mlx5.sh resolved startup without changing firmware. Keep this step in the
sequence for equivalent nodes. [Patch details](../scripts/cloudlab/patches/README.md).

Machine setup should end with `Machine checks passed`. On a fresh node, the
upstream script may report that ksched and /dev/ksched did not previously exist;
the final verification determines success. Run setup before workloads, not during
them. It reserves 5192 2-MiB hugepages per NUMA node.

Logs live under .git/cloudlab-setup/. setup.sh skips successfully initialized
submodules and refuses to blindly retry a failed patching attempt. Inspect its
log or use a fresh checkout. After a reboot, rerun machine-setup.sh; rebuilding
ksched is needed if the running kernel changes.

## 4. Generate runtime configs

On **both nodes**, verify the proposed runtime IPs are unused (zero responses):

```bash
sudo arping -D -I ens1f1np1 -c 3 192.168.1.101
sudo arping -D -I ens1f1np1 -c 3 192.168.1.102
./scripts/cloudlab/smoke.sh configure
```

The generated configs use four guaranteed workers each. The client also keeps
four workers spinning, which fixed most missed send deadlines in our first test.
DPDK forwarding is the script default; no NETWORK_MODE argument is needed.

## 5. Launch in four terminals

Start each terminal in ~/predictive-caladan. Keep both IOKernels running, and
wait for `running dataplane` before starting applications.

| Order | Node | Command |
| --- | --- | --- |
| 1 | node0 | `./scripts/cloudlab/smoke.sh iokernel node0` |
| 2 | node1 | `./scripts/cloudlab/smoke.sh iokernel node1` |
| 3 | node0, another terminal | `./scripts/cloudlab/smoke.sh server` |
| 4 | node1, another terminal | `./scripts/cloudlab/smoke.sh client 10` |

The client runs one 10-second measurement at 20,000 offered requests/sec, with
constant 1000-iteration sqrt work. Iterations are not microseconds. The smoke test
reserves the writer core but disables disk telemetry. Configs and logs are saved
under .git/cloudlab-smoke-dpdk/.

Our working run reported 19,900 sent/received requests/sec, zero reported drops,
and 99.85% schedule adherence. This is a reference smoke result, not a performance
guarantee. Check all three metrics: Dropped excludes requests skipped before send;
the column labeled Target actually reports achieved sent requests/sec. Loadgen
warns below 99.9% adherence and aborts below 95%.

The DIRECTPATH DISABLED warning is expected for this configuration. These results
characterize DPDK forwarding, not native Directpath or the paper's absolute latency.

The client exits after its run. Keep the server and IOKernels running for retries;
when stopping, stop the server before its IOKernel. Re-running machine setup is
not needed between client runs.

## First telemetry capture

Leave node1's IOKernel running. Stop the server on node0, then stop node0's
baseline IOKernel. In its place run:

```bash
./scripts/cloudlab/smoke.sh capture
```

This uses the same NIC/socket/writer CPU settings, creates a unique output prefix
under ~/experiments/, and captures for 180 seconds. Capture starts immediately.
After `running dataplane`, restart smoke.sh server on node0 and smoke.sh client on
node1 in separate terminals. Plain `smoke.sh client` now runs for 120 seconds;
`smoke.sh client 10` retains the short smoke test. Start within the capture
window: the timers are independent and 180 seconds leaves about a minute for
manual startup. Wait for `telemetry: all streams drained and closed`
before terminating the capture IOKernel. Then validate the latest files on node0:

```bash
./scripts/cloudlab/smoke.sh check-capture
```

It prints decoder metadata/counts without generating large CSVs. To choose a
prefix/duration explicitly use `smoke.sh capture /path/to/run01 120`; existing
files are refused. `smoke.sh check-capture /path/to/run01` validates that capture. New version-5 captures include a fourth .sched.bin file
recording why core assignment generations change; old version-4 files remain
readable. summarize reports the reason counts.

## Troubleshooting and next experiments

- Read the last 30 lines of the failed step's log rather than copying a whole build.
- For RX CQ startup failures, run `./scripts/cloudlab/nic-debug.sh`; logs are under
  .git/cloudlab-smoke/. Confirm fix-mlx5.sh completed before retrying startup.
- If configure refuses existing configs, inspect them; it preserves hand edits.
- Preserve logs, Cargo.lock, compiler versions, source diff, configs, and hardware
  inventory before your reservation ends. Root had about 57 GB free initially,
  not the full disk's capacity.

Next is a short enabled capture and repeated logging-off/on comparisons on the
same server, with the same application CPU allocation and load. Follow
[telemetry.md](telemetry.md) for capture formats and decoding. Keep the network
path fixed across comparisons; interference workloads come after logger validation.
