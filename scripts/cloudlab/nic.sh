#!/usr/bin/env bash
# Read-only NIC/setup checks. Default interface is the current profile's data LAN.
set -uo pipefail
case "${1:-}" in
    --help|-h) printf 'Usage: nic.sh [EXPERIMENT_INTERFACE] (default: ens1f1np1)\n'; exit 0 ;;
esac
[[ $# -le 1 ]] || { printf 'Usage: nic.sh [EXPERIMENT_INTERFACE]\n' >&2; exit 2; }
interface=${1:-ens1f1np1}
[[ $interface =~ ^[a-zA-Z0-9_.:-]+$ ]] || { printf 'Invalid interface: %s\n' "$interface" >&2; exit 2; }

printf '\n--- Experiment NIC: %s ---\n' "$interface"
if [[ -e /sys/class/net/$interface/device ]]; then
    readlink -f "/sys/class/net/$interface/device"
    cat "/sys/class/net/$interface/device/numa_node"
else
    printf 'Interface/device not found; check ip -br addr.\n' >&2
fi
printf '\n--- Relevant modules ---\n'
lsmod | grep -E 'ksched|mlx5|msr' || printf 'No matching modules found (or lsmod unavailable).\n'
printf '\n--- Known checkout paths ---\n'
found=false
for repo in "$HOME/predictive-caladan" "$HOME/caladan"; do
    if [[ -d $repo ]]; then
        printf '%s\n' "$repo"
        found=true
    fi
done
$found || printf 'No checkout at ~/predictive-caladan or ~/caladan.\n'
printf '\n--- Root filesystem ---\n'
df -h /
