#!/usr/bin/env bash
# Reproducible two-node UDP synthetic smoke test, initially without disk telemetry.
set -euo pipefail

DEFAULT_CLIENT_SECONDS=120
DEFAULT_CAPTURE_SECONDS=180
MAX_DURATION_SECONDS=86400

validate_duration() {
    local seconds=$1
    if [[ ! $seconds =~ ^[1-9][0-9]{0,4}$ ]] || ((seconds > MAX_DURATION_SECONDS)); then
        printf 'Duration must be 1–%s seconds.\n' "$MAX_DURATION_SECONDS" >&2
        return 2
    fi
}
usage() {
    cat <<'USAGE'
Usage: smoke.sh [--dry-run] configure
       smoke.sh [--dry-run] iokernel node0|node1
       smoke.sh [--dry-run] server
       smoke.sh [--dry-run] client [SECONDS]
       smoke.sh [--dry-run] capture [PREFIX [SECONDS]]
       smoke.sh check-capture [PREFIX]
       smoke.sh summarize [PREFIX]

Run configure on BOTH nodes. Then keep four terminals open:
  node0: iokernel node0    node0: server
  node1: iokernel node1    node1: client
capture replaces node0's baseline IOKernel launch and defaults to 180 seconds.
client defaults to 120 seconds; use client 10 for a short connectivity check.
check-capture validates all recorded streams (defaults to the most recent capture).
Start both IOKernels before their applications. Default logs/configs: .git/cloudlab-smoke-dpdk/.

Defaults: NIC_PCI=0000:03:00.1, WRITER_CPU=4, SERVER_IP=192.168.1.101,
CLIENT_IP=192.168.1.102, NETWORK_MODE=dpdk (IOKernel packet forwarding).
Directpath is an explicit optional override with separate configs/logs. Defaults can be overridden in the environment; use the same
IP settings for configure and launch. Runtime IPs must be unused on the LAN.
--dry-run prints configs/commands without launching any privileged operations.
USAGE
}
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
esac
dry_run=false
if [[ ${1:-} == --dry-run ]]; then
    dry_run=true
    shift
fi
[[ $# -ge 1 ]] || { usage >&2; exit 2; }
action=$1
shift
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
network_mode=${NETWORK_MODE:-dpdk}
case "$network_mode" in
    directpath) run_dir=$(git rev-parse --absolute-git-dir)/cloudlab-smoke ;;
    dpdk) run_dir=$(git rev-parse --absolute-git-dir)/cloudlab-smoke-dpdk ;;
    *) printf 'NETWORK_MODE must be directpath or dpdk.\n' >&2; exit 2 ;;
esac
server_ip=${SERVER_IP:-192.168.1.101}
client_ip=${CLIENT_IP:-192.168.1.102}
nic_pci=${NIC_PCI:-0000:03:00.1}
writer_cpu=${WRITER_CPU:-4}
server_config="$run_dir/server.config"
client_config="$run_dir/client.config"

config_text() {
    local ip=$1
    local role=$2
    local include_client_spinning=${3:-true}
    cat <<CONFIG
host_addr $ip
host_netmask 255.255.255.0
host_gateway 192.168.1.1
runtime_kthreads 4
runtime_guaranteed_kthreads 4
runtime_priority lc
CONFIG
    if [[ $role == client && $include_client_spinning == true ]]; then
        printf 'runtime_spinning_kthreads 4\n'
    fi
    if [[ $network_mode == directpath ]]; then
        printf 'enable_directpath\n'
    fi
}

case "$action" in
    configure)
        [[ $# == 0 ]] || { usage >&2; exit 2; }
        if $dry_run; then
            printf '%s\n' '--- server.config ---'
            config_text "$server_ip" server
            printf '%s\n' '--- client.config ---'
            config_text "$client_ip" client
            exit 0
        fi
        mkdir -p "$run_dir"
        # Refuse changed settings instead of silently overwriting hand edits.
        for role in server client; do
            config="$run_dir/$role.config"
            ip=$server_ip
            [[ $role == server ]] || ip=$client_ip
            # Accept only the current template or our previous zero-spinning
            # template. Hand-edited configs still require explicit attention.
            if [[ -e $config ]] &&
               ! cmp -s "$config" <(config_text "$ip" "$role") &&
               ! cmp -s "$config" <(config_text "$ip" "$role" false); then
                printf 'Config already exists with different contents: %s\n' "$config" >&2
                exit 1
            fi
        done
        config_text "$server_ip" server > "$server_config"
        config_text "$client_ip" client > "$client_config"
        printf 'Configs ready in %s\n' "$run_dir"
        exit 0 ;;
    iokernel)
        [[ $# == 1 ]] || { usage >&2; exit 2; }
        command=("$repo/iokerneld" ias numanode 0 nicpci "$nic_pci")
        case "$1" in
            node0) command+=(telemetry "$writer_cpu" - 300) ;;
            node1) command+=(nobw) ;;
            *) usage >&2; exit 2 ;;
        esac
        label="iokernel-$1" ;;
    capture)
        [[ $# -le 2 ]] || { usage >&2; exit 2; }
        capture_prefix=${1:-$HOME/experiments/capture-$(date -u +%Y%m%dT%H%M%SZ)-$$}
        capture_seconds=${2:-$DEFAULT_CAPTURE_SECONDS}
        validate_duration "$capture_seconds"
        command=("$repo/iokerneld" ias numanode 0 nicpci "$nic_pci"
                 telemetry "$writer_cpu" "$capture_prefix" "$capture_seconds")
        printf 'Capture prefix: %s\n' "$capture_prefix"
        label=iokernel-capture ;;
    check-capture|summarize)
        [[ $# -le 1 ]] || { usage >&2; exit 2; }
        if [[ $# == 1 ]]; then
            capture_prefix=$1
        elif [[ -f $run_dir/latest-capture-prefix ]]; then
            IFS= read -r capture_prefix < "$run_dir/latest-capture-prefix"
        else
            printf 'No saved capture prefix; supply one explicitly.\n' >&2
            exit 1
        fi
        if [[ $action == summarize ]]; then
            if $dry_run; then
                printf 'Would summarize: %s\n' "$capture_prefix"
                exit 0
            fi
            exec python3 "$repo/scripts/summarize_telemetry.py" "$capture_prefix"
        fi
        if $dry_run; then
            printf 'Would validate capture: %s\n' "$capture_prefix"
            exit 0
        fi
        exec python3 "$repo/scripts/decode_telemetry.py" --check-prefix "$capture_prefix"
        exit 0 ;;
    server)
        [[ $# == 0 ]] || { usage >&2; exit 2; }
        command=("$repo/apps/loadgen/target/release/loadgen" "$server_ip:5000"
                 --config "$server_config" --mode spawner-server
                 --transport udp --fakework sqrt)
        label=server ;;
    client)
        [[ $# -le 1 ]] || { usage >&2; exit 2; }
        client_seconds=${1:-$DEFAULT_CLIENT_SECONDS}
        validate_duration "$client_seconds"
        command=("$repo/apps/loadgen/target/release/loadgen" "$server_ip:5000"
                 --config "$client_config" --mode runtime-client
                 --transport udp --protocol synthetic --fakework sqrt
                 --distribution constant --mean 1000 --conns 16
                 --mpps 0.02 --samples 1 --runtime "$client_seconds" --rampup 1 --output normal)
        label=client ;;
    *) usage >&2; exit 2 ;;
esac
printf 'Network mode: %s\nCommand: ' "$network_mode"
printf '%q ' "${command[@]}"
printf '\n'
if $dry_run; then
    exit 0
fi
if [[ ! -x ${command[0]} ]]; then
    printf 'Executable missing: %s; complete the build first.\n' "${command[0]}" >&2
    exit 1
fi
if [[ ! -c /dev/ksched ]]; then
    printf '/dev/ksched missing; run machine-setup.sh first.\n' >&2
    exit 1
fi
if [[ ! -e /sys/bus/pci/devices/$nic_pci ]]; then
    printf 'NIC PCI address not found: %s\n' "$nic_pci" >&2
    exit 1
fi
if [[ ($action == server || $action == client) && (! -f $server_config || ! -f $client_config) ]]; then
    printf 'Run ./scripts/cloudlab/smoke.sh configure first.\n' >&2
    exit 1
fi
if [[ $action == capture ]]; then
    if pgrep -x iokerneld >/dev/null; then
        printf 'Stop node0 server and IOKernel before starting capture.\n' >&2
        exit 1
    fi
    for stream in bw llc pmc-failure sched; do
        if [[ -e $capture_prefix.$stream.bin ]]; then
            printf 'Capture file already exists: %s.%s.bin; choose a fresh prefix.\n' "$capture_prefix" "$stream" >&2
            exit 1
        fi
    done
    mkdir -p "$(dirname "$capture_prefix")"
    mkdir -p "$run_dir"
    printf '%s\n' "$capture_prefix" > "$run_dir/latest-capture-prefix"
fi
mkdir -p "$run_dir"
log="$run_dir/$label-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
printf 'Log: %s\n' "$log"
if [[ $(id -u) == 0 ]]; then
    exec "${command[@]}"
else
    exec sudo -- "${command[@]}"
fi
