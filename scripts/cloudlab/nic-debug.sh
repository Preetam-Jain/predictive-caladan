#!/usr/bin/env bash
# Collect evidence for mlx5 queue-creation failures; do not modify NIC settings.
set -uo pipefail
case "${1:-}" in
    --help|-h) printf 'Usage: nic-debug.sh [PCI_ADDRESS] (default: 0000:03:00.1)\nRead-only diagnostics, including sudo for limits/kernel/firmware queries.\n'; exit 0 ;;
esac
[[ $# -le 1 ]] || exit 2
pci=${1:-0000:03:00.1}
[[ $pci =~ ^[a-fA-F0-9]{4}:[a-fA-F0-9]{2}:[a-fA-F0-9]{2}\.[0-7]$ ]] || {
    printf 'Invalid PCI address: %s\n' "$pci" >&2
    exit 2
}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel) || exit 1
state=$(git -C "$repo" rev-parse --absolute-git-dir)/cloudlab-smoke
mkdir -p "$state"
log="$state/nic-debug-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
printf 'Diagnostic log: %s\n' "$log"
probe() {
    printf '\n--- %s ---\n' "$1"
    shift
    "$@" || printf 'Probe unavailable/failed (status %s); continuing.\n' "$?"
}
probe 'Hostname' hostname
probe 'Kernel' uname -r
probe 'PCI device' lspci -Dnn -s "$pci"
probe 'RDMA devices and firmware' ibv_devinfo
probe 'RDMA links' rdma link show
probe 'Root locked-memory limit (KiB, or unlimited)' sudo bash -c 'ulimit -l'
probe 'Hugepages' grep Huge /proc/meminfo
probe 'DPDK revision' git -C "$repo/dpdk" log -1 --format='%H %s'
probe 'DPDK descriptor-limit code' grep -n -A 7 -B 7 log_max_wq_sz \
    "$repo/dpdk/drivers/net/mlx5/mlx5_ethdev.c"
probe 'mlx5 RDMA interface mapping' ls -l /sys/bus/pci/devices/"$pci"/infiniband/
# Query only: never set firmware flags or reboot in this diagnostic script.
if command -v mstconfig >/dev/null; then
    probe 'NIC firmware configuration (query only)' sudo mstconfig -d "$pci" query
elif command -v mlxconfig >/dev/null; then
    probe 'NIC firmware configuration (query only)' sudo mlxconfig -d "$pci" query
else
    printf '\nFirmware query tool unavailable (mstconfig/mlxconfig); no changes made.\n'
fi
probe 'Recent kernel messages' sudo bash -o pipefail -c 'dmesg | tail -n 100'
printf '\nDiagnostic log: %s\n' "$log"
