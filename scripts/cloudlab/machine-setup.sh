#!/usr/bin/env bash
# Invoke upstream machine setup and verify its effects; never run during a workload.
set -euo pipefail
usage() {
    cat <<'USAGE'
Usage: machine-setup.sh [--check-only]

Default: run upstream scripts/setup_machine.sh nouintr with root privileges,
then verify ksched, msr, hugepages, and sysctls. Refuses setup if iokerneld is
running. Reloads the kernel module and changes machine configuration.

--check-only  Verify the current machine without changing it or invoking sudo.
Logs: .git/cloudlab-setup/machine-*.log
USAGE
}
check_only=false
case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --check-only) check_only=true; shift ;;
    '') ;;
    *) usage >&2; exit 2 ;;
esac
[[ $# == 0 ]] || { usage >&2; exit 2; }
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
state=$(git rev-parse --absolute-git-dir)/cloudlab-setup
mkdir -p "$state"
log="$state/machine-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
exec > >(tee -a "$log") 2>&1
trap 'printf "Machine setup/check failed. Log: %s\n" "$log" >&2' ERR
printf 'Machine setup/check log: %s\n' "$log"

if ! $check_only; then
    if pgrep -x iokerneld >/dev/null; then
        printf 'Stop iokerneld and its applications before reloading ksched.\n' >&2
        exit 1
    fi
    if [[ ! -f ksched/ksched.ko ]]; then
        printf 'Missing ksched/ksched.ko; run ./scripts/cloudlab/build.sh first.\n' >&2
        exit 1
    fi
    if [[ $(id -u) == 0 ]]; then
        ./scripts/setup_machine.sh nouintr
    else
        sudo ./scripts/setup_machine.sh nouintr
    fi
fi

# Upstream setup does not fail on every command error, so verify independently.
failures=0
require() {
    local message=$1
    shift
    if ! "$@"; then
        printf 'FAILED: %s\n' "$message" >&2
        failures=$((failures + 1))
    fi
}
require 'ksched module is not loaded' test -d /sys/module/ksched
require '/dev/ksched is not a character device' test -c /dev/ksched
require 'msr module is not loaded' test -d /sys/module/msr
if [[ -f /sys/module/ksched/parameters/nouintr ]]; then
    require 'ksched was not loaded with nouintr enabled' \
        grep -Eq '^(Y|1)$' /sys/module/ksched/parameters/nouintr
fi
require 'transparent hugepages are not set to madvise' \
    grep -Fq '[madvise]' /sys/kernel/mm/transparent_hugepage/enabled

# Match the explicit per-node reservation in upstream scripts/setup_machine.sh.
expected_hugepages=5192
shopt -s nullglob
hugepage_files=(/sys/devices/system/node/node*/hugepages/hugepages-2048kB/nr_hugepages)
if ((${#hugepage_files[@]} == 0)); then
    printf 'FAILED: no per-node 2-MiB hugepage counters found.\n' >&2
    failures=$((failures + 1))
fi
for path in "${hugepage_files[@]}"; do
    actual=$(cat "$path")
    printf '%s: %s pages (expected %s)\n' "$path" "$actual" "$expected_hugepages"
    require "hugepage reservation incomplete: $path" test "$actual" -ge "$expected_hugepages"
done
for setting in \
    kernel.shm_rmid_forced=1 \
    kernel.shmmax=18446744073692774399 \
    vm.hugetlb_shm_group=27 \
    vm.max_map_count=16777216 \
    net.core.somaxconn=3072; do
    name=${setting%%=*}
    expected=${setting#*=}
    actual=$(sysctl -n "$name")
    printf '%s=%s\n' "$name" "$actual"
    require "unexpected sysctl: $name (expected $expected)" test "$actual" = "$expected"
done
ls -l /dev/ksched || true
grep Huge /proc/meminfo || true
if ((failures)); then
    printf '%s verification checks failed. Inspect %s and sudo dmesg.\n' "$failures" "$log" >&2
    exit 1
fi
printf 'Machine checks passed. Ready for Rust/loadgen build and the smoke test.\n'
