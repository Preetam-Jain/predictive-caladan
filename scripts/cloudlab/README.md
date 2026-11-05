# CloudLab scripts

Follow [Set up fresh CloudLab nodes](../../docs/cloudlab-first-run.md) for the
single, ordered setup and smoke-test procedure. The tested path uses DPDK
forwarding; no NETWORK_MODE prefix is needed.

| Script | Run where | Purpose |
| --- | --- | --- |
| deploy.sh USER@NODE0 USER@NODE1 | Development machine | Transfer the baseline, working diff, and new source without committing. |
| specs.sh | Each node | Read-only hardware/system inventory. |
| nic.sh [INTERFACE] | Each node | Read-only NIC mapping, module, checkout, and disk checks. |
| setup.sh | Each node | Install dependencies and build submodules. |
| build.sh [--jobs N] | Each node | Build Caladan and ksched. |
| fix-mlx5.sh | Each node | Apply the queue-limit compatibility fix and rebuild/relink. |
| machine-setup.sh [--check-only] | Each node | Configure and verify modules, hugepages, and sysctls. |
| build-loadgen.sh | Each node | Install Rust if needed and build loadgen. |
| smoke.sh configure | Each node | Generate server/client configs. |
| smoke.sh iokernel node0\|node1 | Respective node | Run the local IOKernel. |
| smoke.sh server / client [SECONDS] | node0 / node1 | Run server / load generator (client default: 120 seconds; use 10 for smoke test). |
| smoke.sh capture [PREFIX [SECONDS]] | node0 | Run the IOKernel with telemetry; default unique prefix and 180 seconds. |
| smoke.sh check-capture [PREFIX] | node0 | Validate the latest or specified capture (three streams in v4, four in v5) without CSV output. |
| smoke.sh summarize [PREFIX] | node0 | Summarize sample intervals, bandwidth, LLC activity, and failures. |
| nic-debug.sh [PCI_ADDRESS] | Affected node | Read-only mlx5 startup diagnostics. |

Every script supports --help. Deployment refuses existing target directories;
--repo NAME chooses another one. setup.sh supports --packages-only and
--skip-packages, and guards against unsafe submodule retries. Build/setup logs
are under .git/cloudlab-setup/; default smoke configs/logs are under
.git/cloudlab-smoke-dpdk/. Preserve these before releasing nodes.

Native Directpath remains available through an explicit NETWORK_MODE=directpath
override, but is not validated on the tested firmware. Its configs/logs are separate.
See [mlx5 patch provenance](patches/README.md) and the
[node inventory](../../docs/cloudlab-node-inventory.md) for hardware details.

Local script tests (mocked SSH, package installation, and builds):

```bash
python3 tests/cloudlab/scripts_test.py
```
