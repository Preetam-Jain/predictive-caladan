# CloudLab experiment node inventory

Recorded November 4, 2026, from the user's SSH command output. This is an initial
snapshot, not evidence of a successful Caladan build, setup, or experiment.

## Nodes and operating system

| Role | Hostname | Kernel |
| --- | --- | --- |
| Server under test (node0) | node0.preetam-324610.cis-7000-fa26-pg0.utah.cloudlab.us | 5.15.0-198-generic |
| Load generator (node1) | node1.preetam-324610.cis-7000-fa26-pg0.utah.cloudlab.us | 5.15.0-198-generic |

The profile specifies Ubuntu 22.04; /etc/os-release has not yet been captured.
SSH username and repository location on each node have not been recorded.

## CPU topology (identical on both nodes)

One socket (socket 0), one NUMA node (node 0), 10 physical cores, and 20 online
logical CPUs. Physical core IDs 0–9 each have two logical CPUs:

| Physical core | Logical CPU pair |
| ---: | --- |
| 0 | 0, 10 |
| 1 | 1, 11 |
| 2 | 2, 12 |
| 3 | 3, 13 |
| 4 | 4, 14 |
| 5 | 5, 15 |
| 6 | 6, 16 |
| 7 | 7, 17 |
| 8 | 8, 18 |
| 9 | 9, 19 |

CPU model/frequency details were not supplied. For a writer on logical CPU 4,
our implementation also reserves CPU 14. That is a candidate allocation, not an
observed runtime assignment. Confirm IOKernel startup's control/dataplane choices
before fixing it in experiment commands. Use numanode 0 for the initial setup.

## Network interfaces

| Interface | State on both nodes | node0 IPv4 | node1 IPv4 | Purpose |
| --- | --- | --- | --- | --- |
| lo | UNKNOWN | 127.0.0.1/8 | 127.0.0.1/8 | Loopback |
| eno49np0 | UP | 128.110.218.58/21 | 128.110.218.62/21 | Public/control network |
| eno50np1 | DOWN | — | — | Not established |
| ens1f0np0 | DOWN | — | — | Not established |
| ens1f1np1 | UP | 192.168.1.1/24 | 192.168.1.2/24 | Experiment LAN |

IPv6 addresses reported:

| Interface | node0 | node1 |
| --- | --- | --- |
| lo | ::1/128 | ::1/128 |
| eno49np0 | fe80::9af2:b3ff:fec8:1a54/64 | fe80::9af2:b3ff:fec8:2934/64 |
| ens1f1np1 | fe80::9edc:71ff:fe5e:2f91/64 | fe80::9edc:71ff:fe56:af85/64 |

eno49np0 reported IPv4 metric 1024 on each node. Routing-table output was not
supplied. Keep Linux's .1 and .2 addresses intact; Caladan runtime IPs must be
separate, unused addresses. .101 and .102 in the runbook remain proposals until
availability is checked.

Both nodes reported these PCI Ethernet functions, all Mellanox MT27710 Family
ConnectX-4 Lx, vendor/device ID 15b3:1015:

- 0000:03:00.0
- 0000:03:00.1
- 0000:07:00.0
- 0000:07:00.1

The actual interface-to-PCI mapping has **not** been verified. Do not select the
NIC solely from its PCI listing or inferred interface naming. On each node run:

```
readlink -f /sys/class/net/ens1f1np1/device
cat /sys/class/net/ens1f1np1/device/numa_node
```

NIC firmware version, RDMA device status, and Directpath/DEVX functionality remain
unverified.

## Memory (initial free -h snapshot)

| Field | node0 | node1 |
| --- | --- | --- |
| Total RAM | 62 GiB | 62 GiB |
| Used | 1.1 GiB | 1.1 GiB |
| Free | 61 GiB | 61 GiB |
| Shared | 4.0 MiB | 4.0 MiB |
| Buffers/cache | 265 MiB | 233 MiB |
| Available | 61 GiB | 61 GiB |
| Swap total / used | 8.0 GiB / 0 B | 8.0 GiB / 0 B |

Hugepage reservation and NUMA memory placement have not been measured.

## Storage (identical lsblk output)

| Device | Size | Type | Mountpoint | Model |
| --- | --- | --- | --- | --- |
| sda | 447.1 GiB approximately (lsblk: 447.1G) | disk | — | VK000480GWCNQ |
| sda1 | 256 MiB (256M) | partition | /boot/efi | — |
| sda2 | 1 MiB (1M) | partition | — | — |
| sda3 | 64 GiB (64G) | partition | / | — |
| sda99 | 8 GiB (8G) | partition | [SWAP] | — |

The root partition is only 64G, despite the disk being 447.1G. The output does not
establish the amount of free filesystem space or how the rest of the disk is
allocated. No partitioning or formatting has been performed. Check df -h and the
full partition table before planning capture storage. Disk throughput is untested.

## Caladan readiness

On both nodes:

```
ls: cannot access '/dev/ksched': No such file or directory
```

The ksched device node is absent. This alone does not establish whether the module
is built, installed, or loaded. Check lsmod and build/setup state before running
the workloads. Repository synchronization, dependency builds, Rust/loadgen build,
module insertion, hugepages, hardware counters, and workload execution remain
unverified.

## Next checks

1. Locate the checkout on both nodes and confirm it contains the current telemetry
   sources, including uncommitted/new files from the development machine.
2. Verify the experiment interface's PCI mapping, OS release, and ksched/module state.
3. Build and set up missing components using [the first-run guide](cloudlab-first-run.md).
4. Confirm runtime IP availability and IOKernel CPU reservations.
5. Run the client/server smoke test, then logging off/on on the same server.

## Follow-up checks (November 4, 2026)

Both nodes verified that ens1f1np1 maps to PCI **0000:03:00.1**:

```
/sys/devices/pci0000:00/0000:00:03.0/0000:03:00.1
```

Both reported mlx5_core, mlx5_ib, ib_uverbs, ib_core and associated Mellanox
modules loaded. No ksched entry appeared in the filtered module listing. The
intel_rapl_msr module is loaded; that does not establish that the separate msr
module required by setup has been loaded. Repository-location checks returned no
paths for ~/predictive-caladan or ~/caladan on either node.

Both root filesystems reported:

```
/dev/sda3  63G total  2.7G used  57G available  5% used  mounted on /
```

These checks supersede the unverified NIC mapping and filesystem-free-space notes
above. Hugepages, CPU model, firmware, runtime IP availability, and builds remain
unverified. Local development changes are intentionally uncommitted; deployment
must transfer the working tree without requiring a commit.


## Runtime IP checks

The user reported zero duplicate-address ARP responses for 192.168.1.101 and
192.168.1.102 from both nodes. These are the selected runtime server/client IPs
for the initial smoke test; Linux retains 192.168.1.1 and 192.168.1.2. Recheck
availability on future CloudLab reservations before reusing them.

## Initial node0 IOKernel attempt

The user supplied startup output showing control CPU 0, dataplane CPU 10,
2394 TSC ticks/us, Intel family 6/model 79, and PCM initialization past memory
controller discovery. DPDK then failed to initialize the mlx5 receive completion
queue. Its preceding TX message reported a requested descriptor count of 1 despite
Caladan's configured mlx5 ring size of 2048.

This sequence matches a reported DPDK descriptor-limit regression when DevX is
unavailable, but the cause on these nodes is not yet confirmed. Firmware/DevX
status and the deployed driver code need inspection. Reference:
https://www.mail-archive.com/dev@dpdk.org/msg307677.html

Use scripts/cloudlab/nic-debug.sh to collect read-only evidence before changing
firmware settings, driver source, or hugepage reservations.

## node0 NIC diagnostic results

User-provided nic-debug output established:

- Experiment PCI function 0000:03:00.1 maps to RDMA device mlx5_3 and Linux
  interface ens1f1np1; its link is ACTIVE.
- All four RDMA devices report firmware 14.18.2030. The experiment NIC reports
  board ID HP_2420110034; the management NIC reports HP_2690110034.
- Root locked-memory limit: 8,214,036 KiB.
- 5,192 hugepages total, 5,128 free after the failed startup (64 pages used).
- Patched DPDK HEAD: df7c03cdcde5ff7b0870a666f1b39540a8a034b7.
- mlx5_ethdev.c unconditionally computes RX/TX maximum descriptors using
  `1 << hca_attr.log_max_wq_sz`, matching the known no-DevX regression.
- No mstconfig/mlxconfig tool was installed, so UCTX_EN/DevX configuration has
  not yet been queried. The captured dmesg tail contains no new mlx5 failure.

Firmware 14.18.2030 predates the ConnectX-4 Lx minimum 14.21.1000 documented in
DPDK's mlx5 guide. This is a compatibility issue to investigate, not proof that a
firmware change alone resolves all startup issues:
https://doc.dpdk.org/guides-20.11/nics/mlx5.html

The upstream fix for queue-size queries supports both Verbs and DevX modes:
https://mails.dpdk.org/archives/stable/2025-May/052505.html

mstflint is now included in the setup dependency list so nic-debug.sh can query
firmware configuration. No NIC configuration or firmware has been changed.

## node0 firmware configuration query

mstconfig successfully queried the experiment NIC. It reports ConnectX4LX and a
Next Boot configuration list, but exposes no UCTX_EN field. That omission alone
does not establish DevX availability. No settings were written.

A reproducible software fallback is prepared: fix-mlx5.sh backports the upstream
Verbs/DevX queue-size fix and rebuilds DPDK/IOKernel. NETWORK_MODE=dpdk selects
IOKernel packet forwarding for the smoke test, omitting runtime enable_directpath.
Native Directpath remains selectable but unverified with this firmware. The
compatibility build and packet-forwarding test have not yet run on CloudLab.


## First successful packet exchange

After the mlx5 fix, node0 reached "core 10 running dataplane" with experiment MAC
9c:dc:71:5e:2f:91. The first client run received responses with no reported network
drops, but met only 22.49% of its send schedule (about 4,474 sends/receives per
second against 20,000 offered). Loadgen aborted due to low schedule adherence.
Those latency percentiles are not a valid offered-load baseline.

Client startup reported four guaranteed workers but zero spinning workers. The
smoke config now requests four spinning client workers; this is a candidate fix
for scheduling misses, pending a rerun. Native Directpath is still disabled.

## Working smoke-test result

The user reran after enabling four spinning client workers and reported 19,900
sent/received requests/sec, zero reported drops, and 99.85% schedule adherence
(up from 22.49%). Loadgen exited successfully, with its warning below 99.9%
adherence still present. Node0's IOKernel reached running dataplane after the
DPDK compatibility fix. This establishes a working two-node DPDK client/server
smoke test; native Directpath, enabled telemetry capture, and logger overhead
remain unvalidated on hardware.
