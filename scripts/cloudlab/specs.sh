#!/usr/bin/env bash
# Read-only inventory. Missing optional devices/tools do not stop the report.
set -uo pipefail
case "${1:-}" in
    --help|-h) printf 'Usage: specs.sh (run on each node; optionally pipe through tee)\n'; exit 0 ;;
    '') ;;
    *) printf 'Usage: specs.sh\n' >&2; exit 2 ;;
esac
[[ $# == 0 ]] || exit 2

probe() {
    printf '\n--- %s ---\n' "$1"
    shift
    "$@" || printf 'Unavailable or failed (status %s); continuing inventory.\n' "$?" >&2
}

probe 'Hostname' hostname
probe 'Kernel' uname -r
probe 'OS release' cat /etc/os-release
probe 'CPU details' lscpu
probe 'CPU topology' lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
probe 'Network addresses' ip -br addr
probe 'Routes' ip route
probe 'Ethernet PCI devices' bash -o pipefail -c "lspci -Dnn | grep -i -E 'ethernet|mellanox'"
probe 'Memory' free -h
probe 'Block devices' lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS,MODEL
probe 'ksched device' ls -l /dev/ksched
